"""F5 / ADR-090 (DS-08): naive times are never given a zone, and ties keep session order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from memspine.api.engine import Engine
from memspine.config.schema import ReadConfig

NAIVE = datetime(2023, 5, 8, 23, 30)  # 23:30 wall clock, no zone


def _engine(**read: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


def test_no_naive_timezone_key_and_no_sequence() -> None:
    """ADR-090 (DS-08): ``read.naive_timezone`` is removed; no zone is ever guessed."""
    read = ReadConfig()
    assert not hasattr(read, "naive_timezone") and read.session_sequence is False
    with pytest.raises(ValueError, match="naive_timezone"):
        ReadConfig(naive_timezone="UTC")


async def test_naive_valid_from_is_refused_by_name() -> None:
    eng = _engine()
    await eng.start()
    try:
        with pytest.raises(ValueError, match="has no timezone"):
            await eng.write("first", namespace="n", memory_type="episodic", valid_from=NAIVE)
        with pytest.raises(ValueError, match="has no timezone"):
            await eng.write("fact", namespace="n", valid_from=NAIVE)
        with pytest.raises(ValueError, match="has no timezone"):
            await eng.ingest_messages(
                [{"role": "user", "content": "hello"}], namespace="n", valid_from=NAIVE
            )
    finally:
        await eng.stop()


async def test_aware_datetime_is_untouched_and_silent() -> None:
    eng = _engine()
    await eng.start()
    try:
        stamp = datetime(2023, 5, 8, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        rec = await eng.write("x", namespace="n", memory_type="episodic", valid_from=stamp)
        assert rec.valid_from == stamp
    finally:
        await eng.stop()


async def test_naive_message_time_stays_civil_text_and_aware_resolves() -> None:
    eng = _engine(skip_defaulted_dates=True)
    await eng.start()
    try:
        civil = await eng.ingest_messages(
            [
                {"role": "user", "content": "hello there", "created_at": "2023-05-08T23:30:00"},
                {"role": "user", "content": "a day note", "created_at": "2023-05-08"},
            ],
            namespace="n",
        )
        for rec, text, precision in zip(
            civil, ("2023-05-08T23:30:00", "2023-05-08"), ("instant", "day"), strict=True
        ):
            message = rec.source.message
            assert message["created_at"] == text
            assert message["source_clock"]["precision"] == "unknown"
            assert message["source_clock"]["text_precision"] == precision
            assert rec.source_at is None  # no resolved instant, no guessed zone
            assert "ts_defaulted" in rec.tags
        declared = await eng.ingest_messages(
            [
                {
                    "role": "user",
                    "content": "in tokyo",
                    "created_at": "2023-05-08T23:30:00",
                    "tz": "Asia/Tokyo",
                }
            ],
            namespace="n",
        )
        assert declared[0].valid_from.astimezone(UTC) == datetime(2023, 5, 8, 14, 30, tzinfo=UTC)
        out = await eng.ingest_messages(
            [{"role": "user", "content": "hi again", "created_at": "2023-05-08T23:30:00+00:00"}],
            namespace="n",
        )
        assert out[0].valid_from == datetime(2023, 5, 8, 23, 30, tzinfo=UTC)
    finally:
        await eng.stop()


_SESSION = [{"role": "user", "content": f"turn number {i} about topic {i}"} for i in range(6)]


async def test_session_sequence_keeps_write_order_when_stamps_tie() -> None:
    day = datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    eng = _engine(session_sequence=True)
    await eng.start()
    try:
        recs = await eng.ingest_messages(_SESSION, namespace="n", session_id="s1", valid_from=day)
        stamps = [r.valid_from for r in recs]
        assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
        assert stamps[0] == day and stamps[-1] == day + timedelta(microseconds=5)
        assert {s.date() for s in stamps} == {day.date()}  # sub-day only, the date is unchanged
        # a later call of the same session at the same stamp continues the sequence
        more = await eng.ingest_messages(
            [{"role": "user", "content": "one more turn"}],
            namespace="n",
            session_id="s1",
            valid_from=day,
        )
        assert more[0].valid_from == day + timedelta(microseconds=6)
        # the session read returns them in order
        got = await eng.conversation("n", "s1")
        assert [r.content for r in got][:6] == [m["content"] for m in _SESSION]
        # another session at the same stamp starts again at zero
        other = await eng.ingest_messages(
            [{"role": "user", "content": "elsewhere"}],
            namespace="n",
            session_id="s2",
            valid_from=day,
        )
        assert other[0].valid_from == day
    finally:
        await eng.stop()


async def test_default_stores_stamps_as_given() -> None:
    day = datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    eng = _engine()
    await eng.start()
    try:
        recs = await eng.ingest_messages(_SESSION, namespace="n", session_id="s1", valid_from=day)
        assert {r.valid_from for r in recs} == {day}
    finally:
        await eng.stop()
