"""C3': query-side temporal interval parsing and the temporal / metadata legs."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import metadata_leg, query_interval, temporal_leg


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_absolute_forms() -> None:
    assert query_interval("what happened on 2023-05-07?") == (_d(2023, 5, 7), _d(2023, 5, 8))
    assert query_interval("on 7 May 2023") == (_d(2023, 5, 7), _d(2023, 5, 8))
    assert query_interval("on May 7th, 2023") == (_d(2023, 5, 7), _d(2023, 5, 8))
    assert query_interval("in May 2023") == (_d(2023, 5, 1), _d(2023, 6, 1))
    assert query_interval("in Dec 2023") == (_d(2023, 12, 1), _d(2024, 1, 1))
    assert query_interval("during 2022") == (_d(2022, 1, 1), _d(2023, 1, 1))


def test_relative_and_absent_are_not_resolved() -> None:
    assert query_interval("what did she do last week?") is None
    assert query_interval("where does Caroline live") is None
    assert query_interval("on 31 February 2023") is None


def _rec(content: str, when: datetime, entity: str | None = None) -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type="episodic", content=content, valid_from=when, entity=entity
    )


def test_temporal_leg_keeps_in_span_closest_first() -> None:
    recs = [
        _rec("early may", _d(2023, 5, 2)),
        _rec("mid may", _d(2023, 5, 16)),
        _rec("june", _d(2023, 6, 3)),
    ]
    hits = temporal_leg("what happened in May 2023", recs, top_k=5)
    assert [h.record_id for h in hits] == [recs[1].record_id, recs[0].record_id]
    assert temporal_leg("no date here", recs, top_k=5) == []


def test_metadata_leg_whole_word_newest_first() -> None:
    recs = [
        _rec("a", _d(2023, 1, 1), entity="Caroline"),
        _rec("b", _d(2023, 3, 1), entity="caroline"),
        _rec("c", _d(2023, 4, 1), entity="Carol"),
    ]
    hits = metadata_leg("Where did Caroline move?", recs, top_k=5)
    assert [h.record_id for h in hits] == [recs[1].record_id, recs[0].record_id]
