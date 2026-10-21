"""W2-13 (P-23, ``write.prepass``): the local pre-pass as hints. Model-free: stub window LLM,
hash embedder, invented names only. GLiNER2 is not installed in CI; its adapter test is skipped
with a reason and a stub finder exercises the same code path."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_w2_06a_envelope import ItemLLM, _item
from test_w2_06a_envelope import _engine as env_engine
from test_window_write import FakeWindowLLM, _facts, _send
from test_window_write import _engine as legacy_engine

from memspine.config.schema import MemspineConfig
from memspine.derive.compile import WindowLine
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.envelope_stages import EnvelopeStageContext
from memspine.derive.gliner_mentions import MentionSpan
from memspine.derive.items import ItemValidator
from memspine.derive.prepass import (
    GlinerHints,
    Hints,
    Prepass,
    prepass_hints,
    prepass_stage,
    rule_hints,
)
from memspine.schemas.durable import OwnerScope
from memspine.schemas.durable.assertion import Mention
from memspine.store.records import MemoryRecord, SourceInfo

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
    assert (
        MemspineConfig(
            runtime={
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            }
        ).write.prepass
        == "off"
    )
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
            [Rec(t["content"], f"w{i}-{k}") for k, t in enumerate(window(i, fact=fact))],
            namespace="a",
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
        return out


def test_gliner_hints_only_add_and_can_veto_a_skip() -> None:
    turns = recs("Ilse: ok, kestrel works next harvest".replace("kestrel works", "Kestrel Works"))
    base = prepass_hints(turns, mode="rules")
    assert not base.nothing_extractable  # capitalised name already blocks the skip
    h = prepass_hints(turns, mode="gliner", gliner=StubGliner())
    assert {s.text for s in h.entities} >= {"Kestrel Works"}
    assert any(s.text == "next harvest" and s.source == "gliner" for s in h.dates)
    # gliner mode without a finder is the rules
    assert prepass_hints(turns, mode="gliner").entities == base.entities


def test_sync_finder_drops_entire_malformed_result() -> None:
    class InvalidFinder:
        def entities(self, text: str) -> list[tuple[str, str]]:
            return [("Ilse", "person"), ("not in source", "person")]

    text = "Ilse met Noor"
    hints = prepass_hints(recs(text), mode="gliner", gliner=InvalidFinder())
    assert not any(span.source == "gliner" for span in (*hints.entities, *hints.dates))


def test_gliner_unavailable_falls_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GlinerHints, "available", staticmethod(lambda: False))
    p = Prepass("gliner")
    assert not p.uses_gliner and p.hints(recs("Hi!"), namespace="a").nothing_extractable


async def test_async_hint_cache_is_scoped_by_namespace() -> None:
    class Finder:
        producer_id = "test-gliner-v1"

        def __init__(self) -> None:
            self.calls = 0

        async def aentities(self, text: str) -> list[tuple[str, str]]:
            self.calls += 1
            return (MentionSpan(text="Mira", label="person", start=0, end=4),)

    finder = Finder()
    prepass = Prepass("gliner", gliner=finder)
    records = [types.SimpleNamespace(record_id="same-id", content="Mira")]
    await prepass.ahints(records, namespace="tenant-a")
    await prepass.ahints(records, namespace="tenant-b")
    await prepass.ahints(records, namespace="tenant-a")
    assert finder.calls == 2


async def test_async_hint_cache_tracks_record_snapshot_and_sender() -> None:
    class Finder:
        producer_id = "snapshot-cache-test"

        def __init__(self) -> None:
            self.calls = 0

        async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
            self.calls += 1
            return (MentionSpan(text="Mira", label="person", start=0, end=4),)

    finder = Finder()
    prepass = Prepass("gliner", gliner=finder)

    def record(content: str, sender: str) -> Any:
        return types.SimpleNamespace(
            record_id="reused-id",
            version=1,
            namespace="tenant-a",
            content=content,
            source=types.SimpleNamespace(
                role="user",
                channel="chat",
                message_id="message-1",
                parents=(),
                message={"message_id": "message-1", "sender": {"id": sender}},
            ),
            axes=types.SimpleNamespace(speaker="Mira"),
        )

    await prepass.ahints([record("Mira likes tea", "sender-a")], namespace="tenant-a")
    await prepass.ahints([record("Mira likes coffee", "sender-a")], namespace="tenant-a")
    await prepass.ahints([record("Mira likes coffee", "sender-b")], namespace="tenant-a")
    assert finder.calls == 3


async def test_gliner_hints_uses_shared_settings_and_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.config.schema.llm import GlinerSettings
    from memspine.models.providers.decision.gliner2_decision import DEFAULT_MODEL
    from memspine.models.runtime.gliner_runtime import GLiNERModelPool

    loaded: list[str] = []

    class _BoundaryExtractor:
        def extract_entities(
            self,
            text: str,
            labels: list[str],
            *,
            include_confidence: bool = False,
            include_spans: bool = False,
        ) -> dict[str, Any]:
            assert include_confidence and include_spans
            return {"entities": {"person": [{"text": "Mira", "start": 0, "end": 4}]}}

    module = types.ModuleType("gliner2")
    module.AutoExtractor = _BoundaryExtractor
    # A legacy class may be present in the SDK but must not be selected.
    module.GLiNER2 = object()
    monkeypatch.setitem(sys.modules, "gliner2", module)

    settings = GlinerSettings()
    pool = GLiNERModelPool(
        loader=lambda effective: (loaded.append(effective.model), _BoundaryExtractor())[1]
    )
    finder = GlinerHints(pool, settings)
    assert await finder.aentities("Mira") == (
        MentionSpan(text="Mira", label="person", start=0, end=4),
    )
    assert await finder.aentities("Mira") == (
        MentionSpan(text="Mira", label="person", start=0, end=4),
    )
    assert loaded == [DEFAULT_MODEL]
    await pool.aclose()


async def test_prepass_stage_binds_mentions_only_to_cited_source_and_roundtrips() -> None:
    from memspine.derive.gliner_mentions import MentionSpan

    content = "Mira met Mira near 東京."
    record = MemoryRecord(
        record_id="turn-1",
        namespace="default",
        memory_type="episodic",
        content=content,
        version=3,
        source=SourceInfo(
            role="user",
            channel="chat",
            message={
                "message_id": "message-7",
                "role": "user",
                "sender": {"id": "principal-2", "kind": "user"},
                "created_at": "2023-06-01T09:00:00+00:00",
            },
        ),
    )

    class Finder:
        producer_id = "source-bound-test"

        async def aentities(self, text: str) -> tuple[MentionSpan, ...]:
            assert text == content
            return (
                MentionSpan(text="Mira", label="person", start=0, end=4, confidence=0.91),
                MentionSpan(text="Mira", label="person", start=9, end=13, confidence=0.84),
                MentionSpan(text="東京", label="place", start=19, end=21, confidence=0.7),
            )

    compiler = EnvelopeCompiler(clock=lambda: T0)
    line = WindowLine(
        turn_id=record.record_id,
        source_id=record.record_id,
        source_version=record.version,
        seg_index=1,
        role="user",
        speaker="Mira",
        source_at=T0,
        tz="UTC",
        content=content,
    )
    compile_ctx = compiler.context(
        OwnerScope(tenant="default", owner="default"), "window-1", {1: line}, source_time=T0
    )
    compiled = ItemValidator(compiler).validate(
        [_item(text="Mira met Mira near Tokyo", span="Mira met Mira")], compile_ctx
    )
    assert len(compiled.envelopes) == 1
    original = compiled.envelopes[0]
    trust = original.assessment.source_trust
    evidence = [e.model_dump(mode="json") for e in original.evidence]
    # Exercise system-stage scrubbing too; normal model output is rejected at compile time.
    spoofed = original.model_copy(
        update={"extras": {"gliner_mentions": [{"text": "fake", "asserted": True}]}}
    )
    prepass = Prepass("gliner", gliner=Finder())
    stage = prepass_stage(prepass)
    stage_ctx = EnvelopeStageContext(
        namespace="default",
        window_id="window-1",
        lines={1: line},
        records=[record],
        compile_ctx=compile_ctx,
    )
    (annotated,) = await stage.run([spoofed], stage_ctx)
    annotations = annotated.extras["gliner_mentions"]
    assert [item["text"] for item in annotations] == ["Mira", "Mira", "東京"]
    assert [(item["start"], item["end"]) for item in annotations] == [(0, 4), (9, 13), (19, 21)]
    assert [item["confidence"] for item in annotations] == [0.91, 0.84, 0.7]
    assert all(item["asserted"] is False for item in annotations)
    assert all(item["source_record_id"] == "turn-1" for item in annotations)
    assert all(item["source_version"] == 3 for item in annotations)
    assert all(item["source_namespace"] == "default" for item in annotations)
    assert all(item["sender_id"] == "principal-2" for item in annotations)
    assert all(item["source_message_id"] == "message-7" for item in annotations)
    assert annotated.assessment.source_trust == trust
    assert [e.model_dump(mode="json") for e in annotated.evidence] == evidence

    # The durable read path validates the system-owned annotation after storage/reopen.
    from memspine.schemas.durable.assertion import AssertionEnvelope

    reloaded = AssertionEnvelope.model_validate(annotated.model_dump(mode="json"))
    assert reloaded.extras["gliner_mentions"] == annotations
    forged = annotated.model_dump(mode="json")
    forged["extras"]["gliner_mentions"][0]["source_namespace"] = "other-tenant"
    with pytest.raises(ValueError, match="namespace differs"):
        AssertionEnvelope.model_validate(forged)
    forged = annotated.model_dump(mode="json")
    forged["extras"]["gliner_mentions"][0]["source_version"] = 2
    with pytest.raises(ValueError, match="source is not cited"):
        AssertionEnvelope.model_validate(forged)


@pytest.mark.skip(reason="real model inference is covered by the explicit offline smoke tool")
async def test_gliner_adapter_real_model() -> None:  # pragma: no cover - requires local weights
    raise AssertionError("real GLiNER inference is not a unit test")
