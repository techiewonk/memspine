"""Temporal helpers for episodic memory (M13.2). Pure functions, no I/O.

Episodic ordering is by ``valid_from`` (event time), not ``recorded_at``
(record time) — a backfilled memory of last week sorts into last week.
"""

from __future__ import annotations

from datetime import datetime

from memspine.core.records import MemoryRecord

__all__ = ["sort_timeline", "within"]


def sort_timeline(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """Chronological event-time order; record time breaks ties stably."""
    return sorted(records, key=lambda record: (record.valid_from, record.recorded_at))


def within(
    records: list[MemoryRecord],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MemoryRecord]:
    """Records whose event time falls in ``[start, end)``. Open bounds pass."""
    selected = []
    for record in records:
        if start is not None and record.valid_from < start:
            continue
        if end is not None and record.valid_from >= end:
            continue
        selected.append(record)
    return selected
