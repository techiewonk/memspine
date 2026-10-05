"""Session lifecycle (#53, ADR-037): ACTIVE / PASSIVE archival of idle sessions.

A *session* here is the explicit conversation id a writer stamps on its turns
(``write_messages(session_id=...)`` / ``write_episode``, stored as the episodic
record's ``source.message_id``). A session whose newest write is older than
``memories.episodic.policies.sessions.passive_after`` is marked PASSIVE by the
sleep cycle: its records stay stored and retrievable, but default reads leave them
out. A new write to the session reopens it.

The decision is made once, in the ``session_lifecycle`` sleep stage, and recorded
as a ``memory.session`` event listing the member records; the record projector sets
``scoring.passive`` from it. Replaying the log therefore gives the same passive set
whatever the wall clock says at rebuild time. Pure functions only; no I/O here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConfigError

__all__ = [
    "SessionTransition",
    "parse_duration",
    "passive_after",
    "plan_transitions",
    "session_of",
]

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: object) -> timedelta | None:
    """``None`` (off), a number of seconds, or ``"<n><s|m|h|d|w>"`` (``"30d"``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"sessions.passive_after must be a duration, got {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        match = _DURATION.match(str(value))
        if match is None:
            raise ConfigError(
                f"sessions.passive_after must be seconds or '<n>[s|m|h|d|w]', got {value!r}"
            )
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if seconds <= 0:
        raise ConfigError(f"sessions.passive_after must be positive, got {value!r}")
    return timedelta(seconds=seconds)


def passive_after(episodic_policies: dict[str, object]) -> timedelta | None:
    """``memories.episodic.policies.sessions.passive_after`` as a timedelta, or None."""
    sessions = episodic_policies.get("sessions")
    if sessions is None:
        return None
    if not isinstance(sessions, dict):
        raise ConfigError("memories.episodic.policies.sessions must be a mapping")
    unknown = set(sessions) - {"passive_after"}
    if unknown:
        raise ConfigError(
            f"memories.episodic.policies.sessions: unknown key(s) {sorted(unknown)} "
            "(valid: passive_after)"
        )
    return parse_duration(sessions.get("passive_after"))


def session_of(record: MemoryRecord) -> str | None:
    """The explicit session id of an episodic record, else None."""
    if record.memory_type != "episodic":
        return None
    return record.source.message_id or None


@dataclass
class SessionTransition:
    """One session changing state, with the member records the event will list."""

    session_id: str
    state: str  # "passive" | "active"
    record_ids: list[str] = field(default_factory=list)
    last_write: datetime | None = None


def plan_transitions(
    records: Iterable[MemoryRecord], now: datetime, horizon: timedelta
) -> list[SessionTransition]:
    """The sessions whose state must change at ``now``.

    A session goes passive when its newest ``recorded_at`` is at least ``horizon``
    old and some live member is not passive yet; it goes active again when it is
    younger than that and some member is still passive (a write that reopened it
    in another process, or a shortened horizon). Deleted records are ignored.
    Sorted by session id, so the event order is deterministic."""
    members: dict[str, list[MemoryRecord]] = {}
    for record in records:
        sid = session_of(record)
        if sid is None or record.status is RecordStatus.DELETED:
            continue
        members.setdefault(sid, []).append(record)
    out: list[SessionTransition] = []
    for sid in sorted(members):
        group = members[sid]
        last = max(r.recorded_at for r in group)
        idle = now - last >= horizon
        ids = sorted(r.record_id for r in group)
        if idle and any(not r.scoring.passive for r in group):
            out.append(SessionTransition(sid, "passive", ids, last))
        elif not idle and any(r.scoring.passive for r in group):
            out.append(SessionTransition(sid, "active", ids, last))
    return out
