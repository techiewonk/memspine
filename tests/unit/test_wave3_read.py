"""Wave-3 read items: #36 persons / time leg, #37 search date filters, #38 completeness
check, #39 answer verification, #40 profile-header packing. Hash embedder and stub
LLMs only; every LLM call is counted by the router."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.escaping import escape_markers
from memspine.core.profile_pack import pack_profile, render_packed_profile
from memspine.core.read_filters import (
    DateFilter,
    person_matches,
    person_time_leg,
    time_expr_span,
    to_utc,
)
from memspine.core.records import MemoryRecord
from memspine.exceptions import MissingServiceError
from memspine.prompts.models import AnswerVerdictOut, MissingInfoOut, ReadPlan
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "reflective": {"enabled": True},
        },
        read={"hybrid": False, "record_access": False, **read},
    )


class _Stub:
    """A scripted LLM: replies in order (the last one repeats), every prompt kept."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "stub:wave3"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.prompts.append(messages)
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        return self.replies[index]


def _with_roles(monkeypatch: pytest.MonkeyPatch, stubs: dict[str, LLMService]) -> None:
    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)


def _rec(content: str = "x", **fields: Any) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="semantic", content=content, **fields)


# -- #37: DateFilter ------------------------------------------------------------------


def test_date_filter_is_none_without_bounds() -> None:
    assert DateFilter.build() is None
    assert DateFilter.build(mode="or") is None


def test_date_filter_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="date_filter_mode"):
        DateFilter.build(valid_from_after="2023-05-01", mode="xor")


def test_date_bounds_coerce_to_aware_utc() -> None:
    assert to_utc("2023-05-01") == datetime(2023, 5, 1, tzinfo=UTC)
    assert to_utc("2023-05-01T10:00:00Z") == datetime(2023, 5, 1, 10, tzinfo=UTC)
    assert to_utc(date(2023, 5, 1)) == datetime(2023, 5, 1, tzinfo=UTC)
    assert to_utc(datetime(2023, 5, 1, 9)) == datetime(2023, 5, 1, 9, tzinfo=UTC)
    with pytest.raises(ValueError):
        to_utc("not a date")


def test_after_is_inclusive_and_before_exclusive() -> None:
    may = DateFilter.build(valid_from_after="2023-05-01", valid_from_before="2023-06-01")
    assert may is not None
    assert may.matches(_rec(valid_from=datetime(2023, 5, 1, tzinfo=UTC)))
    assert may.matches(_rec(valid_from=datetime(2023, 5, 31, 23, 59, tzinfo=UTC)))
    assert not may.matches(_rec(valid_from=datetime(2023, 6, 1, tzinfo=UTC)))
    assert not may.matches(_rec(valid_from=datetime(2023, 4, 30, tzinfo=UTC)))


def test_open_valid_to_is_later_than_any_date() -> None:
    still = _rec(valid_to=None)
    ended = _rec(valid_to=datetime(2023, 5, 3, tzinfo=UTC))
    after = DateFilter.build(valid_to_after="2023-05-10")
    before = DateFilter.build(valid_to_before="2023-05-10")
    assert after is not None and before is not None
    assert after.matches(still) and not after.matches(ended)
    assert before.matches(ended) and not before.matches(still)


