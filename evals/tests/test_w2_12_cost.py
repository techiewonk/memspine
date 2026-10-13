"""W2-12: the eval-side cost helper (model-free: a stub engine, a stub-call ledger)."""

from __future__ import annotations

from typing import Any

from memspine.economy.ledger import WriteCostLedger, cost_scope
from memspine_evals.cost import attach_cost, cost_block, unwaived_misses


class _Engine:
    def __init__(self, ledger: WriteCostLedger | None) -> None:
        self._cost_ledger = ledger

    def cost_report(self, namespace: str | None = None) -> dict[str, Any]:
        return {} if self._cost_ledger is None else self._cost_ledger.report(namespace)


def _ledger(window_calls: int) -> WriteCostLedger:
    ledger = WriteCostLedger()
    with cost_scope(namespace="a", layer="facts", stage="window_extract"):
        for _ in range(window_calls):
            ledger.record("generative", model="m", tokens_in=100, tokens_out=50)
    return ledger


def test_flag_off_attaches_nothing() -> None:
    results: dict[str, Any] = {"score": 1.0}
    assert attach_cost(results, _Engine(None), "a", windows=1) == {"score": 1.0}
    assert cost_block(object()) == {}  # an engine without the method
    assert cost_block(_Engine(None)) == {}


def test_report_and_targets_ride_on_the_results() -> None:
    results: dict[str, Any] = {}
    attach_cost(results, _Engine(_ledger(2)), "a", turns=10, windows=2)
    block = results["write_cost"]
    assert block["ledger"]["model_calls"] == 2
    by = {t["name"]: t for t in block["targets"]}
    assert by["generative_calls_per_window"]["status"] == "met"
    assert unwaived_misses(block) == []


def test_a_missed_target_is_listed_unless_waived() -> None:
    block = cost_block(_Engine(_ledger(3)), "a", turns=10, windows=1)
    assert unwaived_misses(block) == ["generative_calls_per_window"]
    waived = cost_block(
        _Engine(_ledger(3)), "a", turns=10, windows=1, waivers={"generative_calls_per_window": "x"}
    )
    assert unwaived_misses(waived) == []
