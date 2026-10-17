"""W2-13 (P-23, ``write.prepass``): the local pre-pass as hints. Model-free: stub window LLM,
hash embedder, invented names only. GLiNER2 is not installed in CI; its adapter test is skipped
with a reason and a stub finder exercises the same code path."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_w2_06a_envelope import ItemLLM
from test_w2_06a_envelope import _engine as env_engine
from test_window_write import FakeWindowLLM, _facts, _send
from test_window_write import _engine as legacy_engine

from memspine.config.schema import MemspineConfig
from memspine.derive.envelope_stages import EnvelopeStageContext
from memspine.derive.prepass import (
    GlinerHints,
    Hints,
    Prepass,
    prepass_hints,
    prepass_stage,
    rule_hints,
)
from memspine.schemas.durable.assertion import Mention

T0 = datetime(2023, 6, 1, 9, 0, tzinfo=UTC)
NAMES = ["Ilse", "Bram", "Noor", "Tomasz"]
PLACES = ["Vorstad", "Kelmark", "Oosterhaven", "Lindgaard"]
FILLER = [
    "Hi!",
    "Hello there",
    "thanks",
    "ok",
    "Sounds good",
    "lol",
    "Good morning",
    "See you later",
    "Thank you so much",
    "yeah sure",
    "How are you?",
    "Great, got it",
]
FACTS = [
    "I moved to {p} last spring",
    "My brother {n} works at the {p} harbour office",
    "{n} has three cats and a dog",
    "I switched to a standing desk after my back problems",
    "We booked the flight on 14 March for {n}",
    "I prefer green tea over coffee",
    "I am allergic to peanuts",
    "{n} and I are meeting at {p}",
]


def window(i: int, *, fact: bool) -> list[dict[str, str]]:
    """Two or three turns. The fact window has at least one real statement (some with no name,
    date or number at all); the filler window is greetings only."""
    who = NAMES[i % 4]
    if fact:
        text = FACTS[i % len(FACTS)].format(n=NAMES[(i + 1) % 4], p=PLACES[i % 4])
        turns = [FILLER[i % len(FILLER)], text]
    else:
        turns = [FILLER[i % len(FILLER)], FILLER[(i + 5) % len(FILLER)]]
    return [
        {
            "role": "user",
            "content": f"{who}: {t}",
            "timestamp": (T0 + timedelta(days=i, minutes=k)).isoformat(),
        }
        for k, t in enumerate(turns)
    ]


class Rec:
    def __init__(self, content: str, rid: str) -> None:
        self.content = content
        self.record_id = rid


def recs(*texts: str) -> list[Rec]:
    return [Rec(t, f"r{i}") for i, t in enumerate(texts)]


# --- config / hints shape -----------------------------------------------------------------------
def test_flag_default_off_and_literal() -> None:
    assert MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).write.prepass == "off"
    with pytest.raises(ValueError):
        MemspineConfig(write={"prepass": "yes"})


def test_off_returns_empty_non_skipping_hints() -> None:
    h = prepass_hints(recs("Hi!"), mode="off")
    assert h.mode == "off" and not h.entities and h.nothing_extractable is False


def test_hints_shape_names_dates_numbers_known() -> None:
    text = "Ilse: I met Bram Koster in Vorstad on 14 March for 3 hours, Noor came yesterday"
    h = prepass_hints(recs(text), mode="rules", known_aliases=["noor"])
    assert isinstance(h, Hints) and h.turns == 1 and not h.nothing_extractable
    surfaces = [s.text for s in h.entities]
    assert "Bram Koster" in surfaces and "Vorstad" in surfaces and "Noor" in surfaces
    assert h.known == ("Noor",)
    assert any("14 March" in s.text for s in h.dates) and any(
        s.text == "yesterday" for s in h.dates
    )
    assert any(s.text == "3" for s in h.numbers)
    # spans index into the turn content, on the right line
    for s in h.entities + h.dates + h.numbers:
        assert text[s.start : s.end] == s.text and s.line == 1
    lines = h.prompt_lines()
    assert lines[0] == "KNOWN: Noor" and lines[1].startswith("NEW: ") and "Noor" not in lines[1]
    assert lines[2].startswith("DATES: ")
    d = h.to_dict()
    assert d["nothing_extractable"] is False and "Bram Koster" in d["entities"]
    assert h.names() >= {"bram", "koster", "bram koster", "vorstad"}


def test_speaker_prefix_and_sentence_openers_are_not_names() -> None:
    h = rule_hints(recs("Ilse: Hello, thanks"))
    assert h.entities == () and h.nothing_extractable
    assert h.filler_lines == (1,)


def test_filler_needs_every_word_in_the_closed_vocabulary() -> None:
    assert rule_hints(recs("Bram: ok thanks")).nothing_extractable
    assert not rule_hints(recs("Bram: ok thanks, I quit my job")).nothing_extractable
    assert not rule_hints(recs("Bram: ok 42")).nothing_extractable
    assert not rule_hints(recs("Bram: ठीक है")).nothing_extractable  # non-Latin text is kept
    assert not rule_hints(recs("Bram: hi", "Bram: I adopted a dog")).nothing_extractable


# --- the skip ------------------------------------------------------------------------------------
async def _run_stub(
    monkeypatch: pytest.MonkeyPatch, prepass: str
) -> tuple[FakeWindowLLM, dict[str, Any], list[str]]:
    llm = FakeWindowLLM()
    eng = await legacy_engine(monkeypatch, llm, window_max_turns=3, prepass=prepass)
    try:
        for i in range(100):
            await _send(eng, "a", f"s{i}", window(i, fact=i % 2 == 0))
        stats = await eng.flush_windows()
        deposited = sorted(r.content for r in await _facts(eng))
    finally:
        await eng.stop()
    return llm, stats, deposited


async def test_flag_off_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    llm, stats, _ = await _run_stub(monkeypatch, "off")
    assert "prepass" not in stats and "windows_prepass_skipped" not in stats
    assert stats["calls"] == len(llm.calls) == 100


async def test_skip_cuts_stub_calls_and_loses_no_fact_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, base, base_facts = await _run_stub(monkeypatch, "off")
    llm, stats, facts = await _run_stub(monkeypatch, "rules")
    fact_windows = 50
    # every window with a real statement still reaches the model; the filler ones do not
    assert stats["windows_prepass_checked"] == 100
    assert stats["windows_prepass_skipped"] == 50
    assert stats["calls"] == base["calls"] - 50 == len(llm.calls) == 50
    called = {t for t in llm.transcripts}
    assert len(called) == fact_windows
    # grounded facts: every fact whose source turn is a real statement is deposited in both runs;
    # the only facts lost are those of the greeting turns (the stub invents one per turn)
    assert stats["turns_prepass_skipped"] == 100
    real = [f for f in base_facts if not any(f.endswith(x) for x in FILLER)]
    assert real and all(f in facts for f in real)
    # every window with a real statement was sent to the model, in full
    seen = " ".join(llm.transcripts)
    for i in range(0, 100, 2):
        sentence = FACTS[i % len(FACTS)].format(n=NAMES[(i + 1) % 4], p=PLACES[i % 4])
        assert sentence in seen, i
    assert set(facts) <= set(base_facts)
    print(
        f"W2-13 calls off={base['calls']} rules={stats['calls']} "
        f"skipped_windows={stats['windows_prepass_skipped']} facts off={len(base_facts)} rules={len(facts)} real_off={len(real)}"
    )


async def test_no_real_fact_window_is_skipped_100_windows() -> None:
    """100 synthetic windows with invented names, half of them with a real fact (some with no
    name, date or number). The hints never mark a fact window as nothing-extractable."""
    skipped_fact, kept_filler = [], []
    for i in range(100):
        fact = i % 2 == 0
        h = Prepass("rules").hints(
            [Rec(t["content"], f"w{i}-{k}") for k, t in enumerate(window(i, fact=fact))]
        )
        if fact and h.nothing_extractable:
            skipped_fact.append(i)
        if not fact and not h.nothing_extractable:
            kept_filler.append(i)
    assert skipped_fact == []
    assert kept_filler == []


async def test_episode_section_is_never_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await legacy_engine(monkeypatch, llm, mode="all", prepass="rules")
    try:
        await _send(eng, "a", "s1", window(1, fact=False))
        stats = await eng.flush_windows()
        assert stats["windows_prepass_skipped"] == 0 and stats["calls"] == 1
    finally:
        await eng.stop()


# --- the envelope stage --------------------------------------------------------------------------
async def test_stage_flags_unseen_names_and_never_adds_or_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ItemLLM()
    # the model names "Ilse" (seen) in every item; one item is rewritten to name "Zorvak" (never seen)
    llm.mutations[1] = lambda it: {**it, "s": "Zorvak"}
    eng = await env_engine(monkeypatch, llm, prepass="rules")
    try:
        await _send(
            eng,
            "a",
            "s1",
            [
                {
                    "role": "user",
                    "content": f"Ilse: I planted {x} bulbs",
                    "timestamp": (T0 + timedelta(minutes=k)).isoformat(),
                }
                for k, x in enumerate(["red", "blue", "white"])
            ],
        )
        stats = await eng.flush_windows()
        assert stats["envelope_v2"] and stats["prepass"] == "rules"
        facts = await _facts(eng)
        assert len(facts) == 3  # nothing added, nothing dropped
        assert stats["windows_prepass_skipped"] == 0
    finally:
        await eng.stop()


async def test_stage_direct_flags_only() -> None:
    from memspine.derive.compile import WindowLine
    from memspine.derive.envelope_compiler import EnvelopeCompiler
    from memspine.derive.items import ItemValidator
    from memspine.schemas.durable import OwnerScope

    compiler = EnvelopeCompiler(clock=lambda: T0)
    lines = {
        1: WindowLine(
            turn_id="t1",
            source_id="t1",
            source_version=1,
            seg_index=1,
            role="user",
            speaker="Ilse",
            source_at=T0,
            tz="UTC",
            content="Ilse: I met Bram",
        )
    }
    ctx = compiler.context(OwnerScope(tenant="default", owner="a"), "w1", lines)
    validator = ItemValidator(compiler)
    items = [
        {
            "s": "Ilse",
            "r": "met",
            "o": "Bram",
            "text": "Ilse met Bram",
            "kind": "event",
            "mode": "asserted",
            "neg": False,
            "lines": [1],
            "span": "met Bram",
        },
        {
            "s": "Zorvak",
            "r": "met",
            "o": "Bram",
            "text": "Zorvak met Bram",
            "kind": "event",
            "mode": "asserted",
            "neg": False,
            "lines": [1],
            "span": "met Bram",
        },
    ]
    envs = list(validator.validate(items, ctx).envelopes)
    assert len(envs) == 2
    pre = Prepass("rules")
    stage = prepass_stage(pre)
    assert stage.order == 500
    sctx = EnvelopeStageContext(
        namespace="a",
        window_id="w1",
        lines=lines,
        records=recs("Ilse: I met Bram"),
        compile_ctx=ctx,
    )
    out = await stage.run(envs, sctx)
    assert len(out) == len(envs)
    flagged = [e for e in out if e.extras.get("prepass_unseen")]
    assert [e.subject.surface for e in flagged if e.subject] == ["Zorvak"]
    assert sctx.notes["prepass_unseen_entity"] == 1
    assert all(isinstance(m, Mention) or m is None for e in out for m in (e.subject, e.object))


# --- GLiNER2 -------------------------------------------------------------------------------------
class StubGliner:
    def entities(self, text: str) -> list[tuple[str, str]]:
        out = []
        if "Kestrel Works" in text:
            out.append(("Kestrel Works", "organization"))
        if "next harvest" in text:
            out.append(("next harvest", "date"))
        out.append(("not in the text", "person"))  # an invented span is dropped
        return out


def test_gliner_hints_only_add_and_can_veto_a_skip() -> None:
    turns = recs("Ilse: ok, kestrel works next harvest".replace("kestrel works", "Kestrel Works"))
    base = prepass_hints(turns, mode="rules")
    assert not base.nothing_extractable  # capitalised name already blocks the skip
    h = prepass_hints(turns, mode="gliner", gliner=StubGliner())
    assert {s.text for s in h.entities} >= {"Kestrel Works"}
    assert any(s.text == "next harvest" and s.source == "gliner" for s in h.dates)
    assert "not in the text" not in {s.text for s in h.entities}
    # gliner mode without a finder is the rules
    assert prepass_hints(turns, mode="gliner").entities == base.entities


def test_gliner_unavailable_falls_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GlinerHints, "available", staticmethod(lambda: False))
    p = Prepass("gliner")
    assert not p.uses_gliner and p.hints(recs("Hi!")).nothing_extractable


@pytest.mark.skipif(not GlinerHints.available(), reason="gliner2 ([ner] extra) is not installed")
def test_gliner_adapter_real_model() -> None:  # pragma: no cover - needs the model cached
    out = GlinerHints().entities("Ilse works at Kestrel Works in Vorstad")
    assert out and all(isinstance(s, str) and isinstance(label, str) for s, label in out)
