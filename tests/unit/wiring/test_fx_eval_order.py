"""FX-EVAL: pattern_observations and standing_answers run after the graph stages (flag-gated)."""

from __future__ import annotations

import pytest

from memspine.config.schema import MemspineConfig
from memspine.workers.schedule import SLEEP_CYCLE_ORDER, sleep_stage_order
from memspine.workers.taskiq_runner import PIPELINE_PRIORITIES


def _order(**offline: object) -> tuple[str, ...]:
    class _Ctx:
        config = MemspineConfig(
            offline=offline,
            runtime={
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            },
        )  # type: ignore[arg-type]

    return sleep_stage_order(_Ctx())  # type: ignore[arg-type]


def test_flags_off_order_unchanged() -> None:
    assert _order() == SLEEP_CYCLE_ORDER


@pytest.mark.parametrize(
    "offline",
    [{"patterns": True}, {"standing": True}, {"patterns": True, "standing": True}],
)
def test_after_extract_graph(offline: dict[str, object]) -> None:
    order = _order(**offline)
    last = order.index("extract_graph")
    for name in ("pattern_observations", "standing_answers"):
        if name in order:
            assert last < order.index(name) < order.index("summarize_entities")


def test_after_reconcile_and_histories_and_in_order() -> None:
    order = _order(patterns=True, standing=True, reconcile=True, histories=True)
    assert (
        order.index("histories")
        < order.index("pattern_observations")
        < order.index("standing_answers")
    )


def test_priorities_distinct_and_follow_order() -> None:
    assert len(set(PIPELINE_PRIORITIES.values())) == len(PIPELINE_PRIORITIES)
    p = PIPELINE_PRIORITIES
    assert (
        p["extract_graph"]
        < p["histories"]
        < p["pattern_observations"]
        < p["standing_answers"]
        < p["summarize_entities"]
    )
