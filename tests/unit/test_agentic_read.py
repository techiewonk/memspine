"""I67: the opt-in agentic multi-step read (``read.agentic``). Hash embedder and scripted
stub LLMs only; every LLM call is counted by the router."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.agentic import clean_query, evidence_view, fuse_new, merge_budget
from memspine.core.query_shape import is_multi_hop
from memspine.core.records import MemoryRecord
from memspine.engine import search_forensics
from memspine.prompts.models import OUTPUT_MODELS, AgenticStepOut
from memspine.services.decision.decider import TASKS, Decision, HeuristicDecider
from memspine.services.llm import structured
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.services.llm.structured import StructuredOptions

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)

ANSWER_READY = "action: answer_ready\nwhy: the notes are enough"


def search(query: str, why: str = "a fact is missing") -> str:
    return f"action: search\nquery: {query}\nwhy: {why}"


class _Stub:
    """A scripted LLM: replies in order (the last one repeats), every prompt kept."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "stub:agentic"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.prompts.append(messages)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


class _FakeDecider:
    decider_id = "fake"

    def __init__(self, label: str, confidence: float | None = 0.9) -> None:
        self.label = label
        self.confidence = confidence
        self.calls: list[tuple[str, str, str | None]] = []

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        self.calls.append((task, question, context))
        return Decision(self.label, self.confidence, {}, task, self.decider_id)


def _with_roles(monkeypatch: pytest.MonkeyPatch, stubs: dict[str, LLMService]) -> None:
    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


QUESTION = "What do Caroline and Melanie both like?"
TEXTS = [
    "Melanie: I love the pottery class",
    "Melanie: pottery keeps me calm",
    "Caroline: I paint lakes",
    "we slept in a tent trip in the woods",
]


async def _seed(eng: Engine, namespace: str = "a") -> dict[str, str]:
    ids: dict[str, str] = {}
    for i, text in enumerate(TEXTS):
        rec = await eng.write(
            text, namespace=namespace, memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )
        ids[text] = rec.record_id
    return ids


async def _read(eng: Engine, query: str = QUESTION, **kw: Any) -> list[str]:
    out = await eng.read(query, namespace="a", mode="retrieve", top_k=1, budget_tokens=40, **kw)
    return [r.record_id for r in out.context.records]


def _calls(eng: Engine) -> int:
    return eng.model_calls().get("sufficiency", 0)


@pytest.fixture(autouse=True)
def _structured_defaults() -> Iterator[None]:
    yield
    structured.configure(None)


# -- defaults, step 0 ------------------------------------------------------------------


def test_defaults_are_off() -> None:
    read = MemspineConfig().read
    assert read.agentic is False
    assert read.agentic_max_steps == 2
    assert read.agentic_trigger == "multi_hop"
    assert read.decider_tasks == ["list_mode", "bridge_hop"]
    with pytest.raises(ValueError):
        MemspineConfig(read={"agentic_max_steps": 6})


async def test_off_makes_no_call_and_no_forensics(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(search("tent trip"))
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert _calls(eng) == 0 and "agentic_steps" not in stages
    finally:
        await eng.stop()


async def test_answer_ready_equals_todays_read(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(ANSWER_READY)
    _with_roles(monkeypatch, {"sufficiency": stub})
    base, agent = _engine(), _engine(agentic=True, agentic_trigger="always")
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read_ctx(base)).records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read_ctx(agent)).records]
        assert got == want
        assert _calls(agent) == 1
        assert stages["agentic"]["stop"] == "answer_ready"
        assert stages["agentic_steps"][0]["action"] == "answer_ready"
    finally:
        await base.stop()
        await agent.stop()


async def _read_ctx(eng: Engine, query: str = QUESTION, budget: int = 40) -> Any:
    out = await eng.read(query, namespace="a", mode="retrieve", top_k=1, budget_tokens=budget)
    return out.context


# -- search, fusion, stops ---------------------------------------------------------------


async def test_search_adds_new_hits_and_keeps_step0(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(search("tent trip in the woods"), ANSWER_READY)
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic=True, agentic_trigger="always", agentic_top_k=4)
    await eng.start()
    try:
        ids = await _seed(eng)
        plain = _engine()
        await plain.start()
        await _seed(plain)
        base = await _read_ctx(plain, budget=400)  # the same read without the loop
        await plain.stop()
        with search_forensics() as stages:
            ctx = await eng.read(
                QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400
            )
        got = [r.record_id for r in ctx.context.records]
        assert ids[TEXTS[3]] in got
        first = base.records[0].content
        texts = [r.content for r in ctx.context.records]
        assert first in texts
        assert texts.index(first) < texts.index(next(t for t in texts if "tent trip" in t))
        assert len(got) == len(set(got))  # de-duplicated by record id
        row = stages["agentic_steps"][0]
        assert row["action"] == "search" and row["query"] == "tent trip in the woods"
        assert ids[TEXTS[3]] in row["new_ids"] and row["why"] and row["llm_calls"] == 1
        assert row["llm_s"] >= 0 and row["search_s"] >= 0
        assert ids[TEXTS[3]] in stages["agentic"]["new_ids"]
        assert stages["agentic"]["llm_calls"] == 2  # the search and the answer_ready
        assert stages["final"]  # the first pass's own forensics survive the extra search
    finally:
        await eng.stop()