def test_and_or_modes() -> None:
    record = _rec(
        valid_from=datetime(2023, 5, 5, tzinfo=UTC), recorded_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    both = {"valid_from_after": "2023-05-01", "recorded_before": "2025-01-01"}
    strict = DateFilter.build(**both)
    loose = DateFilter.build(**both, mode="or")
    assert strict is not None and loose is not None
    assert not strict.matches(record)
    assert loose.matches(record)


# -- #37: search / read --------------------------------------------------------------


async def _seed_months(eng: Engine) -> dict[str, str]:
    """Three April notes close to the query, one May note far from it."""
    ids = {}
    for i, text in enumerate(
        [
            "Melanie went to the pottery class",
            "Melanie went to the pottery workshop",
            "Melanie went to the pottery studio",
        ]
    ):
        rec = await eng.write(text, namespace="a", valid_from=datetime(2023, 4, 3 + i, tzinfo=UTC))
        ids[f"april{i}"] = rec.record_id
    rec = await eng.write(
        "a charity race for mental health",
        namespace="a",
        valid_from=datetime(2023, 5, 9, tzinfo=UTC),
    )
    ids["may"] = rec.record_id
    return ids


@pytest.mark.parametrize("hybrid", [False, True])
async def test_search_date_filter_keeps_recall(hybrid: bool) -> None:
    eng = _engine(hybrid=hybrid)
    await eng.start()
    try:
        ids = await _seed_months(eng)
        query = "Melanie pottery"
        [(plain, _)] = await eng.search(query, namespace="a", top_k=1)
        assert plain.record_id != ids["may"]
        # The May note is the least similar of all: a post-hoc cut of the unfiltered
        # top 1 would lose it. The prefilter keeps it.
        hits = await eng.search(
            query,
            namespace="a",
            top_k=1,
            valid_from_after="2023-05-01",
            valid_from_before="2023-06-01",
        )
        assert [r.record_id for r, _ in hits] == [ids["may"]]
        none = await eng.search(query, namespace="a", top_k=3, valid_from_after="2030-01-01")
        assert none == []
    finally:
        await eng.stop()


async def test_search_or_mode_unions_the_bounds() -> None:
    eng = _engine()
    await eng.start()
    try:
        ids = await _seed_months(eng)
        hits = await eng.search(
            "Melanie",
            namespace="a",
            top_k=10,
            valid_from_after="2023-05-01",
            valid_from_before="2023-04-04",
            date_filter_mode="or",
        )
        assert {r.record_id for r, _ in hits} == {ids["april0"], ids["may"]}
    finally:
        await eng.stop()


async def test_search_recorded_bounds() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed_months(eng)
        assert await eng.search("Melanie", namespace="a", recorded_after="2999-01-01") == []
        assert len(await eng.search("Melanie", namespace="a", recorded_before="2999-01-01")) == 4
    finally:
        await eng.stop()


@pytest.mark.parametrize("mode", ["full", "retrieve", "compose", "replay"])
async def test_read_date_filter_every_mode(mode: str) -> None:
    eng = _engine()
    await eng.start()
    try:
        ids = await _seed_months(eng)
        out = await eng.read(
            "Melanie pottery charity",
            namespace="a",
            mode=mode,
            top_k=3,
            valid_from_after=date(2023, 5, 1),
        )
        assert [r.record_id for r in out.context.records] == [ids["may"]]
    finally:
        await eng.stop()


async def test_replay_neighbours_respect_the_filter() -> None:
    eng = _engine()
    await eng.start()
    try:
        turns = []
        for i, text in enumerate(["we talked about pottery", "the charity race", "more pottery"]):
            rec = await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=datetime(2023, 4, 30, 23, 58, tzinfo=UTC) + timedelta(minutes=i),
            )
            turns.append(rec.record_id)
        out = await eng.read(
            "charity race", namespace="a", mode="replay", valid_from_after="2023-05-01"
        )
        ids = [r.record_id for r in out.context.records]
        assert turns[0] not in ids and turns[1] not in ids
        assert ids == [turns[2]] or ids == []
        unfiltered = await eng.read("charity race", namespace="a", mode="replay")
        assert turns[1] in [r.record_id for r in unfiltered.context.records]
    finally:
        await eng.stop()


# -- #36: the persons / time leg ------------------------------------------------------


def test_time_expr_span_absolute_and_relative() -> None:
    anchor = datetime(2023, 6, 9, 12, tzinfo=UTC)  # a Friday
    assert time_expr_span("in May 2023", anchor) == (
        datetime(2023, 5, 1, tzinfo=UTC),
        datetime(2023, 6, 1, tzinfo=UTC),
    )
    assert time_expr_span("last week", anchor, week="preceding_7_days") == (
        datetime(2023, 6, 2, tzinfo=UTC),
        datetime(2023, 6, 9, tzinfo=UTC),
    )
    assert time_expr_span("yesterday", anchor) == (
        datetime(2023, 6, 8, tzinfo=UTC),
        datetime(2023, 6, 9, tzinfo=UTC),
    )
    assert time_expr_span("recently", anchor) is None
    assert time_expr_span(None, anchor) is None
    assert time_expr_span("  ", anchor) is None


