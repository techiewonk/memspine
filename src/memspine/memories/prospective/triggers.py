"""Prospective trigger rules (M13.8/ADR-016): which watches fire, and why.

Pure functions — no I/O, no clocks. ``now`` is always an explicit parameter
(the caller owns time), and invalidation is decided over the M4 CONFLICT
events the caller fetched, so the same inputs always fire the same watches.

A watch is an ordinary ``memory_type="prospective"`` record reusing existing
columns (no new DDL):

- **due watch** — ``entity is None``; its due time rides ``valid_from``
  (bi-temporal reuse: "becomes relevant at"). Fires once ``now`` reaches it.
- **target watch** — ``entity`` (and optionally ``attribute``) name the fact
  key it watches; ``attribute=None`` watches every attribute of the entity.
  Fires when an M4 CONFLICT event *changed the current truth* for that key
  (action ``updated`` or ``invalidated`` — a rejected or backfilled write
  left the active fact standing, so nothing invalidated) after the watch was
  recorded.

Only live watches fire: quarantined (E1) or non-ACTIVATED (acknowledged /
deleted) records never do.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus

__all__ = ["due_watches", "fired_watches", "invalidation_watches"]

#: CONFLICT actions that changed the current truth for a fact key (the
#: semantic store's ``_resolve_conflict`` payload vocabulary). ``rejected``
#: and ``added`` (backfill) leave the active fact standing — no invalidation.
_INVALIDATING_ACTIONS = frozenset({"updated", "invalidated"})


def _live_watch(record: MemoryRecord) -> bool:
    return (
        record.memory_type == "prospective"
        and record.status is RecordStatus.ACTIVATED
        and not record.quarantined
    )


def _ordered(fired: list[MemoryRecord]) -> list[MemoryRecord]:
    """Deterministic firing order: earliest relevance first, id tie-break."""
    return sorted(fired, key=lambda record: (record.valid_from, record.record_id))


def due_watches(records: Iterable[MemoryRecord], now: datetime) -> list[MemoryRecord]:
    """Live due watches whose due time (``valid_from``) has been reached.

    A fired watch stays in this set until it is acknowledged (archived) —
    pull-based delivery must be able to re-ask without losing anything.
    """
    return _ordered(
        [
            record
            for record in records
            if _live_watch(record) and record.entity is None and record.valid_from <= now
        ]
    )


def invalidation_watches(
    records: Iterable[MemoryRecord],
    conflict_events: Iterable[MemoryEvent],
) -> list[MemoryRecord]:
    """Live target watches whose watched (entity, attribute) was invalidated.

    A conflict counts when it is an ``EventKind.CONFLICT`` in the watch's
    namespace, its action changed the current truth (``updated`` /
    ``invalidated``), its fact key matches (``attribute=None`` on the watch
    matches any attribute of the entity), and it happened at-or-after the
    watch was recorded — history that predates the watch never fires it.
    """
    invalidated: list[tuple[str, str, str | None, datetime]] = []
    for event in conflict_events:
        if event.kind is not EventKind.CONFLICT:
            continue
        if str(event.payload.get("action", "")) not in _INVALIDATING_ACTIONS:
            continue
        fact_key = event.payload.get("fact_key")
        if not isinstance(fact_key, list | tuple) or len(fact_key) != 2:
            continue
        entity, attribute = fact_key
        if entity is None:
            continue
        invalidated.append(
            (event.namespace, str(entity), None if attribute is None else str(attribute), event.ts)
        )

    fired: list[MemoryRecord] = []
    for record in records:
        if not _live_watch(record) or record.entity is None:
            continue
        for namespace, entity, attribute, ts in invalidated:
            if namespace != record.namespace or entity != record.entity:
                continue
            if record.attribute is not None and attribute != record.attribute:
                continue
            if ts >= record.recorded_at:
                fired.append(record)
                break
    return _ordered(fired)


def fired_watches(
    records: Iterable[MemoryRecord],
    now: datetime,
    conflict_events: Iterable[MemoryEvent],
) -> list[MemoryRecord]:
    """Union of due-time and invalidation firings, deduplicated, ordered."""
    pool = list(records)
    by_id: dict[str, MemoryRecord] = {}
    for record in [*due_watches(pool, now), *invalidation_watches(pool, conflict_events)]:
        by_id.setdefault(record.record_id, record)
    return _ordered(list(by_id.values()))
