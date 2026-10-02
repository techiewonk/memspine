"""H15: lexical-cohesion topic segmentation within a session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.records import MemoryRecord
from memspine.memories.episodic.sessions import topic_segments

T0 = datetime(2023, 5, 1, tzinfo=UTC)


def _turns(texts: list[str]) -> list[MemoryRecord]:
    return [
        MemoryRecord(
            namespace="a", memory_type="episodic", content=t, valid_from=T0 + timedelta(minutes=i)
        )
        for i, t in enumerate(texts)
    ]


def test_splits_at_a_topic_shift() -> None:
    hiking = [f"hiking trail mountain boots summit weekend number {i}" for i in range(6)]
    baking = [f"baking bread sourdough oven flour starter recipe step {i}" for i in range(6)]
    segs = topic_segments(_turns(hiking + baking))
    assert len(segs) == 2
    assert all("hiking" in r.content for r in segs[0])
    assert all("baking" in r.content for r in segs[1])


def test_short_or_cohesive_sessions_stay_whole() -> None:
    same = [f"hiking trail mountain boots summit weekend number {i}" for i in range(12)]
    assert len(topic_segments(_turns(same))) == 1
    assert len(topic_segments(_turns(same[:5]))) == 1
