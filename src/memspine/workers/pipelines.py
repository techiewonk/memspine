"""ANTI-LOCK-IN CORE (D-17): background pipelines as plain, idempotent,
async step functions. Runners decorate these; NO runner imports here, ever.

Each pipeline takes a :class:`PipelineContext` and returns a stats dict.
Phase 1 ships the seam plus the one real pipeline P0 created a need for
(event-log pruning, D-45); consolidation/decay/compression gain their logic in
Phase 3 and stay no-ops until then — calling them is always safe (idempotent).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memspine.config.schema import MemspineConfig
from memspine.core.events import EventLogMode
from memspine.services.storage.sqlite.engine import SQLiteStorage

__all__ = ["PIPELINES", "Pipeline", "PipelineContext", "event_log_prune"]


@dataclass
class PipelineContext:
    storage: SQLiteStorage
    config: MemspineConfig


Pipeline = Callable[[PipelineContext], Awaitable[dict[str, object]]]


async def event_log_prune(ctx: PipelineContext) -> dict[str, object]:
    """D-45 rolling-mode retention: prune applied events past the window."""
    if ctx.config.event_log.mode is not EventLogMode.ROLLING:
        return {"status": "skipped", "reason": "event_log.mode != rolling"}
    cutoff = datetime.now(UTC) - timedelta(days=ctx.config.event_log.retention_days)
    pruned = await ctx.storage.prune_events(older_than=cutoff)
    return {"status": "ok", "pruned": pruned}


async def consolidate(ctx: PipelineContext) -> dict[str, object]:
    """M2 consolidation — logic lands in Phase 3; safe no-op until then."""
    return {"status": "noop", "lands": "P3"}


async def decay_sweep(ctx: PipelineContext) -> dict[str, object]:
    """M3 decay tiers — logic lands in Phase 3; safe no-op until then."""
    return {"status": "noop", "lands": "P3"}


async def compress(ctx: PipelineContext) -> dict[str, object]:
    """M6/D-32 cold-tier compression — logic lands in Phase 3; safe no-op."""
    return {"status": "noop", "lands": "P3"}


#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "consolidate": consolidate,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "event_log_prune": event_log_prune,
}
