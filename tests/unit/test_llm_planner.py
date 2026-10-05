"""G2a: the LLM read planner (``read.planner: llm``) over a real router with stub providers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.prompts.models import ReadPlan
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
FILLER = " and then we talked for a long while about many other unrelated things" * 3


class _Plan:
    """A stub ``plan`` provider: a canned reply, or an error when ``reply`` is None."""

    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "stub:plan"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("provider down")
        return self.reply


def _engine(monkeypatch: pytest.MonkeyPatch, stub: _Plan | None, **read: Any) -> Engine:
    stubs: dict[str, LLMService] = {"plan": stub} if stub is not None else {}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, "planner": "llm", **read},
    )


async def _seed(eng: Engine) -> None:
    lines = [
        "Ana went running in the park" + FILLER,
        "Ana adopted a grey cat called Miso" + FILLER,
        "Ana signed up for pottery classes" + FILLER,
        "Ana ran a charity race in June" + FILLER,
    ]
    for i, text in enumerate(lines):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )


def _plan(mode: str, subqueries: list[str] | None = None) -> str:
    subs = "".join(f"\n  - {s}" for s in subqueries or [])
    return f"mode: {mode}\ntemporal: false\nentities: [Ana]\nsubqueries:{subs or ' []'}"


@pytest.mark.parametrize(
    ("plan_mode", "expected"),
    [("lookup", "replay"), ("replay", "replay"), ("aggregate", "compose")],
)
async def test_plan_routes_the_read(
    monkeypatch: pytest.MonkeyPatch, plan_mode: str, expected: str
) -> None:
    stub = _Plan(_plan(plan_mode))
    eng = _engine(monkeypatch, stub)
    await eng.start()
    try:
        await _seed(eng)
        # A lookup-shaped question: the rules alone would replay, never compose.
        out = await eng.read("what pet does Ana have", namespace="a", top_k=2, budget_tokens=120)
        assert out.mode == expected
        assert stub.calls == 1
        assert eng.model_calls() == {"plan": 1}  # counted through the router
    finally:
        await eng.stop()


@pytest.mark.parametrize("reply", [None, "mode: banana\n", "not: [valid"])
async def test_failure_or_invalid_plan_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch, reply: str | None
) -> None:
    eng = _engine(monkeypatch, _Plan(reply))
    await eng.start()
    try:
        await _seed(eng)
        # Aggregation-shaped: the rules route it to compose.
        out = await eng.read(
            "how many times did Ana go running", namespace="a", top_k=2, budget_tokens=120
        )
        assert out.mode == "compose"
        out = await eng.read("what pet does Ana have", namespace="a", top_k=2, budget_tokens=120)
        assert out.mode == "replay"
    finally:
        await eng.stop()


async def test_unbound_plan_role_falls_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(monkeypatch, None)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(
            "how many times did Ana go running", namespace="a", top_k=2, budget_tokens=120
        )
        assert out.mode == "compose"
        assert eng.model_calls() == {}
    finally:
        await eng.stop()


async def test_aggregate_subqueries_are_extra_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(monkeypatch, _Plan(_plan("aggregate", ["Ana pottery classes", "Ana cat"])))
    probes: list[str] = []
    real_search = Engine._search  # compose probes go through _search (A-1)

    async def spy(self: Engine, query: str, *args: Any, **kwargs: Any) -> Any:
        probes.append(query)
        return await real_search(self, query, *args, **kwargs)

    monkeypatch.setattr(Engine, "_search", spy)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read("what does Ana do", namespace="a", top_k=2, budget_tokens=200)
        assert out.mode == "compose"
        assert "Ana pottery classes" in probes and "Ana cat" in probes
        joined = " ".join(r.content for r in out.context.records)
        assert "pottery" in joined
    finally:
        await eng.stop()


async def test_planner_is_not_called_for_explicit_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Plan(_plan("aggregate"))
    eng = _engine(monkeypatch, stub)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read("what pet", namespace="a", mode="retrieve", budget_tokens=120)
        assert out.mode == "retrieve" and stub.calls == 0
    finally:
        await eng.stop()


def test_read_plan_tolerates_sloppy_replies() -> None:
    plan = ReadPlan.model_validate(
        {"mode": " Aggregate ", "entities": "Ana", "subqueries": ["a", "", "b", "c", "d"]}
    )
    assert plan.mode == "aggregate"
    assert plan.entities == ["Ana"]
    assert plan.subqueries == ["a", "b", "c"]


def _spy_compose(monkeypatch: pytest.MonkeyPatch, eng: Engine) -> list[int]:
    seen: list[int] = []
    real = eng._compose

    async def spy(query: str, ns: str, budget: int, top_k: int, pool: int, **kw: Any) -> Any:
        seen.append(top_k)
        return await real(query, ns, budget, top_k, pool, **kw)

    monkeypatch.setattr(eng, "_compose", spy)
    return seen


@pytest.mark.parametrize(
    ("planner", "reply", "query", "expected"),
    [
        ("llm", _plan("aggregate"), "what pet does Ana have", [20]),  # planner aggregate
        ("rules", None, "how many times did Ana go running", [20]),  # rules compose
        ("llm", _plan("lookup"), "what pet does Ana have", []),  # not aggregation
    ],
)
async def test_aggregate_top_k_widens_routed_compose(
    monkeypatch: pytest.MonkeyPatch,
    planner: str,
    reply: str | None,
    query: str,
    expected: list[int],
) -> None:
    """G11: a routed aggregation read pools ``aggregate_top_k``, still within budget."""
    eng = _engine(monkeypatch, _Plan(reply), planner=planner, aggregate_top_k=20)
    seen = _spy_compose(monkeypatch, eng)
    await eng.start()
    try:
        await _seed(eng)
        out = await eng.read(query, namespace="a", top_k=2, budget_tokens=150)
        assert seen == expected
        assert out.context.tokens_used <= 150
    finally:
        await eng.stop()


async def test_explicit_compose_and_unset_keep_the_callers_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(monkeypatch, _Plan(_plan("aggregate")), aggregate_top_k=20)
    seen = _spy_compose(monkeypatch, eng)
    await eng.start()
    try:
        await _seed(eng)
        await eng.read("what pet", namespace="a", mode="compose", top_k=2, budget_tokens=150)
        assert seen == [2]
    finally:
        await eng.stop()
    eng = _engine(monkeypatch, _Plan(_plan("aggregate")))
    seen = _spy_compose(monkeypatch, eng)
    await eng.start()
    try:
        await _seed(eng)
        await eng.read("what pet", namespace="a", top_k=2, budget_tokens=150)
        assert seen == [2]
    finally:
        await eng.stop()
