"""Read-path fixes from the 2 Oct 2026 review (R1-1 to R1-12, R2-3 read half, R5-3/7/8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import memspine.engine as engine_module
from memspine import Engine
from memspine.core.policies.assembly import AssembledContext, AssemblyPolicy
from memspine.core.query_shape import is_aggregation, is_ordering
from memspine.core.records import MemoryRecord, SourceInfo

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(read: dict[str, Any] | None = None, **extra: Any) -> Engine:
    memories = extra.pop("memories", {"semantic": {"enabled": True}, "episodic": {"enabled": True}})
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories=memories,
        read={"hybrid": False, **(read or {})},
        **extra,
    )


def _rec(
    content: str,
    *,
    persona: bool = False,
    memory_type: str = "semantic",
    when: datetime = T0,
) -> MemoryRecord:
    source = SourceInfo(channel="persona") if persona else SourceInfo()
    return MemoryRecord(
        namespace="n", memory_type=memory_type, content=content, source=source, valid_from=when
    )


async def _turns(eng: Engine, turns: list[str], start: datetime = T0) -> list[str]:
    ids = []
    for i, text in enumerate(turns):
        rec = await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=start + timedelta(minutes=i)
        )
        ids.append(rec.record_id)
    return ids


# R1-1 ------------------------------------------------------------------------------


async def test_state_view_history_omits_taint_rolled_back_poison() -> None:
    eng = _engine({"current_state_view": True}, memories={"semantic": {"enabled": True}})
    await eng.start()
    try:
        poison = await eng.write(
            "user lives in the city of Zorgville",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0,
        )
        await eng.rollback_taint(poison.record_id, namespace="a")
        await eng.write(
            "user lives in the city of Lyon",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0 + timedelta(days=3),
        )
        ctx = await eng.assemble("which city does the user live in", namespace="a")
        text = "\n".join(r.content for r in ctx.records)
        assert "Lyon" in text
        assert "Zorgville" not in text
    finally:
        await eng.stop()


async def test_state_view_history_keeps_a_genuinely_superseded_fact() -> None:
    eng = _engine({"current_state_view": True}, memories={"semantic": {"enabled": True}})
    await eng.start()
    try:
        for days, city in ((0, "Paris"), (3, "Lyon")):
            await eng.write(
                f"user lives in the city of {city}",
                namespace="a",
                entity="user",
                attribute="city",
                valid_from=T0 + timedelta(days=days),
            )
        ctx = await eng.assemble("which city does the user live in", namespace="a")
        [line] = [r.content for r in ctx.records if "Lyon" in r.content]
        assert (
            "HISTORY (superseded): 2023-05-07 to 2023-05-10: user lives in the city of Paris"
            in (line)
        )
    finally:
        await eng.stop()


# R2-3 (read half) --------------------------------------------------------------------


async def test_state_view_labels_only_the_open_fact_current() -> None:
    """A backfilled (closed but activated) statement renders as history, not CURRENT."""
    eng = _engine({"current_state_view": True}, memories={"semantic": {"enabled": True}})
    await eng.start()
    try:
        await eng.write(
            "user lives in the city of Lyon",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0 + timedelta(days=3),
        )
        await eng.write(
            "user lives in the city of Paris",
            namespace="a",
            entity="user",
            attribute="city",
            valid_from=T0,
        )
        ctx = await eng.assemble("which city does the user live in", namespace="a")
        lines = [r.content for r in ctx.records]
        current = [c for c in lines if c.startswith("CURRENT")]
        assert len(current) == 1 and "Lyon" in current[0]
        assert any(c.startswith("HISTORY (superseded): 2023-05-07") for c in lines)
    finally:
        await eng.stop()


async def test_contested_statement_is_not_a_second_current_line() -> None:
    eng = _engine(
        {"current_state_view": True},
        memories={"semantic": {"enabled": True, "policies": {"conflict": {"contest_ties": True}}}},
    )
    await eng.start()
    try:
        for city in ("Lyon", "Nice", "Lille"):
            await eng.write(
                f"Ana lives in {city}", namespace="a", entity="ana", attribute="city", valid_from=T0
            )
        ctx = await eng.assemble("where does Ana live", namespace="a")
        assert sum(r.content.startswith("CURRENT") for r in ctx.records) == 1
    finally:
        await eng.stop()


# R1-2 ------------------------------------------------------------------------------


async def test_search_gates_before_cutting_to_top_k() -> None:
    eng = _engine(memories={"semantic": {"enabled": True}})
    await eng.start()
    try:
        for i, city in enumerate(["Paris", "Lyon", "Nice", "Lille", "Brest"]):
            await eng.write(
                f"user lives in the city of {city}",
                namespace="a",
                entity="user",
                attribute="city",
                valid_from=T0 + timedelta(days=i),
            )
        for i in range(6):
            await eng.write(f"unrelated note {i} about gardening tomatoes", namespace="a")
        hits = await eng.search("where does the user live city", namespace="a", top_k=4)
        contents = [r.content for r, _ in hits]
        assert "user lives in the city of Brest" in contents
        assert len(hits) == 4
    finally:
        await eng.stop()


# R1-3 ------------------------------------------------------------------------------


async def test_full_read_uses_live_effective_trust() -> None:
    eng = _engine(
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "shared": {"enabled": True},
        },
        integrity={
            "enabled": True,
            "kappa": 0.5,
            "admission_threshold": 0.2,
            "live_reevaluation": True,
        },
    )
    await eng.start()
    try:
        await eng.grant("b", namespace="a")
        src = await eng.write(
            "vpn 809: rotate the certificate", namespace="a", memory_type="episodic"
        )
        note = await eng.write(
            "b note: rotate cert for vpn 809",
            namespace="b",
            memory_type="episodic",
            source=SourceInfo(role="assistant"),
            derived_from=[src.record_id],
        )
        await eng.forget(src.record_id, namespace="a")
        for mode in ("auto", "full"):
            out = await eng.read("rotate cert vpn 809", namespace="b", mode=mode)
            assert all(r.record_id != note.record_id for r in out.context.records)
    finally:
        await eng.stop()


# R1-4 ------------------------------------------------------------------------------


async def test_forgotten_persona_is_not_pinned() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write("bananas are yellow fruit", namespace="a", memory_type="semantic")
        persona = await eng.set_persona("a", "You are a helpful assistant.")
        await eng.forget(persona.record_id, namespace="a")
        ctx = await eng.assemble("bananas", namespace="a")
        assert all(r.source.channel != "persona" for r in ctx.records)
        assert all("helpful assistant" not in r.content for r in ctx.records)
    finally:
        await eng.stop()


# R1-5 ------------------------------------------------------------------------------


async def test_replay_keeps_the_hit_under_a_tight_budget() -> None:
    eng = _engine()
    await eng.start()
    try:
        turns = [f"filler chatter number {i} " + "blah " * 30 for i in range(12)]
        turns[6] = "the dentist appointment moved to thursday"
        ids = await _turns(eng, turns)
        out = await eng.read(
            "when is the dentist appointment",
            namespace="a",
            mode="replay",
            top_k=1,
            budget_tokens=60,
        )
        assert out.mode == "replay"
        assert ids[6] in [r.record_id for r in out.context.records]
        assert out.context.tokens_used <= 60
    finally:
        await eng.stop()


# R1-6 ------------------------------------------------------------------------------


async def test_full_mode_gets_the_dated_render() -> None:
    eng = _engine({"render": "dated"})
    await eng.start()
    try:
        await _turns(eng, ["first turn", "second turn", "third turn"])
        out = await eng.read("anything", namespace="a", mode="auto")
        assert out.mode == "full"
        assert [r.content for r in out.context.records] == [
            "[2023-05-07 Sun] first turn",
            "[2023-05-07 Sun] second turn",
            "[2023-05-07 Sun] third turn",
        ]
    finally:
        await eng.stop()


async def _beach_sessions(eng: Engine) -> list[str]:
    ids = []
    for d in (0, 30, 60):
        day = T0 + timedelta(days=d)
        await _turns(eng, [f"chit chat {i} " + "blah " * 20 for i in range(6)], day)
        rec = await eng.write(
            "Melanie went to the beach with her kids",
            namespace="a",
            memory_type="episodic",
            valid_from=day + timedelta(minutes=30),
        )
        ids.append(rec.record_id)
    return ids


async def test_compose_mode_gets_the_dated_render_and_gap_markers() -> None:
    eng = _engine({"render": "dated", "gap_markers": True})
    await eng.start()
    try:
        await _beach_sessions(eng)
        out = await eng.read(
            "How many times has Melanie gone to the beach?",
            namespace="a",
            mode="auto",
            top_k=2,
            budget_tokens=80,
        )
        assert out.mode == "compose"
        lines = [r.content for r in out.context.records]
        assert lines and all(line.startswith("[") for line in lines)
        assert any("weeks later]" in line or "months later]" in line for line in lines)
    finally:
        await eng.stop()


async def test_compose_mode_abstains_like_assembly() -> None:
    eng = _engine({"assembly": {"theta_abstain": 0.99}})
    await eng.start()
    try:
        await _beach_sessions(eng)
        out = await eng.read(
            "How many times has Melanie gone to the beach?",
            namespace="a",
            mode="compose",
            top_k=2,
            budget_tokens=60,
        )
        assert out.context.abstained and out.context.records == []
    finally:
        await eng.stop()


# R1-7 ------------------------------------------------------------------------------


async def test_reply_reserve_applies_to_every_read_mode() -> None:
    eng = _engine({"reply_reserve_tokens": 500})
    await eng.start()
    try:
        for i in range(5):
            await eng.write(
                f"turn {i} " + "word " * 30,
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(minutes=i),
            )
        for mode in ("auto", "full", "replay", "compose"):
            out = await eng.read("turn", namespace="a", mode=mode, budget_tokens=600)
            assert out.context.tokens_used <= 100, (mode, out.mode)
    finally:
        await eng.stop()


# R1-8 ------------------------------------------------------------------------------


async def test_gap_marker_uses_the_chronological_predecessor() -> None:
    eng = _engine({"render": "dated", "gap_markers": True})
    await eng.start()
    try:
        newer = _rec("went hiking again", memory_type="episodic", when=T0 + timedelta(days=21))
        older = _rec("went hiking", memory_type="episodic", when=T0)
        out = eng._render("hiking", AssembledContext(records=[newer, older]))
        assert out.records[0].content.startswith("[3 weeks later] [2023-05-28")
        assert out.records[1].content.startswith("[2023-05-07")
    finally:
        await eng.stop()


# R1-9 ------------------------------------------------------------------------------


async def test_rerank_gate_judges_the_kept_count_not_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    class FakeReranker:
        reranker_id = "fake"

        async def rerank(self, query: str, documents: list[str]) -> list[float]:
            calls.append(len(documents))
            return [float(i) for i in range(len(documents))]

    eng = _engine({"candidate_pool": 4, "rerank_max_top_k": 3})
    await eng.start()
    try:
        monkeypatch.setattr(eng, "_rerank_provider", lambda: FakeReranker())
        for i in range(10):
            await eng.write(f"note {i} about hiking", namespace="a")
        await eng.assemble("hiking", namespace="a", top_k=2)
        assert calls and calls[0] > 3  # the whole pool is reranked
    finally:
        await eng.stop()


# R1-10 -----------------------------------------------------------------------------


def test_persona_does_not_count_as_evidence_for_abstention() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.5})
    persona = _rec("You are a helpful assistant.", persona=True)
    ctx = policy.assemble([(persona, 1.0), (_rec("bananas are yellow"), 0.1)])
    assert ctx.abstained
    assert [r.content for r in ctx.records] == ["You are a helpful assistant."]


def test_persona_does_not_set_the_relative_floor() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.0, "relative_floor": 0.5})
    persona = _rec("You are a helpful assistant.", persona=True)
    ctx = policy.assemble(
        [(persona, 1.0), (_rec("alpha beta"), 0.4), (_rec("gamma delta"), 0.3)],
        budget_tokens=1000,
    )
    assert {r.content for r in ctx.records} == {
        "You are a helpful assistant.",
        "alpha beta",
        "gamma delta",
    }


async def test_unretrieved_persona_does_not_stop_an_off_topic_query_abstaining() -> None:
    eng = _engine({"assembly": {"theta_abstain": 0.9}})
    await eng.start()
    try:
        for i in range(4):
            await eng.write(f"bananas are yellow fruit number {i}", namespace="a")
        await eng.set_persona("a", "You are a helpful assistant.")
        ctx = await eng.assemble("quantum chromodynamics lattice", namespace="a", top_k=1)
        assert ctx.abstained
        assert all(r.source.channel == "persona" for r in ctx.records)
    finally:
        await eng.stop()


# R1-11 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "How much did John's new car cost?",
        "When did Caroline and Melanie first meet each other?",
        "What does John do every morning?",
        "Which of the books did Melanie like best?",
        "What was the first of the events Caroline attended?",
        "Where did Melanie keep her books?",
    ],
)
def test_single_fact_questions_are_not_aggregation(q: str) -> None:
    assert not is_aggregation(q)


@pytest.mark.parametrize(
    "q",
    [
        "How much money has John spent on games in total?",
        "What kinds of music does Melanie like?",
        "Which events has Caroline attended?",
        "Name each country Caroline has visited.",
    ],
)
def test_aggregation_questions_still_route_to_compose(q: str) -> None:
    assert is_aggregation(q)


@pytest.mark.parametrize(
    "q",
    ["What was the last book Melanie read?", "Where was Melanie's last trip?"],
)
def test_last_is_an_ordering_cue(q: str) -> None:
    assert is_ordering(q)


@pytest.mark.parametrize(
    "q", ["What did Melanie do last weekend?", "Where did John go last summer?"]
)
def test_last_as_a_relative_date_is_not_ordering(q: str) -> None:
    assert not is_ordering(q)


# R1-12 -----------------------------------------------------------------------------


async def test_history_dates_resolve_against_their_own_event_time() -> None:
    eng = _engine(
        {"current_state_view": True, "resolve_relative_dates": True},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        await eng.write(
            "Caroline moved to Boston last Friday",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 1, 14, tzinfo=UTC),
        )
        await eng.write(
            "Caroline moved to Seattle",
            namespace="a",
            entity="caroline",
            attribute="city",
            valid_from=datetime(2023, 7, 15, tzinfo=UTC),
        )
        ctx = await eng.assemble("where does Caroline live", namespace="a")
        [line] = [r.content for r in ctx.records if "Seattle" in r.content]
        assert "Boston last Friday [= Fri 2023-01-13]" in line
    finally:
        await eng.stop()


async def test_replay_segments_only_the_sessions_holding_a_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1-12 / R5-3: no per-member lookups over every session of the namespace."""
    segmented: list[int] = []
    real = engine_module.topic_segments

    def spy(records: list[MemoryRecord]) -> list[list[MemoryRecord]]:
        segmented.append(len(records))
        return real(records)

    monkeypatch.setattr(engine_module, "topic_segments", spy)
    eng = _engine({"replay_topic_segments": True})
    await eng.start()
    try:
        for d in (0, 10, 20):
            await _turns(
                eng, [f"talk {d} {i} " + "blah " * 10 for i in range(5)], T0 + timedelta(days=d)
            )
        await eng.write(
            "the dentist appointment moved to thursday",
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(days=10, minutes=6),
        )
        out = await eng.read(
            "when is the dentist appointment",
            namespace="a",
            mode="replay",
            top_k=1,
            budget_tokens=200,
        )
        assert out.mode == "replay"
        assert len(segmented) == 1
    finally:
        await eng.stop()