def test_person_matches_tags_first_then_entity() -> None:
    tagged = _rec(tags=["person:melanie smith"], entity="Caroline")
    assert person_matches(tagged, ["Melanie"])
    assert not person_matches(tagged, ["Caroline"])  # tags win over the entity
    keyed = _rec(entity="Melanie")
    assert person_matches(keyed, ["melanie"])
    assert not person_matches(keyed, ["Mel"])
    assert not person_matches(_rec(), ["Melanie"])
    assert not person_matches(keyed, [" "])


def test_person_time_leg_ranks_both_first() -> None:
    span = (datetime(2023, 5, 1, tzinfo=UTC), datetime(2023, 6, 1, tzinfo=UTC))
    both = _rec("both", entity="Melanie", valid_from=datetime(2023, 5, 20, tzinfo=UTC))
    in_span = _rec("span", valid_from=datetime(2023, 5, 16, tzinfo=UTC))
    person = _rec("person", entity="Melanie", valid_from=datetime(2023, 8, 1, tzinfo=UTC))
    neither = _rec("neither", valid_from=datetime(2023, 8, 2, tzinfo=UTC))
    records = [neither, person, in_span, both]
    leg = person_time_leg(records, ["Melanie"], span, 10)
    assert [h.record_id for h in leg] == [both.record_id, in_span.record_id, person.record_id]
    assert len(person_time_leg(records, ["Melanie"], span, 2)) == 2
    assert person_time_leg(records, [], None, 10) == []


def test_read_plan_v3_fields() -> None:
    plan = ReadPlan.model_validate(
        {"mode": "lookup", "persons": "Melanie", "time_expr": " in May 2023 "}
    )
    assert plan.persons == ["Melanie"] and plan.time_expr == "in May 2023"
    assert ReadPlan.model_validate({"mode": "lookup", "time_expr": "none"}).time_expr is None
    assert ReadPlan.model_validate({"mode": "lookup"}).persons == []


def test_plan_v3_prompt_is_a_variant() -> None:
    from memspine.prompts.registry import PromptRegistry

    v3 = PromptRegistry().select("plan", condition="v3")
    assert (v3.id, v3.version) == ("plan@v3", 4)
    system = v3.render({"query": "What did Melanie do in May 2023?"})[0]["content"]
    assert "time_expr" in system and "persons" in system


PLAN_V3 = (
    "mode: lookup\ntemporal: true\nentities: [Melanie]\nsubqueries: []\n"
    "persons: [Melanie]\ntime_expr: in May 2023"
)


async def _seed_leg(eng: Engine) -> str:
    for i, text in enumerate(
        ["Melanie talked about the weather", "Melanie talked about the news", "Melanie talked"]
    ):
        await eng.write(text, namespace="a", valid_from=datetime(2023, 4, 3 + i, tzinfo=UTC))
    target = await eng.write(
        "a charity race for mental health",
        namespace="a",
        entity="Melanie",
        valid_from=datetime(2023, 5, 9, tzinfo=UTC),
    )
    return target.record_id


async def _leg_read(eng: Engine) -> list[str]:
    out = await eng.read(
        "What did Melanie talk about in May 2023?", namespace="a", top_k=2, budget_tokens=20
    )
    return [r.record_id for r in out.context.records]


@pytest.mark.parametrize(("version", "fused"), [("v2", False), ("v3", True)])
async def test_planner_v3_fuses_the_person_time_leg(
    monkeypatch: pytest.MonkeyPatch, version: str, fused: bool
) -> None:
    stub = _Stub(PLAN_V3)
    _with_roles(monkeypatch, {"plan": stub})
    eng = _engine(planner="llm", planner_version=version)
    await eng.start()
    try:
        target = await _seed_leg(eng)
        assert (target in await _leg_read(eng)) is fused
        assert eng.model_calls() == {"plan": 1}  # the leg costs no call
        assert ("time_expr" in stub.prompts[0][0]["content"]) is fused
    finally:
        await eng.stop()


