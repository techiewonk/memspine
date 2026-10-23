"""W2-12: the write-side cost ledger (``economy.ledger_v2``), the budget (``economy.budget``) and
the P-14 targets as regression gates. Model-free: stub LLMs that count their calls, hash embedder."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM, _Adjudicator, _facts, _send, _turns

from memspine import Engine
from memspine.config.schema import EconomyConfig, MemspineConfig
from memspine.derive.backpressure import UNDERIVED_KEY
from memspine.economy import ledger as costs
from memspine.economy.ledger import Budget, WriteCostLedger, cost_scope
from memspine.economy.targets import TargetFailure, TargetSet
from memspine.observability import bindings
from memspine.services.llm.base import LLMRouter

EDGES = [
    {
        "src_entity": "Mira",
        "rel": "lives_in",
        "dst_entity": "Lyon",
        "kind": "state",
        "fact": "Mira lives in Lyon",
        "turns": [1],
        "valid_from": "2023-05-08",
    },
    {
        "src_entity": "Mira",
        "rel": "resides_in",
        "dst_entity": "Paris",
        "kind": "state",
        "fact": "Mira resides in Paris",
        "turns": [2],
        "valid_from": "2023-06-01",
    },
]


@pytest.fixture(autouse=True)
def _no_leak() -> Any:
    yield
    assert costs.installed() is None  # every engine uninstalls its ledger on stop


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    llm: FakeWindowLLM,
    path: Path,
    *,
    economy: dict[str, Any] | None = None,
    outbox: bool = False,
    mode: str = "facts",
    extra_roles: dict[str, Any] | None = None,
    extract_graph: dict[str, Any] | None = None,
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": llm, **(extra_roles or {})})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    semantic: dict[str, Any] = {"enabled": True}
    if extract_graph is not None:
        semantic["policies"] = {"extract_graph": extract_graph}
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "window_extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
                },
                **{
                    r: {
                        "provider": "openai_compatible",
                        "timeout_s": 60.0,
                        "settings": {"api_base": "http://test.invalid/v1", "model": "fake/x"},
                    }
                    for r in (extra_roles or {})
                },
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": semantic},
        write={"window_extract": mode, "window_async": False, "window_idle_s": 0.0},
        workers={"outbox": outbox},
        economy=economy or {},
    )
    await eng.start()
    return eng


async def _three_sessions(eng: Engine) -> None:
    for session, prefix in (("s1", "trip"), ("s2", "walk"), ("s3", "farm")):
        await _send(eng, "a", session, _turns(prefix, 6))
    await eng.flush_windows()


# ------------------------------------------------------------------------------ flag off
def test_defaults_are_off() -> None:
    cfg = EconomyConfig()
    assert cfg.ledger_v2 is False and cfg.budget is None
    assert (
        MemspineConfig(
            runtime={
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            }
        ).economy
        == cfg
    )


async def test_flag_off_installs_nothing_and_the_facts_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run(economy: dict[str, Any], name: str) -> tuple[list[str], int]:
        llm = FakeWindowLLM()
        eng = await _engine(monkeypatch, llm, tmp_path / name, economy=economy)
        try:
            if not economy:
                assert costs.installed() is None and eng.cost_report() == {}
            await _three_sessions(eng)
            return sorted(f.content for f in await _facts(eng)), len(llm.calls)
        finally:
            await eng.stop()

    off = await run({}, "off.db")
    on = await run({"ledger_v2": True}, "on.db")
    assert off[0] and off == on  # the ledger only observes: same facts, same call count


def test_record_call_is_a_passthrough_without_a_ledger() -> None:
    assert costs.installed() is None
    costs.record_call("generative", tokens_in=5)  # nothing installed: no effect, no error


async def test_budget_needs_the_outbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from memspine.exceptions import ConfigError

    with pytest.raises(ConfigError, match="workers.outbox"):
        await _engine(
            monkeypatch,
            FakeWindowLLM(),
            tmp_path / "x.db",
            economy={"ledger_v2": True, "budget": {"max_calls": 1}},
        )


# ------------------------------------------------------------------------------ the ledger counts
async def test_ledger_counts_a_stubbed_window_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, tmp_path / "w.db", economy={"ledger_v2": True})
    try:
        await _three_sessions(eng)
        report = eng.cost_report()
        assert len(llm.calls) == 3  # one call per window (facts only)
        assert report["model_calls"] == len(llm.calls)
        stage = report["by_stage"]["window_extract"]
        assert stage["model_calls"] == 3 and stage["tokens_in"] > 0 and stage["tokens_out"] > 0
        assert stage["latency_ms"] >= 0 and stage["failures"] == 0
        assert report["by_layer"]["facts"]["model_calls"] == 3
        assert report["by_namespace"]["a"]["model_calls"] == 3
        assert report["embed_calls"] >= 1  # the cached hash embedder was asked for vectors
        # per namespace: another namespace has nothing; the handle sees its own scope
        assert eng.cost_report("b")["model_calls"] == 0
        assert eng.cost_report("a")["model_calls"] == 3
        assert eng.scope("a").cost_report()["model_calls"] == 3
        assert report["budget"] is None
    finally:
        await eng.stop()


async def test_ledger_counts_a_stubbed_sleep_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    llm.edges_override = EDGES
    judge = _Adjudicator()
    eng = await _engine(
        monkeypatch,
        llm,
        tmp_path / "s.db",
        economy={"ledger_v2": True},
        mode="facts_graph",
        extra_roles={"invalidate_edge": judge},
        extract_graph={"resolve": "rules", "contradictions": True},
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 2))
        await eng.flush_windows()
        before = eng.cost_report()["by_stage"]
        assert "extract_graph" not in before  # call 1 is the window, invalidation is sleep
        await eng.sleep()
        assert len(judge.calls) == 1
        sleep_stage = eng.cost_report()["by_stage"]["extract_graph"]
        assert sleep_stage["model_calls"] == 1
        assert eng.cost_report()["by_namespace"][costs.SLEEP_POOL]["model_calls"] == 1
        assert eng.cost_report()["by_stage"]["window_extract"]["model_calls"] == len(llm.calls)
    finally:
        await eng.stop()


async def test_a_failed_call_is_counted_and_reraised() -> None:
    ledger = WriteCostLedger()
    with bindings.bound(bindings.EngineBinding(cost_ledger=ledger)):

        class Boom:
            provider_id = "boom"

            async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
                raise TimeoutError("slow")

        router = LLMRouter({"x": Boom()})  # type: ignore[dict-item]
        with cost_scope(namespace="n", layer="facts", stage="window_extract"):
            with pytest.raises(TimeoutError):
                await router.for_role("x").chat([{"role": "user", "content": "hello"}])
    rep = ledger.report("n")
    assert rep["failures"] == 1 and rep["model_calls"] == 1
    assert ledger.records[0].outcome == "timeout"


def test_the_stage_ledger_holds_no_content() -> None:
    ledger = WriteCostLedger()
    with cost_scope(namespace="Ilse's private diary", layer="facts", stage="window extract"):
        ledger.record("generative", model="fake/x", tokens_in=3, tokens_out=2)
    dumped = repr(ledger.stage_ledger.entries)
    assert "Ilse" not in dumped and "diary" not in dumped  # namespace is hashed, stage sanitised


# ------------------------------------------------------------------------------ budget
async def test_budget_pause_marks_underived_and_resumes_with_nothing_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # reference: the same writes with no budget
    ref_llm = FakeWindowLLM()
    ref = await _engine(monkeypatch, ref_llm, tmp_path / "ref.db", outbox=True)
    try:
        await _three_sessions(ref)
        want = sorted(f.content for f in await _facts(ref))
    finally:
        await ref.stop()
    assert want

    llm = FakeWindowLLM()
    eng = await _engine(
        monkeypatch,
        llm,
        tmp_path / "b.db",
        outbox=True,
        economy={"ledger_v2": True, "budget": {"max_calls": 1}},
    )
    try:
        await _three_sessions(eng)
        assert len(llm.calls) == 1  # the budget stopped the model after one call
        got = sorted(f.content for f in await _facts(eng))
        assert 0 < len(got) < len(want)
        stats = eng.window_stats()
        assert stats["deferred_budget"] == 2
        report = eng.cost_report("a")
        assert report["deferred_windows"] == 2
        assert report["budget"]["namespaces"]["a"]["paused"] is True
        # the paused work is in the outbox, marked underived
        parked = eng._windows._outbox.store.items_after(
            eng._windows._scope("a"), "window.deferred", -1
        )
        assert len(parked) == 2 and all(p[1][UNDERIVED_KEY] is True for p in parked)

        assert await eng.resume_deferred("a") == 0  # still spent: nothing resumes
        assert eng.cost_report("a")["deferred_windows"] == 2

        eng._cost_ledger.budget.max_calls = 10  # the operator raises the allowance
        assert await eng.resume_deferred("a") == 2
        assert eng.cost_report("a")["deferred_windows"] == 0
        assert len(llm.calls) == 3
        assert sorted(f.content for f in await _facts(eng)) == want  # nothing was lost
        assert await eng.resume_deferred("a") == 0  # idempotent
    finally:
        await eng.stop()


async def test_a_period_roll_lets_the_work_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = [0.0]
    llm = FakeWindowLLM()
    eng = await _engine(
        monkeypatch,
        llm,
        tmp_path / "p.db",
        outbox=True,
        economy={"ledger_v2": True, "budget": {"max_calls": 1, "period_s": 60.0}},
    )
    try:
        budget = eng._cost_ledger.budget
        budget._clock = lambda: clock[0]
        await _three_sessions(eng)
        assert len(llm.calls) == 1 and eng.window_stats()["deferred_budget"] == 2
        clock[0] = 61.0  # the period rolled: the spend is forgiven
        assert await eng.resume_deferred("a") == 2
        # one call per period: the first parked window ran, the second parked again (not lost)
        assert len(llm.calls) == 2 and eng.cost_report("a")["deferred_windows"] == 1
        clock[0] = 122.0
        assert await eng.resume_deferred("a") == 1
        assert eng.cost_report("a")["deferred_windows"] == 0
        assert len(await _facts(eng)) == 13  # = the unbudgeted run (dedup merges repeats)
    finally:
        await eng.stop()


def test_budget_unit_limits_periods_and_resume() -> None:
    clock = [0.0]
    b = Budget(max_tokens=10, period_s=5.0, clock=lambda: clock[0])
    assert not b.exhausted("n")
    b.charge("n", calls=1, tokens=10)
    assert b.exhausted("n") and not b.exhausted("other")
    assert b.resume("n") is False
    assert b.resume("n", reset=True) is True and not b.exhausted("n")
    b.charge("n", tokens=11)
    clock[0] = 6.0
    assert not b.exhausted("n")  # rolled
    assert Budget().exhausted("n") is False  # no limits: never


async def test_sleep_stages_pause_when_the_sleep_pool_is_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    llm.edges_override = EDGES
    judge = _Adjudicator()
    eng = await _engine(
        monkeypatch,
        llm,
        tmp_path / "sl.db",
        outbox=True,
        economy={"ledger_v2": True, "budget": {"max_calls": 5}},
        mode="facts_graph",
        extra_roles={"invalidate_edge": judge},
        extract_graph={"resolve": "rules", "contradictions": True},
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 2))
        await eng.flush_windows()
        eng._cost_ledger.budget.charge(costs.SLEEP_POOL, calls=5)  # the pool is spent
        stats = await eng.sleep()
        assert stats["extract_graph"] == {"status": "deferred_budget", "underived": True}
        assert judge.calls == []  # no model call was made
        eng._cost_ledger.budget.resume(costs.SLEEP_POOL, reset=True)
        stats = await eng.sleep()  # the work was not dropped: the next cycle does it
        assert stats["extract_graph"]["status"] == "ok" and len(judge.calls) == 1
    finally:
        await eng.stop()


# ------------------------------------------------------------------------------ targets (gates)
async def test_targets_hold_for_a_stubbed_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    llm.edges_override = EDGES
    judge = _Adjudicator()
    eng = await _engine(
        monkeypatch,
        llm,
        tmp_path / "t.db",
        economy={"ledger_v2": True},
        mode="facts_graph",
        extra_roles={"invalidate_edge": judge},
        extract_graph={"resolve": "rules", "contradictions": True},
    )
    try:
        turns = 12
        await _send(eng, "a", "s1", _turns("trip", turns))
        await eng.flush_windows()
        await eng.sleep()
        windows = eng.window_stats()["windows"]
        targets = TargetSet(output_to_input_ratio=None)
        results = targets.check(
            eng._cost_ledger,
            turns=turns,
            windows=windows,
            static_prompt_tokens=412,
            deep_reads=4,
            deep_extra_calls=3,
            deep_extra_searches=2,
        )
        by = {r.name: r for r in results}
        assert by["generative_calls_per_window"].status == "met"
        assert by["generative_calls_per_window"].observed == len(llm.calls) / windows
        assert by["sleep_calls_per_turn"].status == "met"
        assert by["sleep_calls_per_turn"].observed == pytest.approx(1 / turns)  # 0.083 <= 0.2
        assert by["static_prompt_tokens"].status == "met"
        assert by["deep_extra_calls_per_read"].status == "met"
        assert by["deep_extra_searches_per_read"].status == "met"
        TargetSet.assert_met(results)
    finally:
        await eng.stop()


def _ledger_with(window_calls: int, sleep_calls: int, tin: int = 100, tout: int = 150):
    ledger = WriteCostLedger()
    with cost_scope(namespace="a", layer="facts", stage="window_extract"):
        for _ in range(window_calls):
            ledger.record("generative", model="m", tokens_in=tin, tokens_out=tout)
    with cost_scope(namespace="-", layer="reconcile", stage="reconcile"):
        for _ in range(sleep_calls):
            ledger.record("generative", model="m", tokens_in=10, tokens_out=5)
        ledger.record("embedding", model="e", tokens_in=50)  # embeds are not model calls
    return ledger


def test_each_target_fails_when_exceeded() -> None:
    t = TargetSet()
    # 2 calls for 1 window: over the one-call-per-window target
    r = {x.name: x for x in t.check(_ledger_with(2, 0), turns=10, windows=1)}
    assert r["generative_calls_per_window"].status == "missed"
    with pytest.raises(TargetFailure, match="generative_calls_per_window"):
        TargetSet.assert_met(list(r.values()))
    # sleep: 3 calls over 10 turns = 0.3 > 0.2 (the embed call does not count)
    r = {x.name: x for x in t.check(_ledger_with(1, 3), turns=10, windows=1)}
    assert r["sleep_calls_per_turn"].status == "missed"
    assert r["sleep_calls_per_turn"].observed == pytest.approx(0.3)
    # 2 calls over 10 turns = 0.2: at the target is still met
    r = {x.name: x for x in t.check(_ledger_with(1, 2), turns=10, windows=1)}
    assert r["sleep_calls_per_turn"].status == "met"
    # output more than twice the input
    r = {x.name: x for x in t.check(_ledger_with(1, 0, tin=100, tout=250), turns=1, windows=1)}
    assert r["output_to_input_ratio"].status == "missed"
    # static prompt, deep tier
    r = {
        x.name: x
        for x in t.check(
            _ledger_with(1, 0),
            turns=1,
            windows=1,
            static_prompt_tokens=501,
            deep_reads=2,
            deep_extra_calls=3,
            deep_extra_searches=2,
        )
    }
    assert r["static_prompt_tokens"].status == "missed"
    assert r["deep_extra_calls_per_read"].status == "missed"
    assert r["deep_extra_searches_per_read"].status == "met"


def test_a_waiver_turns_a_miss_into_a_recorded_exception() -> None:
    t = TargetSet(waivers={"generative_calls_per_window": "split mode, run 7"})
    results = t.check(_ledger_with(2, 0), turns=10, windows=1)
    waived = [x for x in results if x.name == "generative_calls_per_window"][0]
    assert waived.status == "waived" and waived.waiver == "split mode, run 7"
    TargetSet.assert_met(results)  # a waived miss does not fail the gate


def test_unmeasured_targets_say_so() -> None:
    results = TargetSet().check(WriteCostLedger())
    assert {r.status for r in results} == {"not_measured"}


# ------------------------------------------------------------- FU-REQS: coverage re-extraction target
def _ledger_with_coverage(window_calls: int, coverage_calls: int) -> WriteCostLedger:
    ledger = _ledger_with(window_calls, 0)
    with cost_scope(namespace="a", layer="facts", stage="coverage_reextract"):
        for _ in range(coverage_calls):
            ledger.record("generative", model="m", tokens_in=20, tokens_out=10)
    return ledger


def test_coverage_reextract_calls_have_their_own_per_window_target() -> None:
    by = {r.name: r for r in TargetSet().check(_ledger_with_coverage(2, 1), turns=10, windows=2)}
    assert by["coverage_reextract_calls_per_window"].status == "met"
    assert by["coverage_reextract_calls_per_window"].observed == pytest.approx(0.5)
    # the call-1 target is untouched by the coverage call (one window_extract call per window)
    assert by["generative_calls_per_window"].observed == pytest.approx(1.0)
    by = {r.name: r for r in TargetSet().check(_ledger_with_coverage(1, 3), turns=10, windows=2)}
    assert by["coverage_reextract_calls_per_window"].status == "missed"
    assert by["coverage_reextract_calls_per_window"].observed == pytest.approx(1.5)
    with pytest.raises(TargetFailure, match="coverage_reextract_calls_per_window"):
        TargetSet.assert_met(list(by.values()))


def test_coverage_target_is_zero_when_coverage_is_off_and_waivable() -> None:
    by = {r.name: r for r in TargetSet().check(_ledger_with(1, 0), turns=1, windows=1)}
    assert by["coverage_reextract_calls_per_window"].status == "met"
    assert by["coverage_reextract_calls_per_window"].observed == 0.0
    waived = TargetSet(waivers={"coverage_reextract_calls_per_window": "dense windows"})
    out = waived.check(_ledger_with_coverage(1, 3), turns=1, windows=1)
    assert [r.status for r in out if r.name == "coverage_reextract_calls_per_window"] == ["waived"]
    TargetSet.assert_met(out)
