"""W8 (plan v3.2): speaker tags at write, the subject leg at read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import SPEAKER_PREFIX, speaker_leg, speaker_of

T0 = datetime(2023, 5, 1, tzinfo=UTC)
TURNS = [
    "Melanie: the pottery class was so relaxing",
    "Caroline: the pottery class was too crowded for me",
    "Melanie: we went camping with the kids",
    "Caroline: I joined a support group",
]
QUERY = "What did Caroline think of the pottery class?"


def test_speaker_of() -> None:
    assert speaker_of("Caroline: hi there") == "caroline"
    assert speaker_of("Mary Ann: hello") == "mary ann"
    assert speaker_of("no speaker here") is None
    assert speaker_of("Note: 3 items") == "note"  # any "Name:" prefix; a tag, not a gate


def test_speaker_leg_ranks_the_named_speakers_turns_by_overlap() -> None:
    recs = [
        MemoryRecord(
            namespace="a",
            memory_type="episodic",
            content=t,
            tags=[f"{SPEAKER_PREFIX}{speaker_of(t)}"],
            valid_from=T0 + timedelta(minutes=i),
        )
        for i, t in enumerate(TURNS)
    ]
    hits = speaker_leg(QUERY, recs, 5)
    by_id = {r.record_id: r.content for r in recs}
    assert [by_id[h.record_id] for h in hits] == [TURNS[1], TURNS[3]]
    assert speaker_leg("What happened at the lake?", recs, 5) == []


def test_keys_default_off() -> None:
    assert ReadConfig().subject_leg is False


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True, "policies": {"subject_tagging": True}}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    for i, text in enumerate(TURNS):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )
    return eng


async def test_subject_tagging_tags_turns_and_the_leg_ranks_the_right_speaker_first() -> None:
    eng = await _engine(subject_leg=True)
    try:
        records = await eng._require_started().list_records("a", "episodic")
        assert {t for r in records for t in r.tags} >= {
            f"{SPEAKER_PREFIX}caroline",
            f"{SPEAKER_PREFIX}melanie",
        }
        hits = await eng.search(QUERY, namespace="a", top_k=4)
        assert hits[0][0].content == TURNS[1]
    finally:
        await eng.stop()
