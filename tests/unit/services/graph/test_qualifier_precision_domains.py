"""RM-97 precision-aware valid time, RM-91 hierarchical domains, RM-88 caller scope layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from memspine.services.graph.base import GraphEdge
from memspine.services.graph.qualifiers import (
    CallerVisibility,
    EdgeQualifiers,
    QualifierFilter,
    TimePrecision,
    with_qualifiers,
)


def _d(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


# ------------------------------------------------------------------ RM-97: precision periods

#: precision -> (stored bound, first instant of its period, first instant after it)
PERIODS = {
    TimePrecision.YEAR: (_d(2023, 5, 7, 10, 30), _d(2023, 1, 1), _d(2024, 1, 1)),
    TimePrecision.MONTH: (_d(2023, 5, 7, 10, 30), _d(2023, 5, 1), _d(2023, 6, 1)),
    TimePrecision.DAY: (_d(2023, 5, 7, 10, 30), _d(2023, 5, 7), _d(2023, 5, 8)),
    TimePrecision.HOUR: (_d(2023, 5, 7, 10, 30), _d(2023, 5, 7, 10), _d(2023, 5, 7, 11)),
    TimePrecision.MINUTE: (
        _d(2023, 5, 7, 10, 30, 45),
        _d(2023, 5, 7, 10, 30),
        _d(2023, 5, 7, 10, 31),
    ),
}
TICK = timedelta(microseconds=1)


@pytest.mark.parametrize("precision", list(PERIODS))
def test_valid_from_covers_its_whole_period(precision: TimePrecision) -> None:
    bound, start, _end = PERIODS[precision]
    q = EdgeQualifiers(valid_from=bound, valid_open_ended=True, valid_precision=precision)
    assert q.valid_at(start) is True  # the first instant of the stated period is covered
    assert q.valid_at(start - TICK) is False
    assert QualifierFilter(valid_at=start).matches(q)
    # without a precision the bound is an instant: the period start is before it
    exact = EdgeQualifiers(valid_from=bound, valid_open_ended=True)
    assert exact.valid_at(start) is (start >= bound)


@pytest.mark.parametrize("precision", list(PERIODS))
def test_valid_to_covers_its_whole_period(precision: TimePrecision) -> None:
    bound, _start, end = PERIODS[precision]
    q = EdgeQualifiers(
        valid_from=bound - timedelta(days=800), valid_to=bound, valid_precision=precision
    )
    assert q.valid_at(end - TICK) is True  # last instant of the end period
    assert q.valid_at(end) is False
    window = QualifierFilter(valid_window=(end - TICK, end + timedelta(days=1)))
    assert window.matches(q)
    assert not QualifierFilter(valid_window=(end, end + timedelta(days=1))).matches(q)


@pytest.mark.parametrize("precision", list(PERIODS))
def test_approximate_boundary_is_unknown(precision: TimePrecision) -> None:
    bound, start, end = PERIODS[precision]
    q = EdgeQualifiers(
        valid_from=bound,
        valid_open_ended=True,
        valid_precision=precision,
        valid_approximate=True,
    )
    assert q.valid_at(start) is None  # inside the start period: unknown
    assert q.valid_at(end - TICK) is None
    assert q.valid_at(start - TICK) is False  # well outside stays definite
    assert q.valid_at(end) is True  # past the boundary period: certain
    assert not QualifierFilter(valid_at=start).matches(q)  # unknown fails allow-filters
    assert QualifierFilter(valid_at=end).matches(q)


def test_approximate_window_needs_the_certain_core() -> None:
    q = EdgeQualifiers(
        valid_from=_d(2023, 5, 7),
        valid_to=_d(2023, 9, 2),
        valid_precision=TimePrecision.MONTH,
        valid_approximate=True,
    )
    # the certain core is [June, September): a window inside May only is unknown
    assert q.valid_overlaps(_d(2023, 5, 1), _d(2023, 5, 20)) is None
    assert q.valid_overlaps(_d(2023, 6, 10), _d(2023, 6, 11)) is True
    assert q.valid_overlaps(_d(2023, 9, 10), _d(2023, 9, 11)) is None  # end period
    assert q.valid_overlaps(_d(2023, 10, 1), _d(2023, 11, 1)) is False
    assert q.valid_overlaps(_d(2023, 1, 1), _d(2023, 5, 1)) is False


def test_same_period_bounds_with_approximation_are_unknown() -> None:
    q = EdgeQualifiers(
        valid_from=_d(2023, 5, 3),
        valid_to=_d(2023, 5, 20),
        valid_precision=TimePrecision.MONTH,
        valid_approximate=True,
    )
    assert q.valid_at(_d(2023, 5, 10)) is None
    assert q.valid_overlaps(_d(2023, 5, 1), _d(2023, 6, 1)) is None


def test_december_and_timezone_periods() -> None:
    q = EdgeQualifiers(
        valid_from=_d(2022, 1, 1),
        valid_to=_d(2022, 12, 9),
        valid_precision=TimePrecision.MONTH,
    )
    assert q.valid_at(_d(2022, 12, 31, 23, 59)) is True
    assert q.valid_at(_d(2023, 1, 1)) is False
    # a day bound is the day on the bound's own clock (UTC+5 here)
    plus5 = timezone(timedelta(hours=5))
    local = EdgeQualifiers(
        valid_from=datetime(2023, 5, 7, 3, 0, tzinfo=plus5),
        valid_open_ended=True,
        valid_precision=TimePrecision.DAY,
    )
    local_midnight = datetime(2023, 5, 7, tzinfo=plus5)  # = 2023-05-06T19:00Z
    assert local.valid_at(local_midnight) is True
    assert local.valid_at(local_midnight - TICK) is False


def test_unknown_end_stays_unknown() -> None:
    q = EdgeQualifiers(valid_from=_d(2023, 5, 7), valid_precision=TimePrecision.MONTH)
    assert q.valid_at(_d(2023, 7, 1)) is None
    assert q.valid_at(_d(2023, 4, 1)) is False


# ------------------------------------------------------------------ RM-91: hierarchical domains


def test_domain_filter_matches_descendants_on_segment_boundary() -> None:
    flt = QualifierFilter(topical_domains=frozenset({"health"}))
    assert flt.matches(EdgeQualifiers(topical_domain="health"))
    assert flt.matches(EdgeQualifiers(topical_domain="health/condition"))
    assert flt.matches(EdgeQualifiers(topical_domain="health/condition/chronic"))
    assert not flt.matches(EdgeQualifiers(topical_domain="healthcare"))
    assert not flt.matches(EdgeQualifiers(topical_domain="work"))
    assert not flt.matches(EdgeQualifiers())  # unknown never passes
    narrow = QualifierFilter(topical_domains=frozenset({"health/condition"}))
    assert not narrow.matches(EdgeQualifiers(topical_domain="health"))  # no upward match
    assert narrow.matches(EdgeQualifiers(topical_domain="health/condition"))


def test_domain_ids_are_validated() -> None:
    with pytest.raises(ValidationError):
        QualifierFilter(topical_domains=frozenset({"Health"}))
    with pytest.raises(ValidationError):
        QualifierFilter(topical_domains=frozenset({"health/"}))
    with pytest.raises(ValidationError):
        EdgeQualifiers(topical_domain="health//x")
    q = EdgeQualifiers(topical_domain="health/allergy", topical_domain_source="predicate")
    assert q.to_properties()["topical_domain_source"] == "predicate"


# ------------------------------------------------------------------ RM-88: caller scope layer


def _edge(scope: str | None) -> GraphEdge:
    return GraphEdge("x", "y", "knows", with_qualifiers({}, EdgeQualifiers(scope=scope)))


def test_caller_scope_layer() -> None:
    caller = CallerVisibility(namespace="b", granted_scopes=frozenset({"a"}))
    assert caller.admits(_edge("b"))  # own scope
    assert caller.admits(_edge("a"))  # held grant
    assert not caller.admits(_edge("c"))  # no grant
    assert not caller.admits(_edge(None))  # unknown scope never passes
    grantor = CallerVisibility(namespace="a")
    assert not grantor.admits(_edge("b"))  # F13: a grantee-scoped edge is not the grantor's
