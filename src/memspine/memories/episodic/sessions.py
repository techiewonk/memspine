"""Session boundary detection (M13.2): split a timeline on silence gaps.

Sessions are *derived*, never stored — the same episodic records always yield
the same sessions (deterministic-first, N6), so there is no session table to
drift from the log. ``session_key`` is content-addressed from the first
member, which stays stable as later records extend the session's tail.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise

from pydantic import BaseModel

from memspine.core.events import fingerprint_payload
from memspine.core.records import MemoryRecord
from memspine.memories.episodic.timeops import sort_timeline

__all__ = [
    "Session",
    "detect_sessions",
    "topic_segments",
]


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


def _words(record: MemoryRecord) -> set[str]:
    return {w for w in record.content.lower().split() if len(w) > 3}


def topic_segments(
    records: list[MemoryRecord], window: int = 3, threshold: float = 0.08, min_len: int = 4
) -> list[list[MemoryRecord]]:
    """H15: split one session at topic shifts (TextTiling-style lexical cohesion).

    At each gap between turns, cohesion is the Jaccard overlap between the words of the
    ``window`` turns before and after it. A gap becomes a boundary when its cohesion is a
    local minimum below ``threshold`` and both sides keep at least ``min_len`` turns.
    Deterministic; no model and no embeddings.
    """
    turns = sort_timeline(records)
    if len(turns) < 2 * min_len:
        return [turns]
    cohesion: list[float] = []
    for gap in range(1, len(turns)):
        left: set[str] = set().union(*(_words(r) for r in turns[max(0, gap - window) : gap]))
        right: set[str] = set().union(*(_words(r) for r in turns[gap : gap + window]))
        cohesion.append(len(left & right) / (len(left | right) or 1))
    cuts: list[int] = []
    for i, value in enumerate(cohesion):
        gap = i + 1
        lower = cohesion[i - 1] if i > 0 else 1.0
        upper = cohesion[i + 1] if i + 1 < len(cohesion) else 1.0
        if value < threshold and value <= lower and value <= upper:
            start = cuts[-1] if cuts else 0
            if gap - start >= min_len and len(turns) - gap >= min_len:
                cuts.append(gap)
    bounds = [0, *cuts, len(turns)]
    return [turns[a:b] for a, b in pairwise(bounds)]


def _build(members: list[MemoryRecord]) -> Session:
    return Session(
        session_key=_session_key(members),
        start=members[0].valid_from,
        end=members[-1].valid_from,
        record_ids=[record.record_id for record in members],
    )