async def test_stops_at_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(search("tent trip"), search("paint lakes"), search("pottery class"))
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic=True, agentic_trigger="always", agentic_max_steps=2, agentic_top_k=1)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400)
        assert _calls(eng) == 2 and len(stages["agentic_steps"]) == 2
        assert stages["agentic"]["stop"] == "max_steps"
    finally:
        await eng.stop()


async def test_stops_when_a_step_finds_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(search("pottery class"), search("never reached"))
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic=True, agentic_trigger="always", agentic_max_steps=3)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read(
                QUESTION, namespace="a", mode="retrieve", top_k=10, budget_tokens=2000
            )
        assert stages["agentic"]["stop"] == "no_new" and _calls(eng) == 1
        assert stages["agentic_steps"][0]["new_ids"] == []
        assert len(out.context.records) == len(TEXTS)
    finally:
        await eng.stop()


async def test_repeated_query_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(search("tent trip"), search("Tent  Trip"))
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic=True, agentic_trigger="always", agentic_max_steps=3)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stages["agentic"]["stop"] in ("repeat", "no_new", "budget")
        assert _calls(eng) <= 2
    finally:
        await eng.stop()


async def test_step0_share_is_never_displaced() -> None:
    mk = lambda text: MemoryRecord(namespace="a", memory_type="episodic", content=text)  # noqa: E731
    step0 = [mk("a" * 40), mk("b" * 40), mk("c" * 40)]  # 11 tokens each
    new = [mk("n" * 40), mk("m" * 40)]
    kept, added, displaced = merge_budget(step0, new, 36, share=0.5, max_new=6)
    assert [r.content[0] for r in kept][:2] == ["a", "b"] or len(kept) >= 1
    assert kept[0] is step0[0]  # the best first-pass hit is protected
    assert displaced and displaced[0] is step0[2]  # last first
    assert added and added[0] is new[0]
    # share 1.0: nothing of step 0 may leave, so nothing is added when full
    kept, added, displaced = merge_budget(step0, new, 33, share=1.0, max_new=6)
    assert kept == step0 and added == [] and displaced == []
    # max_new caps the additions
    _, added, _ = merge_budget(step0[:1], new, 1000, share=0.6, max_new=1)
    assert len(added) == 1


# -- failures fall back to step 0 -------------------------------------------------------


async def test_malformed_reply_falls_back_to_step0(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub("I think we should look elsewhere {{")})
    base, agent = _engine(), _engine(agentic=True, agentic_trigger="always")
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read_ctx(base)).records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read_ctx(agent)).records]
        assert got == want
        assert stages["agentic"]["stop"] == "error"
        assert stages["agentic_steps"][0]["action"] == "error"
    finally:
        await base.stop()
        await agent.stop()


