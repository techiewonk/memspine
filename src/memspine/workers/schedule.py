"""Sleep cycle (M2/E7): the ordered maintenance pass.

consolidate → mine_facts → anticipate → reflect_profile → extract_graph → summarize_entities →
reorganize → check_watches → session_lifecycle → decay_sweep → compress → event_log_prune, with
the E7 sleep-time-compute hook slot reserved
after compress (no-op default, RG tier). The C2 extract_graph stage (LLM edge
extraction → asserted links) runs before reorganize so communities form over
the fresh edges; it self-skips without an extract_edges LLM role. The D-42
reorganizer runs right after so fresh summaries join the graph the same cycle
communities are detected; it self-skips without a graph store or ``[community]``.
check_watches (M13.8/ADR-016) is read-only: it logs fired prospective-watch
counts — delivery stays pull-based via ``Engine.due()`` in v0.1.
"""

from __future__ import annotations

from memspine.workers.pipelines import PipelineContext, rule_edges_options
from memspine.workers.runner import TaskRunner

__all__ = [
    "PREDICT_CALIBRATE_STAGE",
    "RETENTION_STAGE",
    "RULE_EDGES_STAGE",
    "SLEEP_CYCLE_ORDER",
    "run_sleep_cycle",
]

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
    # #53 optional stage: idle sessions -> PASSIVE (skipped without passive_after).
    "session_lifecycle",
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


#: #62: runs right after ``mine_facts``, and only when
#: ``consolidation.predict_calibrate`` is on, so the default cycle is unchanged.
PREDICT_CALIBRATE_STAGE = "predict_calibrate"


#: W12 (ADR-061): runs right after ``extract_graph``, and only when
#: ``memories.associative.policies.rule_edges`` is on, so the default cycle is unchanged.
RULE_EDGES_STAGE = "rule_edges"


def _predict_calibrate_on(ctx: PipelineContext) -> bool:
    mem = ctx.config.memories.get("episodic")
    options = mem.policies.get("consolidation") if mem is not None else None
    return isinstance(options, dict) and bool(options.get("predict_calibrate", False))


async def run_sleep_cycle(runner: TaskRunner, ctx: PipelineContext) -> dict[str, dict[str, object]]:
    order = SLEEP_CYCLE_ORDER
    if _predict_calibrate_on(ctx):
        at = order.index("mine_facts") + 1
        order = (*order[:at], PREDICT_CALIBRATE_STAGE, *order[at:])
    if rule_edges_options(ctx.config) is not None:
        at = order.index("extract_graph") + 1
        order = (*order[:at], RULE_EDGES_STAGE, *order[at:])
    if ctx.config.retention.classes:
        order = (RETENTION_STAGE, *order)
    return {name: await runner.run(name, ctx) for name in order}
