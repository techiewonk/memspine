"""Sleep cycle (M2/E7): the ordered maintenance pass.

consolidate → mine_facts → anticipate → reflect_profile → extract_graph → summarize_entities →
reorganize →
check_watches → decay_sweep → compress → event_log_prune, with the E7 sleep-time-compute hook
slot reserved
after compress (no-op default, RG tier). The C2 extract_graph stage (LLM edge
extraction → asserted links) runs before reorganize so communities form over
the fresh edges; it self-skips without an extract_edges LLM role. The D-42
reorganizer runs right after so fresh summaries join the graph the same cycle
communities are detected; it self-skips without a graph store or ``[community]``.
check_watches (M13.8/ADR-016) is read-only: it logs fired prospective-watch
counts — delivery stays pull-based via ``Engine.due()`` in v0.1.
"""

from __future__ import annotations

from memspine.workers.pipelines import PipelineContext
from memspine.workers.runner import TaskRunner

__all__ = ["RETENTION_STAGE", "SLEEP_CYCLE_ORDER", "run_sleep_cycle"]

SLEEP_CYCLE_ORDER: tuple[str, ...] = (
    "consolidate",
    "mine_facts",
    "anticipate",
    "reflect_profile",
    # C2 optional stage: LLM edge extraction -> semantic facts + asserted links.
    # Runs before reorganize so communities form over the fresh LLM edges.
    "extract_graph",
    # GP-6 optional stage: entity summaries over the fresh facts (off by default).
    "summarize_entities",
    # D-40/D-42 optional stage: graph communities -> summary parents.
    "reorganize",
    # M13.8/ADR-016: log fired prospective watches (pull-based, read-only).
    "check_watches",
    "decay_sweep",
    "compress",
    # E7 hook slot: anticipatory sleep-time compute (no-op default; deployments
    # override by registering their own pipeline under this name)
    "sleep_compute",
    "event_log_prune",
)


#: #48: runs first, and only when ``retention.classes`` is configured, so expired
#: records are gone before consolidation reads them and the default cycle is unchanged.
RETENTION_STAGE = "retention_expire"


async def run_sleep_cycle(runner: TaskRunner, ctx: PipelineContext) -> dict[str, dict[str, object]]:
    order = SLEEP_CYCLE_ORDER
    if ctx.config.retention.classes:
        order = (RETENTION_STAGE, *order)
    return {name: await runner.run(name, ctx) for name in order}
