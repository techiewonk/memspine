"""Read path: timeview moved out of the engine (F-10b)."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from memspine.config import constants
from memspine.core.read_filters import active_date_filter, active_record_scope
from memspine.exceptions import MemspineError
from memspine.memories.episodic.sessions import Session
from memspine.read.pipeline.state import (
    _FOCAL_ENTITY,
    _PASSIVE_SCOPE,
    _READ_SNAPSHOT,
    CUE_TAG,
    _PassiveScope,
)
from memspine.store.records import MemoryRecord, RecordStatus, chrono_key
from memspine.store.scope import validate_namespace


def _snapshot_listings(storage: Any) -> None:
    """I2: make ``storage.list_records`` read through the current read snapshot.

    Inside a read every listing of a (namespace, type, group) is fetched once and a
    fresh list is returned to each caller; outside a read it is the plain listing.
    Every append clears the snapshot (``_append_and_project``), so a read never sees
    a listing older than its own writes. The episodic store and every engine helper
    share the storage object, so all of them go through it."""
    original = storage.list_records

    async def list_records(
        namespace: str, memory_type: str | None = None, group_id: str | None = None
    ) -> list[MemoryRecord]:
        cache = _READ_SNAPSHOT.get()
        if cache is None:
            records: list[MemoryRecord] = await original(namespace, memory_type, group_id)
            return records
        key = (namespace, memory_type, group_id)
        if key not in cache:
            cache[key] = await original(namespace, memory_type, group_id)
        return list(cache[key])

    storage.list_records = list_records


def _snapshot_scoped[**P, R](
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """I2: run ``fn`` inside one read snapshot (a nested call reuses the outer one)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if _READ_SNAPSHOT.get() is not None:
            return await fn(*args, **kwargs)
        token = _READ_SNAPSHOT.set({})
        focal = _FOCAL_ENTITY.set(None)  # G-11: a focal entity lives for one read
        try:
            return await fn(*args, **kwargs)
        finally:
            _FOCAL_ENTITY.reset(focal)
            _READ_SNAPSHOT.reset(token)

    return wrapper


def _passive_scoped[**P, R](
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    """#53: run a public read with its ``include_passive`` / ``session_id`` arguments
    widening the passive-session scope for every search it makes (nested reads
    only widen it, never narrow it)."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        bound = signature.bind_partial(*args, **kwargs).arguments
        include = bool(bound.get("include_passive", False))
        # I9: ``ledger_id`` is the clearer name of the same argument and wins.
        session = bound.get("ledger_id") or bound.get("session_id")
        outer = _PASSIVE_SCOPE.get()
        if not include and not session:
            return await fn(*args, **kwargs)
        scope = _PassiveScope(
            include_all=include or (outer is not None and outer.include_all),
            sessions=(outer.sessions if outer is not None else frozenset())
            | ({str(session)} if session else frozenset()),
        )
        token = _PASSIVE_SCOPE.set(scope)
        try:
            return await fn(*args, **kwargs)
        finally:
            _PASSIVE_SCOPE.reset(token)

    return wrapper


def _current_at(record: MemoryRecord, as_of: datetime | None) -> bool:
    """W7: ``record`` is a superseded (ARCHIVED) fact that was the current one at
    ``as_of``: begun by then, ended after it, not a retraction, never quarantined."""
    if as_of is None or record.status is not RecordStatus.ARCHIVED or record.quarantined:
        return False
    if record.valid_to is None or "retract" in record.tags:
        return False
    start = record.valid_from if record.valid_from.tzinfo else record.valid_from.replace(tzinfo=UTC)
    end = record.valid_to if record.valid_to.tzinfo else record.valid_to.replace(tzinfo=UTC)
    return start <= as_of < end


def _by_time(
    lines: Mapping[str, tuple[MemoryRecord, str]],
) -> list[tuple[MemoryRecord, str]]:
    return sorted(lines.values(), key=lambda v: chrono_key(v[0]))


async def _date_allowed(eng: Any, ns: str) -> tuple[set[str], int] | None:
    """#37: under an active date filter, the ids a search leg may return, and how
    many records the namespace holds (the leg window that reaches all of them).

    The ids are the records that match the filter plus every cue record: a cue is a
    key whose target is judged after the gates resolve it. None: no filter."""
    date_filter = active_date_filter()
    scope = active_record_scope()  # I7 / I8: session and role limits
    if date_filter is None and scope is None:
        return None
    listed = await eng._records(ns)
    allowed = {
        r.record_id
        for r in listed
        if CUE_TAG in r.tags
        or ((date_filter is None or date_filter.matches(r)) and (scope is None or scope.matches(r)))
    }
    return allowed, len(listed)


async def timeline(
    eng: Any,
    namespace: str = "default",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MemoryRecord]:
    """Chronological episodic timeline (M13.2), optionally windowed."""
    eng._require_started()
    if eng._episodic is None:
        raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
    ns = validate_namespace(namespace)
    return eng._inflate_all(await eng._episodic.timeline(ns, start, end), ns)


async def sessions(
    eng: Any, namespace: str = "default", gap_minutes: int = constants.SESSION_GAP_MINUTES
) -> list[Session]:
    """Derived session boundaries over the episodic timeline (M13.2)."""
    eng._require_started()
    if eng._episodic is None:
        raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
    return await eng._episodic.sessions(validate_namespace(namespace), gap_minutes)
