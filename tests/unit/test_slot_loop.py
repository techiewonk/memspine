"""E02: the missing-slot-driven agentic read (``read.agentic_mode: slot``). Hash embedder and
scripted stub LLMs only; every LLM call is counted by the router."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.query_contract import build_contract
from memspine.core.records import MemoryRecord
from memspine.core.slot_loop import CalcError, calculate, slot_hint, slot_view, step_signature
from memspine.engine import search_forensics
from memspine.prompts.models import SlotStepOut
from memspine.services.llm import structured
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUESTION = "What do Caroline and Melanie both like?"
TEXTS = [
    "Melanie: I love the pottery class",
    "Melanie: pottery keeps me calm",
    "Caroline: I paint lakes",
    "we slept in a tent trip in the woods",
]


def step(action: str, slot: str = "the second thing they like", **kw: str) -> str:
    keys = "".join(f"\n{k}: {v}" for k, v in kw.items())
    return f"action: {action}\nslot: {slot}{keys}\nwhy: needed"


ANSWER = "action: answer\nslot:\nwhy: enough"


class _Stub:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "stub:slot"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.prompts.append(messages)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


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
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        read={
            "hybrid": False,
            "record_access": False,
            "agentic": True,
            "agentic_mode": "slot",
            "agentic_trigger": "always",
            **read,
        },
    )


async def _seed(
    eng: Engine, namespace: str = "a", texts: list[str] | None = None
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for i, text in enumerate(texts or TEXTS):
        rec = await eng.write(
            text, namespace=namespace, memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )
        ids[text] = rec.record_id
    return ids


async def _read(eng: Engine, query: str = QUESTION, budget: int = 400, ns: str = "a") -> Any:
    return await eng.read(query, namespace=ns, mode="retrieve", top_k=1, budget_tokens=budget)


@pytest.fixture(autouse=True)
def _structured_defaults() -> Iterator[None]:
    yield
    structured.configure(None)


# -- pure parts -----------------------------------------------------------------------------


def test_defaults_keep_i67() -> None:
    read = MemspineConfig().read
    assert read.agentic_mode == "query" and read.agentic is False
    assert read.fact_chain == "off" and read.fact_chain_trigger == "multi_hop"
    with pytest.raises(ValueError):
        MemspineConfig(read={"agentic_mode": "free"})


def test_every_action_but_answer_names_the_slot() -> None:
    with pytest.raises(ValidationError):
        SlotStepOut.model_validate({"action": "memory_search", "query": "x"})
    with pytest.raises(ValidationError):
        SlotStepOut.model_validate({"action": "qualified_stop"})
    ok = SlotStepOut.model_validate({"action": "answer"})
    assert ok.slot == ""
    assert SlotStepOut.model_validate({"action": "search", "slot": "s", "query": "q"}).action == (
        "memory_search"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "memory_search", "slot": "s"},
        {"action": "relation_expand", "slot": "s"},
        {"action": "neighbor_lookup", "slot": "s"},
        {"action": "calculate", "slot": "s", "op": "date_diff"},
    ],
)
def test_an_action_lacking_its_argument_is_invalid(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SlotStepOut.model_validate(payload)


def test_slot_view_hands_out_handles_for_the_lines_shown() -> None:
    recs = [
        MemoryRecord(namespace="a", memory_type="episodic", content=f"line {i}") for i in range(3)
    ]
    view, handles = slot_view(recs)
    assert view.splitlines()[0].startswith("[T1] ") and set(handles) == {"T1", "T2", "T3"}
    assert handles["T2"] == recs[1].record_id
    assert slot_view([]) == ("(none)", {})


def test_slot_hint_comes_from_the_contract() -> None:
    hint = slot_hint(build_contract("In which country did Anna live in 2019?"))
    assert "place:country" in hint and "Anna" in hint and "2019" in hint


def test_step_signature_ignores_case_whitespace_slot() -> None:
    assert step_signature("memory_search", query="Tent  Trip") == step_signature(
        "memory_search", query="tent trip"
    )
    assert step_signature("calculate", a="1") != step_signature("memory_search", a="1")


def test_calculate_is_code() -> None:
    assert calculate("date_diff", "2023-05-07", "2023-06-09") == (
        "from 2023-05-07 to 2023-06-09 = 33 days"
    )
    assert "4 weeks and 5 days" in calculate("date_diff", "2023-05-07", "2023-06-09", unit="weeks")
    assert (
        calculate("date_diff", "2023-01-31", "2023-03-01", unit="months")
        == "from 2023-01-31 to 2023-03-01 = 1 months"
    )
    assert "2 years and 3 months" in calculate(
        "date_diff", "2020-01-15", "2022-04-20", unit="years"
    )
    assert "(the second date is earlier)" in calculate("date_diff", "2023-06-09", "2023-05-07")
    assert calculate("date_add", "2023-01-31", amount="1", unit="months").endswith("2023-02-28")
    assert calculate("date_add", "2023-05-07", amount="-10", unit="days").endswith("2023-04-27")
    assert calculate("sum", "12 books", "1,030") == "12 + 1030 = 1042"
    assert calculate("difference", "10", "2.5") == "10 - 2.5 = 7.5"
    # a relative phrase is read by the H1 resolver against the anchor day, never guessed
    assert "2023-07-14" in calculate("date_diff", "last Friday", "2023-07-20", anchor="2023-07-15")


@pytest.mark.parametrize(
    "args",
    [
        ("date_diff", "last Friday", "2023-07-20"),  # relative phrase, no anchor
        ("date_diff", "2023-13-40", "2023-07-20"),
        ("date_diff", "sometime", "2023-07-20"),
        ("sum", "none", "3"),
        ("cube", "2", "3"),
    ],
)
def test_calculate_fails_safe(args: tuple[str, ...]) -> None:
    with pytest.raises(CalcError):
        calculate(*args)


# -- the loop ---------------------------------------------------------------------------------


async def test_off_by_default_is_i67(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub("action: answer_ready\nwhy: ok")
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic_mode="query")
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stages["agentic"]["stop"] == "answer_ready" and "mode" not in stages["agentic"]
        assert "agentic_slot" not in stub.prompts[0][0]["content"]
    finally:
        await eng.stop()


async def test_answer_equals_todays_read_and_the_slot_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(ANSWER)})
    base, agent = _engine(agentic=False), _engine()
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read(agent)).context.records]
        assert got == want
        assert stages["agentic"]["stop"] == "sufficient" and stages["agentic"]["mode"] == "slot"
        assert stages["agentic_steps"][0]["action"] == "answer"
    finally:
        await base.stop()
        await agent.stop()


async def test_memory_search_names_the_slot_and_adds_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(
        step("memory_search", "the place Caroline and Melanie camped", query="tent trip woods"),
        ANSWER,
    )
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic_top_k=4)
    await eng.start()
    try:
        ids = await _seed(eng)
        with search_forensics() as stages:
            out = await _read(eng)
        assert ids[TEXTS[3]] in [r.record_id for r in out.context.records]
        row = stages["agentic_steps"][0]
        assert row["action"] == "memory_search" and row["slot"].startswith("the place Caroline")
        assert row["query"] == "tent trip woods" and ids[TEXTS[3]] in row["new_ids"]
        sent = stub.prompts[0][-1]["content"]  # the contract line and the handles reach the model
        assert "the question asks for:" in sent and "[T1]" in sent
    finally:
        await eng.stop()


async def test_relation_expand_is_one_hop_through_stored_facts_of_this_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(
        step(
            "relation_expand",
            "the country for the home country of Caroline",
            entity="Caroline",
            relation="home country",
        ),
        ANSWER,
    )
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        mine = await eng.write(
            "Caroline home country: Sweden",
            namespace="a",
            memory_type="semantic",
            entity="Caroline",
            attribute="home country",
        )
        other = await eng.write(  # same entity, another namespace: never reachable
            "Caroline home country: Peru",
            namespace="b",
            memory_type="semantic",
            entity="Caroline",
            attribute="home country",
        )
        with search_forensics() as stages:
            out = await _read(eng)
        got = {r.record_id for r in out.context.records}
        assert mine.record_id in got and other.record_id not in got
        assert not any("Peru" in r.content for r in out.context.records)
        row = stages["agentic_steps"][0]
        assert row["action"] == "relation_expand" and row["entity"] == "Caroline"
    finally:
        await eng.stop()


async def test_neighbor_lookup_takes_the_adjacent_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(step("neighbor_lookup", "what came after the pottery line", turn_id="T1"), ANSWER)
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine()
    await eng.start()
    try:
        ids = await _seed(eng)
        with search_forensics() as stages:
            out = await _read(eng)
        first = out.context.records[0]
        order = [ids[t] for t in TEXTS]
        at = order.index(first.record_id)
        adjacent = {order[i] for i in (at - 1, at + 1) if 0 <= i < len(order)}
        row = stages["agentic_steps"][0]
        assert row["action"] == "neighbor_lookup" and set(row["new_ids"]) <= adjacent
        assert row["new_ids"]
        assert set(row["new_ids"]) <= {r.record_id for r in out.context.records}
    finally:
        await eng.stop()


async def test_neighbor_lookup_of_an_unseen_turn_fails_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub(step("neighbor_lookup", turn_id="T99"))})
    base, agent = _engine(agentic=False), _engine()
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read(agent)).context.records]
        assert got == want
        assert stages["agentic"]["stop"] == "no_new"
        assert "handle" in stages["agentic_steps"][0]["error"]
    finally:
        await base.stop()
        await agent.stop()


async def test_calculate_runs_code_and_adds_a_computed_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(
        step(
            "calculate",
            "days between the two trips",
            op="date_diff",
            arg_a="2023-05-07",
            arg_b="2023-06-09",
            unit="days",
        ),
        ANSWER,
    )
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            out = await _read(eng)
        note = out.context.records[-1]
        assert "slot_computed" in note.tags and "= 33 days" in note.content
        assert stages["agentic"]["computed"] == ["from 2023-05-07 to 2023-06-09 = 33 days"]
        # the note is a never-stored block whose provenance is the step-0 evidence
        assert note.source.parents
        for parent in note.source.parents:
            assert await eng._require_started().get_record(parent) is not None
        assert await eng._require_started().get_record(note.record_id) is None  # never stored
        assert stages["agentic_steps"][0]["slot"] == "days between the two trips"
    finally:
        await eng.stop()


async def test_a_calculation_that_cannot_be_done_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = step("calculate", "the gap", op="date_diff", arg_a="last Friday", arg_b="2023-06-09")
    _with_roles(monkeypatch, {"sufficiency": _Stub(bad)})
    base, agent = _engine(agentic=False), _engine()
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read(agent)).context.records]
        assert got == want and stages["agentic"]["stop"] == "no_new"
        assert "calculation failed" in stages["agentic_steps"][0]["error"]
    finally:
        await base.stop()
        await agent.stop()


async def test_qualified_stop_reports_the_slot_and_never_guesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Stub(step("qualified_stop", "the shared hobby of the two women"))
    _with_roles(monkeypatch, {"sufficiency": stub})
    base, agent = _engine(agentic=False), _engine()
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read(agent)).context.records]
        assert got == want
        assert stages["agentic"]["stop"] == "qualified_stop"
        assert stages["agentic"]["unresolved_slot"] == "the shared hobby of the two women"
        assert stages["agentic"]["llm_calls"] == 1
    finally:
        await base.stop()
        await agent.stop()


async def test_a_repeated_action_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    same = step("memory_search", "tent", query="tent trip woods")
    _with_roles(monkeypatch, {"sufficiency": _Stub(same, same.replace("tent trip", "Tent  Trip"))})
    eng = _engine(agentic_max_steps=4, agentic_top_k=1)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stages["agentic"]["stop"] == "repeat" and eng.model_calls()["sufficiency"] == 2
    finally:
        await eng.stop()


async def test_stops_at_max_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(
        step("memory_search", "a", query="tent trip"),
        step("memory_search", "b", query="paint lakes"),
        step("memory_search", "c", query="pottery class"),
    )
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic_max_steps=2, agentic_top_k=1)
    await eng.start()
    try:
        await _seed(eng)
        with search_forensics() as stages:
            await _read(eng)
        assert stages["agentic"]["stop"] in ("max_steps", "no_new", "budget")
        assert eng.model_calls()["sufficiency"] <= 2
    finally:
        await eng.stop()


async def test_a_malformed_reply_keeps_step0(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_roles(monkeypatch, {"sufficiency": _Stub("action: memory_search\nquery: x")})
    base, agent = _engine(agentic=False), _engine()
    await base.start()
    await agent.start()
    try:
        await _seed(base)
        await _seed(agent)
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            got = [r.content for r in (await _read(agent)).context.records]
        assert got == want and stages["agentic"]["stop"] == "error"  # no slot named
    finally:
        await base.stop()
        await agent.stop()


async def test_the_model_cannot_name_a_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Stub(
        step("memory_search", "the other tenant", query="secret plan", namespace="b"), ANSWER
    )
    _with_roles(monkeypatch, {"sufficiency": stub})
    eng = _engine(agentic_top_k=8)
    await eng.start()
    try:
        await _seed(eng)
        secret = await eng.write(
            "Mallory: the secret plan is the tent trip", namespace="b", memory_type="episodic"
        )
        out = await _read(eng)
        assert secret.record_id not in [r.record_id for r in out.context.records]
    finally:
        await eng.stop()
