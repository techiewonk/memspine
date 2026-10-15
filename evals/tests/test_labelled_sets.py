"""X-05: structure, vocabulary, balance and leak checks for the synthetic labelled sets."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

from labelled import loader as L  # noqa: E402

NAMES = ("grounding", "entity_pairs", "contradictions", "global_questions")
FILES = {n: L.HERE / f"{n}.v1.jsonl" for n in NAMES}


def _gold_lint():
    path = ROOT / "tests" / "unit" / "test_prompt_gold_lint.py"
    spec = importlib.util.spec_from_file_location("_gold_lint_for_labelled", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _share(counter: Counter[str], key: str) -> float:
    return counter[key] / sum(counter.values())


@pytest.mark.parametrize("name", NAMES)
def test_file_parses_with_header(name: str) -> None:
    lines = FILES[name].read_text(encoding="utf-8").split("\n")
    assert lines[-1] == "", "file must end with a newline"
    rows = [json.loads(ln) for ln in lines[:-1]]
    head = rows[0]
    assert set(head) == {"set", "version", "schema", "description"}
    assert head["set"] == name
    assert isinstance(head["version"], int) and not isinstance(head["version"], bool)
    assert head["version"] == 1
    assert head["schema"] == f"{name}/1"
    assert head["description"]
    assert len(rows) > 1


@pytest.mark.parametrize("name", NAMES)
def test_ids_unique(name: str) -> None:
    ids = [r["id"] for r in L.LOADERS[name]().records]
    assert len(ids) == len(set(ids))


def test_sizes() -> None:
    assert len(L.load_grounding().items) == 62
    assert len(L.load_entity_pairs().items) == 80
    assert len(L.load_contradictions().items) == 44
    g = L.load_global_questions().items
    assert 8 <= len(g.conversations) <= 10
    assert len(g.questions) == 20


def test_grounding_vocabulary_and_turn_references() -> None:
    items = L.load_grounding().items
    for it in items:
        assert it.label in L.GROUNDING_LABELS
        assert it.claimed_modality in L.MODALITIES
        assert it.binding_basis in L.BINDING_BASES
        assert it.failure is None or it.failure in L.GROUNDING_FAILURES
        turn_ids = [t.turn_id for t in it.turns]
        assert len(turn_ids) == len(set(turn_ids))
        for tid in (*it.support_turns, *it.binding_turns):
            assert tid in turn_ids, (it.id, tid)
        if it.label == "supported":
            assert it.failure is None and it.unsupported_part is None
            assert it.support_turns and it.span
        elif it.label == "partly":
            assert it.failure and it.unsupported_part
            assert it.support_turns and it.span
        else:
            assert it.failure and not it.support_turns and it.span is None
        if it.span:
            assert it.span.turn_id in it.support_turns
            text = next(t.text for t in it.turns if t.turn_id == it.span.turn_id)
            assert text[it.span.start : it.span.end] == it.span.text
        if it.binding_basis == "turn":
            assert it.binding_turns
        if it.binding_basis == "source_time":
            assert it.source_time


def test_grounding_balance_and_coverage() -> None:
    items = L.load_grounding().items
    labels = Counter(i.label for i in items)
    for lab in L.GROUNDING_LABELS:
        assert 0.25 <= _share(labels, lab) <= 0.42, labels
    mods = {i.claimed_modality for i in items}
    assert {"hypothetical", "planned", "negated", "reported", "question"} <= mods
    bases = Counter(i.binding_basis for i in items)
    assert bases["turn"] >= 5 and bases["source_time"] >= 3
    assert len({i.lang for i in items}) >= 4


def test_entity_pairs_vocabulary_and_balance() -> None:
    items = L.load_entity_pairs().items
    for p in items:
        assert p.label in L.PAIR_LABELS
        assert p.category in L.PAIR_CATEGORIES
        assert p.a.mention and p.b.mention and p.a.context and p.b.context
    labels = Counter(p.label for p in items)
    assert 0.45 <= _share(labels, "same") <= 0.55, labels
    for cat in L.PAIR_CATEGORIES:
        sub = Counter(p.label for p in items if p.category == cat)
        assert sub["same"] and sub["different"], cat
        assert 0.25 <= _share(sub, "same") <= 0.75, (cat, sub)
    subs = {p.subcategory for p in items if p.category == "alias"}
    assert {"nickname", "initials", "transliteration"} <= subs
    langs = {x for p in items for x in p.languages}
    assert {"en", "de", "nl"} <= langs and len(langs) >= 4


def test_contradictions_vocabulary_and_rules() -> None:
    items = L.load_contradictions().items
    rank = {"low": 0, "default": 1}
    for c in items:
        assert c.expected in L.OUTCOMES
        assert c.reason in L.OUTCOME_REASONS
        assert c.cardinality in L.CARDINALITIES and c.stage in L.STAGES
        assert c.existing
        ids = [a.id for a in (*c.existing, c.incoming)]
        assert len(ids) == len(set(ids))
        for a in (*c.existing, c.incoming):
            assert a.modality in L.MODALITIES
            assert a.binding in L.BINDINGS
            assert a.source_trust in L.TRUST_TIERS
        inc, cur = c.incoming, c.existing[-1]
        bound = inc.binding == "bound"
        full = inc.modality == "asserted" and bound
        negation = inc.modality == "negated" and bound
        if c.expected == "supersede":
            assert c.cardinality == "one" and full
            assert rank[inc.source_trust] >= rank[cur.source_trust]
        elif c.expected == "retire":
            # a bound negation at cardinality one, trust >= current, closes the interval
            assert c.cardinality == "one" and negation and inc.value == "none"
            assert rank[inc.source_trust] >= rank[cur.source_trust]
        elif c.expected == "contest":
            assert c.cardinality == "one" and (full or negation) and c.reason == "trust"
            assert rank[inc.source_trust] < rank[cur.source_trust]
        elif c.expected == "tentative":
            assert not full and not negation
            assert c.reason == ("binding" if inc.binding != "bound" else "modality")
        else:
            assert c.reason in ("cardinality", "time")
    outcomes = Counter(c.expected for c in items)
    for o in L.OUTCOMES:
        if o == "retire":
            assert outcomes[o] >= 2, outcomes
        else:
            assert 0.15 <= _share(outcomes, o) <= 0.35, outcomes
    negated = [c for c in items if c.incoming.modality == "negated"]
    assert len(negated) >= 3
    assert len({c.lang for c in items}) >= 4


def test_global_questions_references() -> None:
    g = L.load_global_questions().items
    conv_ids = {c.id for c in g.conversations}
    assert len(conv_ids) == len(g.conversations)
    turn_ids = [t.turn_id for c in g.conversations for t in c.turns]
    assert len(turn_ids) == len(set(turn_ids))
    assert len({c.lang for c in g.conversations}) >= 4
    for q in g.questions:
        assert q.question_type in L.QUESTION_TYPES
        assert q.expected_conversations and q.answer_elements
        assert set(q.expected_conversations) <= conv_ids, q.id
        assert len(set(q.expected_conversations)) == len(q.expected_conversations)
        if q.expected_count is not None:
            assert q.expected_count > 0
    kinds = Counter(q.question_type for q in g.questions)
    assert len(kinds) >= 3
    wide = [q for q in g.questions if len(q.expected_conversations) >= 5]
    assert len(wide) >= 4, "need several questions that span many conversations"


def test_content_hash_is_stable_and_sensitive(tmp_path: Path) -> None:
    a = L.load_grounding()
    assert L.content_hash(a) == L.content_hash(L.load_grounding())
    assert len(L.content_hash(a)) == 64
    text = FILES["grounding"].read_text(encoding="utf-8")
    crlf = tmp_path / "g.jsonl"
    crlf.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    assert L.content_hash(L.load_grounding(crlf)) == L.content_hash(a)
    edited = tmp_path / "g2.jsonl"
    edited.write_text(text.replace("Ilse moved to Utrecht.", "Ilse moved to Delft."), encoding="utf-8")
    assert L.content_hash(L.load_grounding(edited)) != L.content_hash(a)
    hashes = {n: L.content_hash(L.LOADERS[n]()) for n in NAMES}
    assert len(set(hashes.values())) == len(NAMES)


def test_no_gold_deny_list_token_in_any_line() -> None:
    lint = _gold_lint()
    literals = [s for s in lint.KNOWN_GOLD if s.lower() not in lint.ALLOWED]
    bad: list[str] = []
    for name, path in FILES.items():
        for no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            text = " ".join(line.split())
            low = text.lower()
            bad += [f"{name}:{no} gold {s!r}" for s in literals if s.lower() in low]
            bad += [f"{name}:{no} speaker {n!r}" for n in lint.KNOWN_SPEAKERS if re.search(rf"\b{n}\b", text)]
    assert not bad, bad


def test_no_credentials_hostnames_or_urls() -> None:
    pat = re.compile(r"https?://|@[A-Za-z0-9-]+\.[a-z]{2,}|\b(?:sk|pk)-[A-Za-z0-9]{8,}")
    for name, path in FILES.items():
        assert not pat.search(path.read_text(encoding="utf-8")), name
