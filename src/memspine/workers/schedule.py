"""Sleep cycle (M2/E7): the ordered maintenance pass.

consolidate → decay_sweep → compress → event_log_prune, with the E7
sleep-time-compute hook slot reserved after compress (no-op default, RG tier).
"""

from __future__ import annotations

from memspine.workers.pipelines import PipelineContext
from memspine.workers.runner import TaskRunner

__all__ = ["SLEEP_CYCLE_ORDER", "run_sleep_cycle"]

SLEEP_CYCLE_ORDER: tuple[str, ...] = (
    "consolidate",
    "decay_sweep",
    "compress",
    # E7 hook slot: anticipatory sleep-time compute (reserved, no-op default)
    "event_log_prune",
)


async def run_sleep_cycle(runner: TaskRunner, ctx: PipelineContext) -> dict[str, dict[str, object]]:
    return {name: await runner.run(name, ctx) for name in SLEEP_CYCLE_ORDER}
