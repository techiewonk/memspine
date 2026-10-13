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

from contextlib import AbstractContextManager, nullcontext

from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.derive.pipeline.stages.mining import projection_on
from memspine.derive.pipeline.stages.rule_edges import rule_edges_options
from memspine.economy import ledger as _costs
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.ops import ledger as _ops
from memspine.workers.runner import TaskRunner

_log = get_logger(__name__)

__all__ = [
    "COMPILE_EDGES_STAGE",
    "ERASURE_REDERIVE_STAGE",
    "HISTORIES_STAGE",
    "MAINTENANCE_STAGES",
    "PATTERN_OBSERVATIONS_STAGE",
    "PREDICT_CALIBRATE_STAGE",
    "PROJECT_FACTS_STAGE",
    "RECONCILE_STAGE",
    "RETENTION_STAGE",
    "RULE_EDGES_STAGE",
    "SLEEP_CYCLE_ORDER",
    "STANDING_ANSWERS_STAGE",
    "maintenance_stage_order",
    "run_sleep_cycle",
    "run_sleep_stage",
    "sleep_stage_order",
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


#: E01 write-time arm: runs right after ``mine_facts``, and only when
#: ``memories.semantic.policies.fact_projection`` is ``on``, so the default cycle is unchanged.
PROJECT_FACTS_STAGE = "project_facts"


#: O4-05 (``offline.patterns_v2``) and O4-08 (``offline.standing_v2``): run right after
#: ``reflect_profile``, in that order, and only when their flag is on (default cycle unchanged).
PATTERN_OBSERVATIONS_STAGE = "pattern_observations"
STANDING_ANSWERS_STAGE = "standing_answers"


#: O4-03: runs after ``extract_graph``/``rule_edges`` (the fresh facts are folded before summaries
#: read them), only when ``offline.reconcile_v2`` is on, so the default cycle is unchanged.
RECONCILE_STAGE = "reconcile"


#: O4-04: runs right after ``reconcile`` (a superseded value is never merged into a live one), only
#: when ``offline.histories_v2`` is on, so the default cycle is unchanged.
HISTORIES_STAGE = "histories"


#: F-02c: runs right before ``summarize_entities``, only when ``graph.edge_compiler_v2`` is on.
COMPILE_EDGES_STAGE = "compile_edges"


#: FU-ERASE: runs right after ``retention_expire`` and before ``consolidate`` (a re-derived summary
#: carries its membership key, so consolidation skips it), only when ``privacy.erasure_rederive``
#: and ``privacy.erasure_v2`` are on, so the default cycle is unchanged.
ERASURE_REDERIVE_STAGE = "erasure_rederive"


def _predict_calibrate_on(ctx: PipelineContext) -> bool:
    mem = ctx.config.memories.get("episodic")
    options = mem.policies.get("consolidation") if mem is not None else None
    return isinstance(options, dict) and bool(options.get("predict_calibrate", False))


def sleep_stage_order(ctx: PipelineContext) -> tuple[str, ...]:
    """The ordered stage names of one sleep cycle for this configuration."""
    order = SLEEP_CYCLE_ORDER
    if _predict_calibrate_on(ctx):
        at = order.index("mine_facts") + 1
        order = (*order[:at], PREDICT_CALIBRATE_STAGE, *order[at:])
    if projection_on(ctx):
        at = order.index("mine_facts") + 1
        order = (*order[:at], PROJECT_FACTS_STAGE, *order[at:])
    if rule_edges_options(ctx.config) is not None:
        at = order.index("extract_graph") + 1
        order = (*order[:at], RULE_EDGES_STAGE, *order[at:])
    if ctx.config.offline.patterns_v2:
        at = order.index("reflect_profile") + 1
        order = (*order[:at], PATTERN_OBSERVATIONS_STAGE, *order[at:])
    if ctx.config.offline.standing_v2:
        after = PATTERN_OBSERVATIONS_STAGE if ctx.config.offline.patterns_v2 else "reflect_profile"
        at = order.index(after) + 1
        order = (*order[:at], STANDING_ANSWERS_STAGE, *order[at:])
    if ctx.config.offline.reconcile_v2 or ctx.config.offline.histories_v2:
        at = order.index(RULE_EDGES_STAGE if RULE_EDGES_STAGE in order else "extract_graph") + 1
        extra = tuple(
            name
            for name, on in (
                (RECONCILE_STAGE, ctx.config.offline.reconcile_v2),
                (HISTORIES_STAGE, ctx.config.offline.histories_v2),
            )
            if on
        )
        order = (*order[:at], *extra, *order[at:])
    if ctx.config.graph.edge_compiler_v2:  # F-02c: once the facts settled
        at = order.index("summarize_entities")
        order = (*order[:at], COMPILE_EDGES_STAGE, *order[at:])
    if ctx.config.privacy.erasure_v2 and ctx.config.privacy.erasure_rederive:
        order = (ERASURE_REDERIVE_STAGE, *order)
    if ctx.config.retention.classes:
        order = (RETENTION_STAGE, *order)
    return order


#: O4-01a: the daily maintenance pass (decay, retention, compress), as stage names.
MAINTENANCE_STAGES: tuple[str, ...] = ("decay_sweep", "compress")


def maintenance_stage_order(ctx: PipelineContext) -> tuple[str, ...]:
    """The stages of the daily maintenance job: retention (when classes are configured), decay,
    compress. A subset of the sleep order, in the same relative order."""
    full = sleep_stage_order(ctx)
    return tuple(s for s in full if s in MAINTENANCE_STAGES or s == RETENTION_STAGE)


async def run_sleep_stage(
    runner: TaskRunner, ctx: PipelineContext, name: str
) -> dict[str, object]:
    """Run one named stage, timed in the ops ledger when ``observability.ops_ledger`` is on
    (T5-06: runs, errors, dropped and deferred counts; a pass-through otherwise)."""
    with _ops.stage("sleep", name) as handle:
        stat = await _run_sleep_stage(runner, ctx, name)
        if handle is not None:
            if str(stat.get("status", "")).startswith("deferred"):
                handle.add(deferred=1)
            if "error" in stat:
                handle.add(errors=1)
            for key in ("dropped", "deferred"):
                val = stat.get(key)
                if isinstance(val, int) and not isinstance(val, bool):
                    handle.add(**{key: val})
        return stat


async def _run_sleep_stage(
    runner: TaskRunner, ctx: PipelineContext, name: str
) -> dict[str, object]:
    """Run one named stage (one line per stage, so a silent stall names its stage; WT-02 trace)."""
    _log.info("sleep.stage", stage=name)
    ledger = _costs.installed()  # W2-12 (economy.ledger_v2); None = the original path
    pool = ctx.scope_namespace or _costs.SLEEP_POOL  # FU-SLEEP: a scoped job has its own pool
    if ledger is not None and ledger.budget_exhausted(pool):
        # W-26 D / O-05: the sleep budget is spent. The stage is not run; its work stays
        # dirty (every stage reads the store, so the next cycle picks it up), counted here.
        assert ledger.budget is not None
        ledger.budget.note_paused(pool)
        return {"status": "deferred_budget", "underived": True}
    if not tap.enabled():
        with _stage_cost(ledger, name, pool):
            return await runner.run(name, ctx)
    with tap.request("job", stage=name):  # WT-02: one child trace per stage
        tap.event("job.stage.start", stage=name)
        try:
            with _stage_cost(ledger, name, pool):
                stat = await runner.run(name, ctx)
        except BaseException as exc:
            tap.event("job.stage.end", stage=name, status="raised", error=type(exc).__name__)
            raise
        tap.event("job.stage.end", **_stage_fields(name, stat))
        return stat


async def run_sleep_cycle(runner: TaskRunner, ctx: PipelineContext) -> dict[str, dict[str, object]]:
    stats: dict[str, dict[str, object]] = {}
    for name in sleep_stage_order(ctx):
        stats[name] = await run_sleep_stage(runner, ctx, name)
    return stats


def _stage_cost(
    ledger: _costs.WriteCostLedger | None, name: str, pool: str = _costs.SLEEP_POOL
) -> AbstractContextManager[None]:
    """W2-12: attribute a sleep stage's model calls to it. Engine-wide stages: namespace ``"-"``
    (the ``SLEEP_POOL`` budget key); a namespace-scoped job (FU-SLEEP): its own namespace.
    A null context without a ledger."""
    if ledger is None:
        return nullcontext()
    return _costs.cost_scope(namespace=pool, layer=name, stage=name)


def _stage_fields(name: str, stat: dict[str, object]) -> dict[str, object]:
    """A stage's stats dict as tap fields: numbers and the status code; reason text is content."""
    out: dict[str, object] = {"stage": name, "status": str(stat.get("status", "ok"))}
    for key, val in stat.items():
        if key in ("status", "stage") or isinstance(val, bool):
            if isinstance(val, bool):
                out[key] = val
        elif isinstance(val, (int, float)):
            out[key] = val
    if out["status"] == "skipped":
        out["skipped"] = True
    if "reason" in stat:
        out["reason_text"] = tap.content(str(stat["reason"]))
    if "error" in stat:
        out["error_text"] = tap.content(str(stat["error"]))
        out["failed"] = True
    return out