async def test_person_time_leg_k_caps_the_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"plan": _Stub(PLAN_V3)})
    eng = _engine(planner="llm", planner_version="v3", person_time_leg_k=1)
    await eng.start()
    try:
        await _seed_leg(eng)
        from memspine.prompts.models import ReadPlan as Plan

        leg = await eng._person_time_leg(
            "a", Plan.model_validate({"mode": "lookup", "persons": ["Melanie"]})
        )
        assert len(leg) == 1
        empty = await eng._person_time_leg("a", Plan.model_validate({"mode": "lookup"}))
        assert empty == []
    finally:
        await eng.stop()


# -- #38: completeness check ----------------------------------------------------------


AGGREGATE = "mode: aggregate\ntemporal: false\nentities: [Melanie]\nsubqueries: [Melanie pottery]"
LOOKUP = "mode: lookup\ntemporal: false\nentities: [Melanie]\nsubqueries: []"
INCOMPLETE = "complete: false\nreason: camping is mentioned but not described"
MISSING = "queries:\n  - tent trip in the woods\n  - Melanie pottery"


async def _seed_activities(eng: Engine) -> str:
    for i, text in enumerate(
        [
            "Melanie: I love the pottery class",
            "Melanie: pottery keeps me calm",
            "Caroline: I paint lakes",
        ]
    ):
        await eng.write(text, namespace="a", valid_from=T0 + timedelta(days=i))
    hidden = await eng.write("we slept in a tent trip in the woods", namespace="a")
    return hidden.record_id


async def _activities(eng: Engine, query: str = "What activities does Melanie do?") -> list[str]:
    out = await eng.read(query, namespace="a", top_k=1, compose_pool=1, budget_tokens=30)
    return [r.record_id for r in out.context.records]


def _sufficiency_calls(eng: Engine) -> int:
    return eng.model_calls().get("sufficiency", 0)


async def test_completeness_runs_missing_queries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Stub(INCOMPLETE, MISSING)
    _with_roles(monkeypatch, {"plan": _Stub(AGGREGATE), "sufficiency": check})
    eng = _engine(planner="llm", completeness_check=True)
    await eng.start()
    try:
        hidden = await _seed_activities(eng)
        assert hidden in await _activities(eng)
        assert _sufficiency_calls(eng) == 2  # one verdict, one missing-info call, one round
        system_one, system_two = (p[0]["content"] for p in check.prompts)
        assert "complete" in system_one and "search queries" in system_two
        assert "Melanie: I love the pottery class" in check.prompts[0][-1]["content"]
    finally:
        await eng.stop()


async def test_completeness_off_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Stub(INCOMPLETE, MISSING)
    _with_roles(monkeypatch, {"plan": _Stub(AGGREGATE), "sufficiency": check})
    eng = _engine(planner="llm")
    await eng.start()
    try:
        hidden = await _seed_activities(eng)
        assert hidden not in await _activities(eng)
        assert _sufficiency_calls(eng) == 0
    finally:
        await eng.stop()


async def test_non_aggregate_read_makes_no_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _Stub(INCOMPLETE, MISSING)
    _with_roles(monkeypatch, {"plan": _Stub(LOOKUP), "sufficiency": check})
    eng = _engine(planner="llm", completeness_check=True)
    await eng.start()
    try:
        await _seed_activities(eng)
        await _activities(eng, "Where does Melanie take a class?")
        assert _sufficiency_calls(eng) == 0
        assert eng.model_calls() == {"plan": 1}
    finally:
        await eng.stop()