async def test_fenced_reply_is_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "```yaml\n" + search("tent trip in the woods") + "\n```"
    _with_roles(monkeypatch, {"sufficiency": _Stub(reply, ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        ids = await _seed(eng)
        got = [
            r.record_id
            for r in (
                await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400)
            ).context.records
        ]
        assert ids[TEXTS[3]] in got
    finally:
        await eng.stop()


async def test_invalid_action_is_retried_with_the_i69_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub("action: search\nquery:\n", search("tent trip in the woods"), ANSWER_READY)
    _with_roles(monkeypatch, {"sufficiency": stub})
    structured.configure(StructuredOptions(retry_on_error=True))
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        ids = await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read(
                QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400
            )
        assert ids[TEXTS[3]] in [r.record_id for r in out.context.records]
        assert stages["agentic_steps"][0]["llm_calls"] == 2  # the failed reply and its retry
    finally:
        await eng.stop()


async def test_unbound_role_keeps_step0(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stages["agentic"]["stop"] == "unbound" and stages["agentic_steps"] == []
    finally:
        await eng.stop()


# -- safety -----------------------------------------------------------------------------


async def test_the_namespace_cannot_be_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "action: search\nquery: tent trip in the woods\nnamespace: b\nwhy: x"
    _with_roles(monkeypatch, {"sufficiency": _Stub(reply, ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        await _seed(eng, "a")
        other = await eng.write(
            "the other tenant's tent trip secret",
            namespace="b",
            memory_type="episodic",
            valid_from=T0,
        )
        out = await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400)
        assert other.record_id not in [r.record_id for r in out.context.records]
        assert all(r.namespace == "a" for r in out.context.records)
        assert not hasattr(AgenticStepOut(action="answer_ready"), "namespace")
    finally:
        await eng.stop()


async def test_quarantined_records_never_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(search("tent trip in the woods"), ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        ids = await _seed(eng)
        await eng.quarantine(ids[TEXTS[3]], namespace="a")
        out = await eng.read(QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400)
        assert ids[TEXTS[3]] not in [r.record_id for r in out.context.records]
    finally:
        await eng.stop()


async def test_person_time_search_runs_through_the_read_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = "action: search_person_time\nperson: Caroline\ntime: \nwhy: the second person"
    _with_roles(monkeypatch, {"sufficiency": _Stub(reply, ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        ids = await _seed(eng)
        with search_forensics() as stages:
            out = await eng.read(
                QUESTION, namespace="a", mode="retrieve", top_k=1, budget_tokens=400
            )
        row = stages["agentic_steps"][0]
        assert row["action"] == "search_person_time" and row["person"] == "Caroline"
        assert ids[TEXTS[2]] in [r.record_id for r in out.context.records]
    finally:
        await eng.stop()


# -- triggers ----------------------------------------------------------------------------


async def test_multi_hop_trigger_only_fires_on_multi_hop_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(ANSWER_READY)
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic=True, agentic_trigger="multi_hop")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng, "What does Melanie like?")
        assert _calls(eng) == 0 and stages["agentic"]["stop"] == "not_triggered"
        with search_forensics() as stages:
            await _read(eng)
        assert _calls(eng) == 1 and stages["agentic"]["fired"] is True
    finally:
        await eng.stop()


async def test_always_trigger_fires_on_a_plain_question(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="always")
    await eng.start()
    try:
        await _seed(eng)
        await _read(eng, "What does Melanie like?")
        assert _calls(eng) == 1
    finally:
        await eng.stop()


async def test_decider_trigger_follows_the_decider(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(ANSWER_READY)})
    eng = _engine(
        agentic=True,
        agentic_trigger="decider",
        decider="opendecider",
        decider_tasks=["needs_more_evidence"],
    )
    fake = _FakeDecider("needs_more")
    eng.set_decider(fake)
    await eng.start()
    try:
        await _seed(eng)
        await _read(eng, "What does Melanie like?")  # the heuristic says enough; the decider wins
        assert _calls(eng) == 1 and fake.calls[0][0] == "needs_more_evidence"
        fake.label = "enough"
        await _read(eng)  # multi-hop by the heuristic; the decider says enough
        assert _calls(eng) == 1
    finally:
        await eng.stop()


async def test_decider_trigger_without_a_decider_uses_the_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(ANSWER_READY)})
    eng = _engine(agentic=True, agentic_trigger="decider")  # decider: heuristic (default)
    await eng.start()
    try:
        await _seed(eng)
        await _read(eng, "What does Melanie like?")
        assert _calls(eng) == 0
        await _read(eng)
        assert _calls(eng) == 1
    finally:
        await eng.stop()


# -- pure parts ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What do Caroline and Melanie both like?", True),
        ("Did Caroline move before Melanie started pottery?", True),
        ("What is Caroline's sister's job?", True),
        ("Where did Caroline move from 4 years ago?", True),  # bridge cue
        ("What does Melanie like?", False),
        ("When did Melanie go camping in June?", False),
        ("How many pets does she have?", False),
    ],
)
def test_is_multi_hop(question: str, expected: bool) -> None:
    assert is_multi_hop(question) is expected


def test_heuristic_decider_has_the_task() -> None:
    assert "needs_more_evidence" in TASKS
    h = HeuristicDecider()

    async def go() -> tuple[str, str]:
        return (
            (await h.decide("needs_more_evidence", QUESTION)).label,
            (await h.decide("needs_more_evidence", "What does Melanie like?")).label,
        )

    import asyncio

    assert asyncio.run(go()) == ("needs_more", "enough")


def test_agentic_step_validation() -> None:
    assert AgenticStepOut(action="Answer-Ready").action == "answer_ready"
    assert AgenticStepOut(action="done").action == "answer_ready"
    assert AgenticStepOut.model_validate({"action": "search", "query": "x", "why": None}).why == ""
    with pytest.raises(ValidationError):
        AgenticStepOut(action="search")
    with pytest.raises(ValidationError):
        AgenticStepOut(action="search_person_time")
    with pytest.raises(ValidationError):
        AgenticStepOut(action="delete_everything")
    assert AgenticStepOut(action="search_person_time", time="in May 2023").person == ""
    assert OUTPUT_MODELS["AgenticStepOut"] is AgenticStepOut


def test_fuse_new_dedupes_and_ranks() -> None:
    mk = lambda i: MemoryRecord(record_id=i, namespace="a", memory_type="episodic", content=i)  # noqa: E731
    a, b, c, d = (mk(x) for x in "abcd")
    out = fuse_new([[a, b, c], [d, b]], seen={"a"}, rrf_k=60)
    assert [r.record_id for r in out] == ["b", "d", "c"]  # b is in both lists
    assert fuse_new([[a]], {"a"}, 60) == []


def test_view_and_query_are_capped() -> None:
    mk = lambda t: MemoryRecord(namespace="a", memory_type="episodic", content=t)  # noqa: E731
    view = evidence_view([mk("x " * 400)] * 50, max_tokens=60, line_chars=40)
    assert all(len(line) <= 42 for line in view.splitlines()) and len(view.splitlines()) < 50
    assert evidence_view([]) == "(none)"
    assert len(clean_query("q\x00\n" * 500)) <= 200 and "\x00" not in clean_query("a\x00b")
    assert StructuredOptions().retry_on_error is False  # the I69 retry is opt-in as before
