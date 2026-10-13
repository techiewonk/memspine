"""Derive: session_close, the call-2 hook (F-15)."""

from __future__ import annotations

from memspine.derive.pipeline.stages.close_v2 import run_close_v2
from memspine.derive.pipeline.stages.context import PipelineContext, SessionClosed
from memspine.store.events import EventKind, EventLogMode, MemoryEvent

#: W1: the marker a closed session leaves in the log (ids and a count, never content).
SESSION_CLOSED_MARKER = "session_closed"


async def session_close(ctx: PipelineContext, event: SessionClosed) -> dict[str, object]:
    """W1 call 2, session close: the hook and a STUB stage.

    Called once when a session ends (``Engine.close_session``, a new session starting in the
    namespace, or the end of a stream). The content of call 2 (session summary, the episode
    narrative stitched from the window fragments, profile updates as guarded operations) is
    not built yet: with no ``ctx.close_session`` handler this only records that the session
    closed (a MARKER with the session id and the window count) and reports ``stub``. A handler
    replaces the stub without touching the engine. Not in ``PIPELINES``: it takes an event,
    not just a context, and runs on a session boundary, not in the sleep cycle."""
    if ctx.close_session is not None:
        return await ctx.close_session(event)
    skipped_v2: dict[str, object] | None = None
    if ctx.config.write.session_close_v2:  # O4-02, opt-in: the real call 2
        stats = await run_close_v2(ctx, event)
        if stats.get("status") != "skipped" or stats.get("reason") == "already done":
            await _mark_closed(ctx, event)
            return stats
        skipped_v2 = stats
    await _mark_closed(ctx, event)
    out: dict[str, object] = {
        "status": "stub",
        "reason": "call 2 (summary, episode narrative, profile updates) is not built yet",
        "session_id": event.session_id,
        "windows": event.windows,
    }
    if skipped_v2 is not None:
        out["v2"] = skipped_v2
    return out


async def _mark_closed(ctx: PipelineContext, event: SessionClosed) -> None:
    """The marker a closed session leaves in the log (ids and a count, never content)."""
    if ctx.append_event is not None and ctx.config.event_log.mode is not EventLogMode.EPHEMERAL:
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace=event.namespace,
                actor="system",
                payload={
                    "marker": SESSION_CLOSED_MARKER,
                    "session_id": event.session_id,
                    "windows": event.windows,
                },
            )
        )
