"""Episodic sessions (M13.2): boundary detection + temporal helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.records import MemoryRecord
from memspine.memories.episodic.sessions import detect_sessions
from memspine.memories.episodic.timeops import sort_timeline, within

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
GAP = timedelta(minutes=30)


def rec(minutes_ago: float) -> MemoryRecord:
    return MemoryRecord(
        namespace="agent/a",
        memory_type="episodic",
        content=f"event {minutes_ago}",
        valid_from=NOW - timedelta(minutes=minutes_ago),
    )


def test_detect_sessions_splits_on_silence_gaps() -> None:
    # Two bursts: [-120, -115, -110] and [-40, -35], separated by 70 min.
    records = [rec(120), rec(115), rec(110), rec(40), rec(35)]
    sessions = detect_sessions(records, GAP)
    assert [session.size for session in sessions] == [3, 2]
    assert sessions[0].end < sessions[1].start


def test_session_keys_are_stable_as_the_tail_grows() -> None:
    burst = [rec(120), rec(115), rec(110)]
    key_before = detect_sessions(burst, GAP)[0].session_key
    grown = [*burst, rec(105)]  # a record joins the same session's tail
    key_after = detect_sessions(grown, GAP)[0].session_key
    assert key_before == key_after  # consolidation idempotence depends on this


def test_detect_sessions_orders_by_event_time_not_insertion() -> None:
    records = [rec(35), rec(120), rec(110), rec(40), rec(115)]  # shuffled
    sessions = detect_sessions(records, GAP)
    assert [session.size for session in sessions] == [3, 2]


def test_within_is_half_open_interval() -> None:
    records = [rec(60), rec(30), rec(10)]
    window = within(records, start=NOW - timedelta(minutes=30), end=NOW)
    assert [record.content for record in window] == ["event 30", "event 10"]


def test_sort_timeline_uses_event_time() -> None:
    backfilled = rec(1000)  # recorded now, happened long ago
    fresh = rec(1)
    assert sort_timeline([fresh, backfilled])[0] is backfilled
