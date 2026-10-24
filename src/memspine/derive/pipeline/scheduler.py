"""Derive: the sleep verb, prompt-version sync and record inflation (F-15, from the engine)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from memspine.derive.budget import COVERS_SEQ, DerivationBudget
from memspine.derive.job import LeaseStore
from memspine.derive.scheduler import DerivationScheduler, job_namespace
from memspine.derive.triggers import TriggerConfig, TriggerPolicy
from memspine.exceptions import MemspineError, StorageError
from memspine.memories.procedural.prompt_registry import prompt_version_records
from memspine.observability.logging import EVENT_WRITE, get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace
from memspine.workers.schedule import (
    maintenance_stage_order,
    run_sleep_cycle,
    run_sleep_stage,
    sleep_stage_order,
)

_log = get_logger(__name__)


async def sync_prompt_versions(eng: Any, namespace: str = "default") -> list[MemoryRecord]:
    """D-43 §4: record each resolved prompt version as a procedural record
    (reference only — the definition lives in prompts/). Idempotent."""
    eng._require_started()
    if eng._procedural is None or eng._prompts is None:
        raise MemspineError(
            "prompt-version sync needs procedural memory enabled and prompts loaded"
        )
    ns = validate_namespace(namespace)
    # Same one-writer-per-namespace unit as every other write verb: the
    # read-diff-append sequence below must not interleave with itself
    # (double-sync duplicating versions) or with corroboration's
    # read-modify-write. Firewall exemption: content is deterministic,
    # system-generated reference strings — see ADR-014.
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        existing = await eng._procedural.list(ns, kind="prompt")
        fresh = prompt_version_records(ns, eng._prompts, existing)
        for record in fresh:
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=ns,
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
    if fresh:
        _log.info(EVENT_WRITE, namespace=ns, prompt_versions=len(fresh))
    return fresh


def build_sleep(eng: Any, config: Any) -> DerivationScheduler:
    """O4-01a: the derivation scheduler for ``offline.sleep``. The lease/watermark tables live in
    the storage database itself (RM-111 WP5: encrypted with it on SQLite, shared on Postgres), so
    two processes on one database never overlap; in memory for a ``:memory:`` store."""
    off = config.offline
    sql = getattr(eng, "_sql", None)
    resources = getattr(eng, "_provider_resources", None)
    path = ":memory:"
    shared_sql_engine = None
    if sql is not None:
        capabilities = sql.capabilities
        if sql.is_memory:
            path = ":memory:"
        elif {"sync_sql", "sql.shared_leases"} & capabilities and resources is not None:
            shared_sql_engine = sql.sync_lease(resources, "sleep_leases").engine
        else:
            _log.warning(
                "sleep.leases_in_memory",
                detail="selected SQL provider has no durable lease capability",
            )
    policy = TriggerPolicy(
        TriggerConfig(
            min_turns=off.sleep_min_turns,
            idle=timedelta(minutes=off.sleep_idle_minutes),
            context_pressure=off.sleep_context_pressure,
            min_gap=timedelta(minutes=off.sleep_min_gap_minutes),
            maintenance_interval=timedelta(hours=off.sleep_maintenance_hours),
        )
    )

    scoped = bool(off.sleep_scoped)  # FU-SLEEP: a namespace job sweeps its own namespace only

    async def _run_stage(name: str) -> dict[str, object]:
        assert eng._runner is not None
        ctx = eng._pipeline_ctx()
        if scoped:
            ctx.scope_namespace = job_namespace()
        return await run_sleep_stage(eng._runner, ctx, name)

    async def _prepare() -> None:
        if eng._windows is not None:  # W1: call 1 first (same as the blocking sleep)
            await eng._windows.flush(final=not eng._windows.asynchronous)

    budget = None
    if off.budget and (
        off.budget_calls_per_cycle is not None or off.budget_tokens_per_cycle is not None
    ):
        budget = DerivationBudget(off.budget_calls_per_cycle, off.budget_tokens_per_cycle)
    tracker = getattr(eng, "_freshness_tracker", None)
    return DerivationScheduler(
        store=LeaseStore(shared_sql_engine if shared_sql_engine is not None else path),
        policy=policy,
        run_stage=_run_stage,
        stages=lambda: sleep_stage_order(eng._pipeline_ctx()),
        maintenance_stages=lambda: maintenance_stage_order(eng._pipeline_ctx()),
        prepare=_prepare,
        lease_ttl=off.sleep_lease_ttl_seconds,
        background_priority=bool(config.runtime.scheduler),
        idle_timers=not off.sleep_manual_only,
        budget=budget,
        covers_snapshot=tracker.snapshot if tracker is not None else None,
    )


async def sleep(eng: Any) -> Any:
    """Run the maintenance sleep cycle now (M2/E7): consolidate → decay →
    compress → prune. All steps are idempotent; P3 gives them teeth.

    Under ``offline.sleep`` (O4-01a) this does not block: it starts a namespace job and returns
    its :class:`~memspine.derive.job.SleepHandle`."""
    eng._require_started()
    if eng._derive_sched is not None:
        return eng._derive_sched.sleep()
    assert eng._runner is not None
    if eng._windows is not None:
        # W1: call 1 first, so call 3 (extract_graph) sees this cycle's window edges. In
        # live (async) mode the open window is left to its idle timer; in batch mode
        # nothing else would ever close it.
        await eng._windows.flush(final=not eng._windows.asynchronous)
    tracker = getattr(eng, "_freshness_tracker", None)
    marks = COVERS_SEQ.set(tracker.snapshot()) if tracker is not None else None
    try:
        stats = await run_sleep_cycle(eng._runner, eng._pipeline_ctx())
    finally:
        if marks is not None:
            COVERS_SEQ.reset(marks)
    eng._passive_sessions.clear()  # #53: the cycle may have passivated sessions
    degraded = {
        name: stage for name, stage in stats.items() if stage.get("status") in ("error", "partial")
    }
    if degraded:
        # Callers rarely inspect the stats dict — degradation must be loud.
        _log.warning("memory.sleep_degraded", failures=degraded)
    return stats


def _inflate_all(eng: Any, records: list[MemoryRecord], namespace: str) -> list[MemoryRecord]:
    """Inflate cold-tier content, skipping (loudly) any corrupt row rather
    than failing the entire read (blast-radius containment).

    Quarantined rows are NOT filtered here by design: ``retrieve(include_held=True)``
    is the operator listing/audit surface, so held content stays inspectable.
    Model-facing paths (``search``/``assemble``/timeline/sessions) apply
    the E1 quarantine gate themselves — never feed ``retrieve()`` output
    to a context window."""
    inflated: list[MemoryRecord] = []
    for record in records:
        if record.status is RecordStatus.DELETED:
            continue
        try:
            inflated.append(eng._inflate.inflate(record))
        except StorageError:
            _log.warning("memory.inflate_failed", namespace=namespace, record_id=record.record_id)
    return inflated
