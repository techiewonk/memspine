"""Read path: timeview moved out of the engine (F-10b)."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from memspine.config import constants
from memspine.core.read_filters import (
    active_as_of,
    active_date_filter,
    active_record_scope,
)
from memspine.exceptions import MemspineError
from memspine.memories.episodic.sessions import Session
from memspine.observability import tap
from memspine.read import timeview as tv
from memspine.read.pipeline.state import (
    _FOCAL_ENTITY,
    _PASSIVE_SCOPE,
    _READ_SNAPSHOT,
    CUE_TAG,
    _PassiveScope,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
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
    if date_filter is None and scope is None and active_as_of() is None:
        return None
    listed = await eng._records(ns)
    snapshot = _timeview_allowed(listed, date_filter, scope)
    if snapshot is not None:
        return snapshot, len(listed)
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
    return eng._inflate_all(await eng._episodic.timeline(ns, start, end), ns)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def sessions(
    eng: Any, namespace: str = "default", gap_minutes: int = constants.SESSION_GAP_MINUTES
) -> list[Session]:
    """Derived session boundaries over the episodic timeline (M13.2)."""
    eng._require_started()
    if eng._episodic is None:
        raise MemspineError("episodic memory not enabled — set memories.episodic.enabled: true")
    return await eng._episodic.sessions(validate_namespace(namespace), gap_minutes)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


_MICRO = timedelta(microseconds=1)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _event_start(r: MemoryRecord) -> TemporalRef:
    """The event start with the record's own precision, approximate flag and basis (F-02b) when it
    has them; otherwise an exact, explicit instant as before."""
    basis = r.event_basis or "explicit"
    anchor = _aware(r.source_at) if r.source_at is not None else None
    if basis == "source_time" and anchor is None:
        basis = "explicit"  # a source-time basis needs the anchor it came from
    if basis in ("relation", "none"):
        basis = "explicit"  # the record keeps no relation; the date itself is the start
    return TemporalRef(
        at=_aware(r.valid_from),
        precision=r.event_precision if r.event_precision not in (None, "unknown") else "instant",
        approximate=r.event_approximate,
        basis=basis,
        anchor=anchor if basis == "source_time" else None,
    )


def _as_envelope(r: MemoryRecord) -> AssertionEnvelope:
    """R3-14 adapter: a record as the time view sees it. Event time = ``valid_from`` /
    ``invalid_at`` (else ``valid_to``, an inferred end); recorded time = ``recorded_at``."""
    start = _event_start(r)
    end_at = r.invalid_at or r.valid_to
    end = (
        TemporalRef(at=_aware(end_at), precision="instant", basis="inferred")
        if end_at is not None
        else None
    )
    status = (
        "active"
        if r.status is RecordStatus.ACTIVATED
        else "superseded"
        if r.status is RecordStatus.ARCHIVED
        else "hidden"
    )
    time = TimeAxes(
        event=Interval(start=start, end=end),
        # F-02b: knowledge time is when it was said and when it was stored; ``source_at`` only
        # when the record has it (never guessed from ``valid_from``).
        source_time=_aware(r.source_at) if r.source_at is not None else None,
        recorded_at=_aware(r.recorded_at),
    )
    return AssertionEnvelope(
        write_id=r.record_id,
        assertion_key=AssertionKey(
            src=r.entity or "_", rel=r.attribute or "_", dst="_", kind="fact"
        ),
        occurrence_id=r.record_id,
        record_id=r.record_id,
        scope=OwnerScope(tenant=r.namespace or "_", owner="_"),
        text=r.content or "_",
        kind="fact",
        subject=Mention(surface=r.entity, mention_id=f"m:{r.record_id}") if r.entity else None,
        attribute=r.attribute,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=r.record_id, source_version=1, seg_index=0),
                source_family="user",
            )
        ],
        assessment=Assessment(
            source_trust=0.9,
            binding="bound",
            modality="asserted",
            time_precision=time.event_precision,  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
            support=Support(),
        ),
        src_class="user_stated",
        status=status,  # type: ignore[arg-type]
    )


def _timeview_allowed(listed: list[MemoryRecord], date_filter: Any, scope: Any) -> set[str] | None:
    """R3-14 (``read.timeview``): the allow-list under the three-valued time view, or
    None when the read has no as-of / known-at bound (the legacy path then runs).

    The as-of scope's own ``valid_from_before`` and a ``recorded_before`` bound are taken
    over by the view (event time and knowledge time); every other date bound still
    applies. Undecidable records stay in (flagged) instead of being dropped."""
    as_of = active_as_of()
    known_bound = date_filter.recorded_before if date_filter is not None else None
    if date_filter is not None and date_filter.mode == "or":
        return None
    if as_of is None and known_bound is None:
        return None
    if as_of is not None and known_bound is not None:
        view = tv.TimeView.both(as_of, known_bound - _MICRO)
    elif as_of is not None:
        view = tv.TimeView.at_valid(as_of)
    else:
        view = tv.TimeView.at_known(known_bound - _MICRO)  # type: ignore[arg-type,operator]  # CI-GREEN: pre-existing typing debt
    rest = date_filter
    if date_filter is not None:
        drop: dict[str, None] = {"recorded_before": None}
        if as_of is not None and date_filter.valid_from_before == as_of + _MICRO:
            drop["valid_from_before"] = None
        rest = replace(date_filter, **drop)
        if not rest.bounds():
            rest = None
    result = tv.apply(view, [_as_envelope(r) for r in listed])
    kept = {h.record_id for h in result.hits}
    undecidable = sum(1 for h in result.hits if h.uncertain_time)
    if tap.enabled():
        tap.event(
            "read.timeview",
            mode=view.mode,
            n_in=len(kept) - undecidable,
            n_out=len(result.out_of_window),
            n_undecidable=undecidable,
        )
    return {
        r.record_id
        for r in listed
        if CUE_TAG in r.tags
        or (
            r.record_id in kept
            and (rest is None or rest.matches(r))
            and (scope is None or scope.matches(r))
        )
    }
