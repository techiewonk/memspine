"""Sleep cycle (M2/E7): the ordered maintenance pass.

consolidate → reorganize → decay_sweep → compress → event_log_prune, with the
E7 sleep-time-compute hook slot reserved after compress (no-op default, RG
tier). The D-42 reorganizer runs right after consolidate so fresh summaries
join the graph the same cycle communities are detected; it self-skips without
a graph store or the ``[community]`` extra.
"""

from __future__ import annotations

from memspine.workers.pipelines import PipelineContext
from memspine.workers.runner import TaskRunner

__all__ = ["SLEEP_CYCLE_ORDER", "run_sleep_cycle"]

SLEEP_CYCLE_ORDER: tuple[str, ...] = (
    "consolidate",
    # D-40/D-42 optional stage: graph communities -> summary parents.
    "reorganize",
    "decay_sweep",
    "compress",
    # E7 hook slot: anticipatory sleep-time compute (no-op default; deployments
    # override by registering their own pipeline under this name)
    "sleep_compute",
    "event_log_prune",
)


async def run_sleep_cycle(runner: TaskRunner, ctx: PipelineContext) -> dict[str, dict[str, object]]:
    return {name: await runner.run(name, ctx) for name in SLEEP_CYCLE_ORDER}
