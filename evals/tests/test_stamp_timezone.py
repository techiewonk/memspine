"""F5: the adapter reads naive dataset stamps in an explicit zone and says so."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.systems import memspine_system as ms


@pytest.fixture(autouse=True)
def _reset_warning() -> None:
    ms._NAIVE_WARNED.clear()


def test_default_is_utc_as_written(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        got = ms.parse_turn_time("1:56 pm on 8 May, 2023")
        ms.parse_turn_time("2:00 pm on 8 May, 2023")
    assert got == datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    naive = [r for r in caplog.records if "naive wall-clock" in r.getMessage()]
    assert len(naive) == 1  # once, not per turn


def test_configured_zone_moves_the_instant_not_the_wall_clock() -> None:
    zone = ms.stamp_zone("Asia/Kolkata")
    got = ms.parse_turn_time("11:45 pm on 8 May, 2023", zone)
    assert got is not None and got.hour == 23 and got.day == 8  # wall clock kept
    assert got.astimezone(UTC) == datetime(2023, 5, 8, 18, 15, tzinfo=UTC)


def test_aware_iso_stamp_is_untouched_and_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        got = ms.parse_turn_time("2023-05-08T23:30:00+02:00", ms.stamp_zone("Asia/Tokyo"))
    assert got is not None and got.utcoffset().total_seconds() == 7200  # type: ignore[union-attr]
    assert not caplog.records


def test_question_date_uses_the_same_zone() -> None:
    zone = ms.stamp_zone("Europe/Paris")
    got = ms.parse_question_date("2023/05/30 (Tue) 23:40", zone)
    assert got is not None and got.tzinfo is not None
    assert got.astimezone(UTC) == datetime(2023, 5, 30, 21, 40, tzinfo=UTC)
    assert ms.parse_question_date("2023/05/30 (Tue) 23:40") == datetime(
        2023, 5, 30, 23, 40, tzinfo=UTC
    )


def test_unparseable_stamp_still_raises() -> None:
    from memspine_evals.contracts import Turn

    turn = Turn(turn_id="t1", session_id="s1", speaker="A", text="x", timestamp="sometime soon")
    with pytest.raises(ValueError, match="unparseable"):
        ms.parse_turn_stamp(turn, ms.stamp_zone("UTC"))


def test_cli_flag() -> None:
    assert build_parser().parse_args(["c0-1", "--dataset", "locomo"]).stamp_timezone == "UTC"
    args = build_parser().parse_args(
        ["c0-1", "--dataset", "locomo", "--stamp-timezone", "America/Chicago"]
    )
    assert args.stamp_timezone == "America/Chicago"
