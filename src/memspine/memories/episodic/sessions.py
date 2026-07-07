"""Session boundary detection (M13.2): split a timeline on silence gaps.

Sessions are *derived*, never stored — the same episodic records always yield
the same sessions (deterministic-first, N6), so there is no session table to
drift from the log. ``session_key`` is content-addressed from the first
member, which stays stable as later records extend the session's tail.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from memspine.core.events import fingerprint_payload
from memspine.core.records import MemoryRecord
from memspine.memories.episodic.timeops import sort_timeline

__all__ = ["Session", "detect_sessions"]


class Session(BaseModel):
    """One detected burst of episodic activity."""

    session_key: str
    start: datetime
    end: datetime
    record_ids: list[str]

    @property
    def size(self) -> int:
        return len(self.record_ids)


def _session_key(members: list[MemoryRecord]) -> str:
    return fingerprint_payload({"member_ids": sorted(r.record_id for r in members)})


def detect_sessions(records: list[MemoryRecord], gap: timedelta) -> list[Session]:
    """Split event-time-ordered records into sessions wherever the silence
    between consecutive records is >= ``gap``."""
    timeline = sort_timeline(records)
    sessions: list[Session] = []
    current: list[MemoryRecord] = []
    for record in timeline:
        if current and record.valid_from - current[-1].valid_from >= gap:
            sessions.append(_build(current))
            current = []
        current.append(record)
    if current:
        sessions.append(_build(current))
    return sessions


def _build(members: list[MemoryRecord]) -> Session:
    return Session(
        session_key=_session_key(members),
        start=members[0].valid_from,
        end=members[-1].valid_from,
        record_ids=[record.record_id for record in members],
    )