# R5-7 ------------------------------------------------------------------------------


def test_latest_slots_respect_dedupe() -> None:
    policy = AssemblyPolicy.bind({"theta_abstain": 0.0, "latest_slots": 3, "dedupe_jaccard": 0.8})
    dups = [
        (_rec("Melanie went hiking on the trail", when=T0 + timedelta(days=d)), 0.5)
        for d in range(3)
    ]
    ctx = policy.assemble(dups, budget_tokens=1000)
    assert len(ctx.records) == 1


# R5-8 ------------------------------------------------------------------------------


async def _compose_with_rrf_k(monkeypatch: pytest.MonkeyPatch, rrf_k: int | None) -> set[str]:
    eng = _engine({"rrf_k": rrf_k}, memories={"semantic": {"enabled": True}})
    await eng.start()
    try:
        recs = {}
        for name in "AXYBZWV":
            recs[name] = await eng.write(f"record {name} {name * 3}", namespace="a")
        query = "what is the thing?"

        async def fake_search(
            probe: str, namespace: str = "a", top_k: int = 8, **_: Any
        ) -> list[tuple[MemoryRecord, float]]:
            order = "AXYB" if probe == query else "ZWVB"
            return [(recs[n], 0.9) for n in order]

        monkeypatch.setattr(eng, "_search", fake_search)  # compose reads via _search (A-1)
        out = await eng.read(query, namespace="a", mode="compose", top_k=1, budget_tokens=500)
        return {r.content for r in out.context.records}
    finally:
        await eng.stop()


async def test_compose_fuses_with_the_configured_rrf_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """B ranks 4th in both probes; A and Z rank 1st in one. At k=60 B wins the fusion,
    at k=1 the first places do."""
    assert "record B BBB" in await _compose_with_rrf_k(monkeypatch, None)
    assert "record B BBB" not in await _compose_with_rrf_k(monkeypatch, 1)
