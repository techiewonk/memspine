"""Derive stage: check_watches (F-15)."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.derive.pipeline.stages.context import PipelineContext, _sweep_stats
from memspine.memories.prospective.triggers import due_watches, invalidation_watches
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

_log = get_logger(__name__)


#: SF-1/ADR-018: latch so the "invalidation watches never fire under
#: event_log.mode=ephemeral" warning from check_watches is logged at most once
#: per process (mirrors the engine-side latch on the watch-creation path).
_ephemeral_watch_warned = False


async def check_watches(ctx: PipelineContext) -> dict[str, object]:
    """M13.8/ADR-016 prospective sweep: log which watches have fired.

    Read-only and trivially idempotent — delivery is pull-based in v0.1
    (``Engine.due()``), so this step only surfaces fired counts loudly in the
    sleep cycle; nothing is pushed and nothing mutates. Invalidation firing
    reads M4 CONFLICT events from the log (nothing readable in ephemeral
    mode, so only due-time watches can fire there — ADR-016).
    """
    global _ephemeral_watch_warned
    now = datetime.now(UTC)
    ephemeral = ctx.config.event_log.mode is EventLogMode.EPHEMERAL
    due_total = 0
    invalidated_total = 0
    fired_total = 0
    errors: list[str] = []
    conflicts_by_ns: dict[str, list[MemoryEvent]] | None = None  # lazy: one log scan
    for namespace in await ctx.storage.list_namespaces():
        try:
            watches = await ctx.storage.list_records(namespace, "prospective")
            if not watches:
                continue
            due = due_watches(watches, now)
            invalidated: list[MemoryRecord] = []
            has_target_watches = any(watch.entity is not None for watch in watches)
            # SF-1/ADR-018: target watches fire off M4 CONFLICT events; ephemeral
            # mode persists none, so they never fire. Surface it once at sweep
            # time (not just at creation) so long-running deployments see it.
            if ephemeral and has_target_watches and not _ephemeral_watch_warned:
                _ephemeral_watch_warned = True
                _log.warning(
                    "prospective.ephemeral_invalidation_never_fires",
                    namespace=namespace,
                    detail="event_log.mode=ephemeral persists no CONFLICT events — "
                    "invalidation (target) watches can never fire (ADR-016)",
                )
            if has_target_watches:
                if conflicts_by_ns is None:
                    conflicts_by_ns = await _conflicts_by_namespace(ctx)
                invalidated = invalidation_watches(watches, conflicts_by_ns.get(namespace, []))
            fired = {record.record_id for record in due} | {
                record.record_id for record in invalidated
            }
            if fired:
                _log.info(
                    "memory.watch_fired",
                    namespace=namespace,
                    fired=len(fired),
                    due=len(due),
                    invalidated=len(invalidated),
                )
            due_total += len(due)
            invalidated_total += len(invalidated)
            fired_total += len(fired)
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning(
                "check_watches.namespace_failed",
                namespace=namespace,
                error=str(exc),
                exc_info=True,
            )
    stats = _sweep_stats("fired", fired_total, errors)
    stats.update(due=due_total, invalidated=invalidated_total)
    return stats


async def _conflicts_by_namespace(ctx: PipelineContext) -> dict[str, list[MemoryEvent]]:
    """One batched scan of the log for CONFLICT events, bucketed by namespace."""
    buckets: dict[str, list[MemoryEvent]] = {}
    after = 0
    while True:
        batch = await ctx.storage.read_events(after_seq=after)
        if not batch:
            return buckets
        for event in batch:
            if event.kind is EventKind.CONFLICT:
                buckets.setdefault(event.namespace, []).append(event)
        last_seq = batch[-1].seq
        assert last_seq is not None  # events past the door always carry seq
        after = last_seq