async def test_complete_verdict_keeps_the_first_read(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: list[list[str]] = []
    for on in (False, True):
        check = _Stub("complete: true\nreason: all there")
        _with_roles(monkeypatch, {"plan": _Stub(AGGREGATE), "sufficiency": check})
        eng = _engine(planner="llm", completeness_check=on)
        await eng.start()
        try:
            await _seed_activities(eng)
            out = await eng.read(
                "What activities does Melanie do?",
                namespace="a",
                top_k=1,
                compose_pool=1,
                budget_tokens=30,
            )
            outputs.append([r.content for r in out.context.records])
            assert _sufficiency_calls(eng) == (1 if on else 0)
        finally:
            await eng.stop()
    assert outputs[0] == outputs[1]


async def test_rules_count_question_is_checked_with_the_plan_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No planner: a count question routed to compose is checked; the plan role answers."""
    check = _Stub(INCOMPLETE, MISSING)
    _with_roles(monkeypatch, {"plan": check})
    eng = _engine(completeness_check=True)
    await eng.start()
    try:
        hidden = await _seed_activities(eng)
        ids = await _activities(eng, "How many times did Melanie go to pottery?")
        assert eng.model_calls() == {"plan": 2}
        assert hidden in ids
    finally:
        await eng.stop()


async def test_completeness_failure_keeps_the_first_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"plan": _Stub(AGGREGATE), "sufficiency": _Stub("::: not yaml [")})
    eng = _engine(planner="llm", completeness_check=True)
    await eng.start()
    try:
        hidden = await _seed_activities(eng)
        assert hidden not in await _activities(eng)
        assert _sufficiency_calls(eng) == 1
    finally:
        await eng.stop()


async def test_completeness_unbound_role_makes_no_call() -> None:
    eng = _engine(completeness_check=True)
    await eng.start()
    try:
        await _seed_activities(eng)
        await _activities(eng, "How many times did Melanie go to pottery?")
        assert eng.model_calls() == {}
    finally:
        await eng.stop()


def test_missing_info_out_caps_and_cleans() -> None:
    out = MissingInfoOut.model_validate({"queries": ["a", " ", None, "b", "c", "d"]})
    assert out.queries == ["a", "b", "c"]
    assert MissingInfoOut.model_validate({"queries": "one"}).queries == ["one"]


# -- #39: answer verification ----------------------------------------------------------


async def _verifier(
    monkeypatch: pytest.MonkeyPatch, reply: str, role: str = "verify_answer"
) -> tuple[Engine, _Stub]:
    stub = _Stub(reply)
    _with_roles(monkeypatch, {role: stub})
    eng = _engine()
    await eng.start()
    return eng, stub


async def test_verify_answer_maps_evidence_to_record_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    eng, stub = await _verifier(
        monkeypatch, "supported: false\nevidence: [2, 9]\nrevised_answer: Denver"
    )
    try:
        first = await eng.write("Caroline likes tea", namespace="a")
        second = await eng.write("Caroline moved to Denver", namespace="a")
        out = await eng.verify_answer("Where does Caroline live?", "Boston", [first, second])
        assert out == {
            "supported": False,
            "evidence_ids": [second.record_id],
            "revised_answer": "Denver",
        }
        user = stub.prompts[0][-1]["content"]
        assert "[1] Caroline likes tea" in user and "[2] Caroline moved to Denver" in user
        assert "answer: Boston" in user
        assert eng.model_calls() == {"verify_answer": 1}
    finally:
        await eng.stop()


async def test_verify_answer_supported_drops_the_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    eng, _ = await _verifier(monkeypatch, "supported: true\nevidence: [1]\nrevised_answer: Denver")
    try:
        out = await eng.verify_answer("Where?", "Denver", "Caroline moved to Denver\n\nfine")
        assert out == {"supported": True, "evidence_ids": ["L1"], "revised_answer": None}
    finally:
        await eng.stop()


async def test_verify_answer_on_a_read_context_falls_back_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng, _ = await _verifier(
        monkeypatch, "supported: false\nevidence: []\nrevised_answer:", role="chat"
    )
    try:
        await eng.write("Caroline moved to Denver", namespace="a")
        ctx = await eng.assemble("Caroline", namespace="a")
        out = await eng.verify_answer("Where?", "Boston", ctx)
        assert out == {"supported": False, "evidence_ids": [], "revised_answer": None}
        assert eng.model_calls() == {"chat": 1}
    finally:
        await eng.stop()


async def test_verify_answer_needs_a_role() -> None:
    eng = _engine()
    await eng.start()
    try:
        with pytest.raises(MissingServiceError):
            await eng.verify_answer("q", "a", "context")
    finally:
        await eng.stop()


def test_answer_verdict_parsing() -> None:
    out = AnswerVerdictOut.model_validate(
        {"supported": "false", "evidence": "[1], 3", "revised_answer": "none"}
    )
    assert out.supported is False and out.evidence == [1, 3] and out.revised_answer is None


# -- #40: profile-header packing -----------------------------------------------------


def _profile(records: Iterable[MemoryRecord]) -> list[MemoryRecord]:
    return [r for r in records if constants.PROFILE_TAG in r.tags]


async def _seed_profile(eng: Engine) -> dict[str, str]:
    for day in (0, 5):
        for i, text in enumerate(
            [
                "Melanie: I love the pottery class with the kids",
                "Caroline: that sounds great, I paint lakes",
                "Melanie: we also went camping in the woods",
            ]
        ):
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(days=day, minutes=i),
            )
    await eng.sleep()  # two session summaries (extractive)
    turns = await eng.retrieve(namespace="a", memory_type="episodic")
    insight = await eng._deposit_profile_reflection(
        "a", "Melanie loves pottery and family camping", [turns[0].record_id], "s1"
    )
    return {"insight": insight.record_id}


async def test_packed_profile_header_sections_in_order() -> None:
    eng = _engine(profile_header_packing=True, profile_header_budget=200)
    await eng.start()
    try:
        ids = await _seed_profile(eng)
        out = await eng.read(
            "What does Melanie like, pottery?", namespace="a", mode="retrieve", budget_tokens=600
        )
        [header] = _profile(out.context.records)
        lines = header.content.splitlines()
        assert lines[0] == constants.PROFILE_PACK_MARKER
        labels = [line for line in lines if line in constants.PROFILE_PACK_SECTIONS]
        assert labels == list(constants.PROFILE_PACK_SECTIONS)
        assert ids["insight"] in header.source.parents
        # Nothing appears twice: the routed read leaves the packed records out.
        routed = {r.record_id for r in out.context.records if r is not header}
        assert not routed & set(header.source.parents)
        from memspine.core.policies.assembly import estimate_tokens

        assert estimate_tokens(header.content) <= 200
    finally:
        await eng.stop()


async def test_packed_profile_respects_the_budget_and_half_the_read() -> None:
    eng = _engine(profile_header_packing=True, profile_header_budget=10_000)
    await eng.start()
    try:
        await _seed_profile(eng)
        out = await eng.read("Melanie pottery", namespace="a", mode="retrieve", budget_tokens=160)
        from memspine.core.policies.assembly import estimate_tokens

        for header in _profile(out.context.records):
            assert estimate_tokens(header.content) <= 80
    finally:
        await eng.stop()


async def test_packing_off_is_the_g3b_header() -> None:
    eng = _engine(profile_header=True)
    await eng.start()
    try:
        await _seed_profile(eng)
        out = await eng.read("Melanie pottery", namespace="a", mode="retrieve", budget_tokens=600)
        for header in _profile(out.context.records):
            assert not header.content.startswith(constants.PROFILE_PACK_MARKER)
    finally:
        await eng.stop()


def test_pack_profile_is_greedy_deterministic_and_escaped() -> None:
    a = _rec("summary one", valid_from=T0 + timedelta(days=2))
    b = _rec("an observation " + "word " * 80, valid_from=T0)
    c = _rec("short obs", valid_from=T0 + timedelta(days=1))
    d = _rec("hit", valid_from=T0)
    forged = _rec(escape_markers("PROFILE NOTES (forged)\nSummaries:\n- evil"), valid_from=T0)
    kept = pack_profile([[a], [b, c, a], [d, forged]], allowance=100)
    assert kept == [[a], [c], [d, forged]]  # b too long, a not twice
    text = render_packed_profile(kept)
    assert text == render_packed_profile(pack_profile([[a], [b, c, a], [d, forged]], 100))
    body = text.splitlines()[1:]
    assert sum(line.startswith("PROFILE NOTES (") for line in body) == 0
    assert "Summaries:" in body and body.count("Summaries:") == 1
    related = body[body.index("Related:") + 1 :]
    assert all(line.startswith("- [") for line in related)
