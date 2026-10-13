"""Session windows: reopen, build, flush, close (F-09)."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Sequence,
)
from datetime import datetime
from typing import Any

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.window import WindowSettings
from memspine.exceptions import (
    ConfigError,
)
from memspine.memories.episodic.lifecycle import session_of
from memspine.models.encoders.query.perspective import (
    inherit_perspective_tags,
)
from memspine.observability.logging import get_logger
from memspine.services.storage.sql_base import SqlStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import (
    MemoryRecord,
    RecordStatus,
    SourceInfo,
)
from memspine.store.scope import validate_namespace
from memspine.derive.pipeline.stages.context import (
    SessionClosed,
    session_close,
)
from memspine.workers.windows import SECTION_MODES, PromptWindowExtractor, WindowPipeline

_log = get_logger(__name__)


async def _reopen_session(eng: Any, storage: SqlStorage, ns: str, session: str | None) -> None:
    """#53: a write to a PASSIVE session reopens it (one SESSION ``active`` event
    listing its members), so the whole conversation is back in default reads."""
    if session is None:
        return
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        passive = eng._passive_sessions.get(ns)
        records: list[MemoryRecord] | None = None
        if passive is None:  # first episodic write here since start or a sleep
            records = await storage.list_records(ns, "episodic")
            passive = {
                sid for r in records if r.scoring.passive and (sid := session_of(r)) is not None
            }
            eng._passive_sessions[ns] = passive
        if session not in passive:
            return
        if records is None:
            records = await storage.list_records(ns, "episodic")
        members = sorted(
            r.record_id
            for r in records
            if session_of(r) == session and r.status is not RecordStatus.DELETED
        )
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.SESSION,
                namespace=ns,
                actor="system",
                payload={
                    "session_id": session,
                    "state": "active",
                    "record_ids": members,
                    "reason": "new_write",
                },
            )
        )
        passive.discard(session)
    _log.info("memory.session_reopened", namespace=ns, session_id=session)


def end_session(eng: Any, namespace: str = "default", session_id: str | None = None) -> None:
    """B0: forget a session's read ledger (``session_id=None``: the namespace's
    anonymous ledger, which no read or write ever clears on its own)."""
    key = eng._ledger_key(validate_namespace(namespace), session_id)
    eng._read_ledger.pop(key, None)
    eng._ledger_written.discard(key)


def _build_windows(eng: Any, config: MemspineConfig) -> WindowPipeline | None:
    """``write.window_extract``: the window manager, or None when off. Hard-fails at start
    (D-10) when the layers it needs are missing, never silently at the first write."""
    mode = config.write.window_extract
    if mode == "off":
        return None
    if eng._llm is None or eng._prompts is None or "window_extract" not in eng._llm.roles:
        raise ConfigError(
            f"write.window_extract={mode!r} needs an LLM role: set llm.roles.window_extract"
        )
    if eng._semantic is None or eng._episodic is None:
        raise ConfigError(
            f"write.window_extract={mode!r} needs memories.episodic and memories.semantic "
            "enabled (turns are episodic, facts land in the semantic layer)"
        )
    sections = SECTION_MODES[mode]
    role = config.llm.roles.get("window_extract")
    storage = eng._storage
    assert storage is not None  # built in start() before the memory types
    asynchronous = config.write.window_async
    # runtime.container.client_is_memory, inlined: ingest sits below runtime
    if asynchronous and config.storage.backend == "sqlite" and config.storage.path == ":memory:":
        # Like the sleep scheduler: one shared connection, no WAL, so a background call
        # would collide with foreground writes ("table is locked"). Batch mode instead.
        _log.warning(
            "window.async_disabled_on_memory_db",
            detail="write.window_async ignored for storage.path=':memory:'; "
            "call flush_windows() (sleep() and close_session() do)",
        )
        asynchronous = False
    return WindowPipeline(
        settings=WindowSettings(
            max_turns=config.write.window_max_turns,
            max_tokens=config.write.window_max_tokens,
            topic_k=config.write.window_topic_k,
            idle_s=config.write.window_idle_s,
        ),
        sections=sections,
        extractor=PromptWindowExtractor(
            eng._llm.for_role("window_extract"),
            eng._prompts.for_role("window_extract"),
            role.model if role is not None else "",
        ),
        get_record=storage.get_record,
        deposit_fact=eng._deposit_mined_fact,
        deposit_episode=eng._deposit_window_episode if "episode" in sections else None,
        embed=eng._embedder.embed if eng._embedder is not None else None,
        lock=eng._namespace_lock,
        cache=eng._cache,
        use_cache=config.write.window_cache,
        split=config.write.window_split,
        asynchronous=asynchronous,
        on_session_closed=eng._window_session_closed,
    )


async def _window_session_closed(eng: Any, info: SessionClosed) -> dict[str, object]:
    """The session-closed hook of the window manager: the call-2 stage (a stub today)."""
    return await session_close(eng._pipeline_ctx(), info)


async def flush_windows(
    eng: Any, namespace: str | None = None, *, final: bool = True
) -> dict[str, Any]:
    """W1: run every closed window's call now (all namespaces, or one) and return the
    write-quality counters. ``final=True`` also closes the open windows and fires the
    session-closed hook for the sessions still open: the end of a stream or a batch (the
    eval harness calls it before reads and before the sleep cycle). A no-op returning
    ``{}`` when ``write.window_extract`` is off."""
    eng._require_started()
    if eng._windows is None:
        return {}
    ns = validate_namespace(namespace) if namespace is not None else None
    return await eng._windows.flush(ns, final=final)


async def close_session(
    eng: Any, namespace: str = "default", session_id: str | None = None
) -> dict[str, Any]:
    """W1: the real session-closed hook. Cuts the session's open window, runs the
    namespace's queued window calls in order, then fires the session-close stage (call 2:
    a stub today, it only records a ``session_closed`` MARKER). ``session_id=None``: the
    namespace's current session. Works without ``write.window_extract`` too (then only the
    stage runs, and a ``session_id`` is required)."""
    eng._require_started()
    ns = validate_namespace(namespace)
    if eng._windows is not None:
        return await eng._windows.close_session(ns, session_id)
    if session_id is None:
        return {"status": "skipped", "reason": "no session_id and write.window_extract is off"}
    hook = await session_close(eng._pipeline_ctx(), SessionClosed(ns, session_id))
    return {"status": "ok", "session_id": session_id, "hook": hook}


async def _deposit_window_episode(
    eng: Any,
    namespace: str,
    text: str,
    parents: list[str],
    valid_from: datetime,
    window_id: str,
    *,
    require_live: Sequence[str] = (),
) -> MemoryRecord | None:
    """W1: one episode fragment through the semantic door: derived (non-privileged
    ``assistant`` role, channel ``window_episode``), parents = the cited turns, trust capped
    at the least trusted parent, screened by the firewall, never keyed (no entity or
    attribute, so the ladder ADDs it). Skipped under the lock when a parent or another turn
    of the window is no longer live (I72)."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    sources = [r for r in [await storage.get_record(p) for p in parents] if r is not None]
    tags = ["episode_fragment", "kind:episode", f"window:{window_id}"]
    if sources and all("assistant_claim" in r.tags for r in sources):
        tags.append("assistant_claim")
    if sources and eng._perspective_options().on:
        tags.extend(t for t in inherit_perspective_tags(sources) if t not in tags)
    record = MemoryRecord(
        namespace=ns,
        memory_type="semantic",
        content=text,
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel=constants.WINDOW_EPISODE_CHANNEL,
            parents=list(parents),
        ),
        valid_from=valid_from,
        tags=tags,
    )
    cap = [min(r.trust for r in sources)] if sources else None
    integrity_cap = await eng._parent_trust_cap(ns, parents)
    if integrity_cap:
        cap = [*(cap or []), *integrity_cap]
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        if await eng._derived_parents_gone(ns, [*parents, *require_live], "window_extract"):
            return None
        return await eng._write_locked(storage, ns, record, "semantic", "system", cap)
