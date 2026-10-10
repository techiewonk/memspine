"""F5: naive datetimes are explicit (configurable zone, warning) and ties keep session order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from structlog.testing import capture_logs

from memspine.config.schema import ReadConfig
from memspine.engine import Engine

NAIVE = datetime(2023, 5, 8, 23, 30)  # 23:30 wall clock, no zone


def _engine(**read: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


def test_defaults_keep_utc_and_no_sequence() -> None:
    read = ReadConfig()
    assert read.naive_timezone == "UTC" and read.session_sequence is False


async def test_naive_datetime_defaults_to_utc_with_one_warning() -> None:
    eng = _engine()
    await eng.start()
    try:
        with capture_logs() as logs:
            a = await eng.write("first", namespace="n", memory_type="episodic", valid_from=NAIVE)
            await eng.write("second", namespace="n", memory_type="episodic", valid_from=NAIVE)
        assert a.valid_from == NAIVE.replace(tzinfo=UTC)
        warnings = [e for e in logs if e["event"] == "write.naive_datetime"]
        assert len(warnings) == 1 and warnings[0]["assumed_timezone"] == "UTC"
    finally:
        await eng.stop()


async def test_aware_datetime_is_untouched_and_silent() -> None:
    eng = _engine(naive_timezone="Asia/Kolkata")
    await eng.start()
    try:
        stamp = datetime(2023, 5, 8, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
        with capture_logs() as logs:
            rec = await eng.write("x", namespace="n", memory_type="episodic", valid_from=stamp)
        assert rec.valid_from == stamp
        assert not [e for e in logs if e["event"] == "write.naive_datetime"]
    finally:
        await eng.stop()


async def test_configured_zone_applies_to_naive_values_and_message_timestamps() -> None:
    eng = _engine(naive_timezone="America/New_York")
    await eng.start()
    try:
        rec = await eng.write("x", namespace="n", memory_type="episodic", valid_from=NAIVE)
        # 23:30 New York (EDT, UTC-4) is 03:30 UTC the next day: the zone matters near midnight
        assert rec.valid_from.astimezone(UTC) == datetime(2023, 5, 9, 3, 30, tzinfo=UTC)
        out = await eng.write_messages(
            [{"role": "user", "content": "hello there", "timestamp": "2023-05-08T23:30:00"}],
            namespace="n",
        )
        assert out[0].valid_from.astimezone(UTC) == datetime(2023, 5, 9, 3, 30, tzinfo=UTC)
        out = await eng.write_messages(
            [{"role": "user", "content": "hi again", "timestamp": "2023-05-08T23:30:00+00:00"}],
            namespace="n",
        )
        assert out[0].valid_from == datetime(2023, 5, 8, 23, 30, tzinfo=UTC)
    finally:
        await eng.stop()


async def test_unknown_zone_fails_loudly_on_a_naive_write() -> None:
    eng = _engine(naive_timezone="Not/AZone")
    await eng.start()
    try:
        with pytest.raises(Exception):  # noqa: B017 - zoneinfo's error type varies by OS
            await eng.write("x", namespace="n", memory_type="episodic", valid_from=NAIVE)
    finally:
        await eng.stop()


_SESSION = [{"role": "user", "content": f"turn number {i} about topic {i}"} for i in range(6)]


async def test_session_sequence_keeps_write_order_when_stamps_tie() -> None:
    day = datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    eng = _engine(session_sequence=True)
    await eng.start()
    try:
        recs = await eng.write_messages(_SESSION, namespace="n", session_id="s1", valid_from=day)
        stamps = [r.valid_from for r in recs]
        assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
        assert stamps[0] == day and stamps[-1] == day + timedelta(microseconds=5)
        assert {s.date() for s in stamps} == {day.date()}  # sub-day only, the date is unchanged
        # a later call of the same session at the same stamp continues the sequence
        more = await eng.write_messages(
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
        other = await eng.write_messages(
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
        recs = await eng.write_messages(_SESSION, namespace="n", session_id="s1", valid_from=day)
        assert {r.valid_from for r in recs} == {day}
    finally:
        await eng.stop()
