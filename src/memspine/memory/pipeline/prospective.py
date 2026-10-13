"""Memory: prospective moved out of the engine (F-12)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from memspine.exceptions import ConflictError, MemspineError
from memspine.memories.prospective.watches import ProspectiveMemory, make_watch_record
from memspine.observability.logging import get_logger
from memspine.store.events import EventLogMode
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.scope import validate_namespace

_log = get_logger(__name__)


async def watch(
    eng: Any,
    content: str,
    namespace: str = "default",
    due_at: datetime | None = None,
    entity: str | None = None,
    attribute: str | None = None,
    actor: str = "user",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """Store a prospective watch (M13.8): ``content`` is what to do or
    remember when it fires — at ``due_at`` (bi-temporal reuse: the due
    time rides ``valid_from``) OR when the watched ``entity``/``attribute``
    fact key is invalidated by the M4 conflict ladder.

    A watch is a write like any other: it enters through the firewall
    gate (E1) — instruction-shaped watch content is quarantined and can
    never fire.
    """
    storage = eng._require_started()
    eng._require_prospective()  # enablement check — the write rides the normal door
    ns = validate_namespace(namespace)
    record = make_watch_record(
        ns,
        content,
        due_at=due_at,
        entity=entity,
        attribute=attribute,
        source=source or SourceInfo(role=actor),
    )
    # SF-1/ADR-018: an invalidation (target) watch reads M4 CONFLICT events
    # to fire; in ephemeral mode nothing is persisted to read, so it can
    # never fire. This is documented in ADR-016 but was runtime-silent —
    # warn once (the engine knows the mode; the pure builder does not).
    if (
        due_at is None
        and entity is not None
        and not eng._ephemeral_watch_warned
        and eng._config().event_log.mode is EventLogMode.EPHEMERAL
    ):
        eng._ephemeral_watch_warned = True
        _log.warning(
            "prospective.ephemeral_invalidation_never_fires",
            namespace=ns,
            detail="event_log.mode=ephemeral persists no CONFLICT events — "
            "invalidation (target) watches can never fire (ADR-016); "
            "due-time watches are unaffected",
        )
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await eng._write_locked(storage, ns, record, "prospective", actor)


async def due(
    eng: Any, namespace: str = "default", now: datetime | None = None
) -> list[MemoryRecord]:
    """Fired-but-unacknowledged watches (M13.8): due time reached or the
    watched fact key invalidated. Read-only and pull-based (ADR-016) —
    a fired watch stays here until ``acknowledge_watch`` archives it.
    ``now`` defaults to the current UTC instant; pass it explicitly to
    own time (tests always should)."""
    eng._require_started()
    prospective = eng._require_prospective()
    ns = validate_namespace(namespace)
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        # COR-1/ADR-018: watch due times are tz-aware (make_watch_record
        # rejects naive). Comparing an aware valid_from to a naive `now`
        # raises deep in the trigger — reject it loudly at the boundary.
        raise ConflictError("now must be timezone-aware (naive datetimes are ambiguous)")
    fired = await prospective.pending(ns, now)
    return eng._inflate_all(fired, ns)


async def acknowledge_watch(eng: Any, record_id: str, namespace: str = "default") -> MemoryRecord:
    """Acknowledge a fired watch: archived via a delta event
    (``reason="watch_fired"``), idempotent (M13.8)."""
    eng._require_started()
    prospective = eng._require_prospective()
    ns = validate_namespace(namespace)
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        return await prospective.acknowledge(record_id, namespace=ns)


def _require_prospective(eng: Any) -> ProspectiveMemory:
    if eng._prospective is None:
        raise MemspineError(
            "prospective memory not enabled — set memories.prospective.enabled: true"
        )
    return eng._prospective
