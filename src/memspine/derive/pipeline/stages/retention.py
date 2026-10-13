"""Derive stages: retention expiry and event-log pruning (F-15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.observability import tap
from memspine.store.events import EventLogMode


async def retention_expire(ctx: PipelineContext) -> dict[str, object]:
    """#48: expire records past their ``retention.classes`` TTL (hard forget)."""
    if not ctx.config.retention.classes and not (
        ctx.config.retention.v2 and ctx.config.retention.profile
    ):
        return {"status": "skipped", "reason": "no retention.classes"}
    if ctx.expire_retention is None:
        return {"status": "skipped", "reason": "no engine forget path in this context"}
    return await ctx.expire_retention()


async def event_log_prune(ctx: PipelineContext) -> dict[str, object]:
    """D-45 rolling-mode retention: prune applied events past the window."""
    if ctx.config.event_log.mode is not EventLogMode.ROLLING:
        return {"status": "skipped", "reason": "event_log.mode != rolling"}
    cutoff = datetime.now(UTC) - timedelta(days=ctx.config.event_log.retention_days)
    pruned = await ctx.storage.prune_events(older_than=cutoff)
    out: dict[str, object] = {"status": "ok", "pruned": pruned}
    if ctx.config.retention.v2:  # T5-05: a pruned log never advertises a full rebuild
        out["rebuild"] = "limited"
        if tap.enabled():
            tap.event(
                "job.retention.prune",
                pruned=pruned,
                window_days=ctx.config.event_log.retention_days,
                rebuild="limited",
            )
    return out
