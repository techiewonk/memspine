from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.window import WindowSettings, WindowTurn
from memspine.derive.segmenter import SegmentationSettings, StreamSegmenter


def _turn(record_id: str, when: datetime, *, session: str = "s") -> WindowTurn:
    return WindowTurn(record_id, record_id, session, when, None, 1)


def test_late_arrival_isolated_from_newer_open_window() -> None:
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    segmenter = StreamSegmenter(
        WindowSettings(max_turns=6, max_tokens=1500),
        segmentation=SegmentationSettings(gap_s=3600),
    )
    assert segmenter.add(_turn("newer", now)) == []
    closed = segmenter.add(_turn("historical", now - timedelta(seconds=1)))
    assert len(closed) == 1
    assert closed[0].reason == "late_arrival"
    assert closed[0].record_ids == ["newer"]
    assert [turn.record_id for turn in segmenter.open_turns] == ["historical"]


def test_session_boundary_wins_before_late_arrival_reason() -> None:
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    segmenter = StreamSegmenter(WindowSettings(max_turns=6, max_tokens=1500))
    segmenter.add(_turn("first", now, session="a"))
    closed = segmenter.add(_turn("second", now - timedelta(days=1), session="b"))
    assert [window.reason for window in closed] == ["session_end"]
