"""RM-41: the typed EdgeQualifiers model and QualifierFilter semantics (unknown never passes)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memspine.services.graph.base import GraphEdge
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    ClaimState,
    EdgeQualifiers,
    LifecycleOutcome,
    LifecycleStatus,
    Provenance,
    QualifierFilter,
    edge_qualifiers,
    with_qualifiers,
)


def _t(year: int, month: int = 1) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def test_every_field_defaults_to_unknown() -> None:
    q = EdgeQualifiers()
    assert q.to_properties() == {}
    assert all(value is None for value in q.model_dump().values())


def test_tiers_are_all_present() -> None:
    fields = set(EdgeQualifiers.model_fields)
    p1 = {
        "valid_from", "valid_to", "known_at", "assertion_modality", "polarity",
        "lifecycle_status", "claim_state", "provenance", "trust_tier", "subject_binding",
        "scope", "grants", "erasure_state", "event_role", "vocabulary_pack",
    }  # fmt: skip
    p2 = {
        "topical_domain", "sensitivity", "context_location", "applicability_condition",
        "extraction_confidence", "extraction_method", "recurrence_series_id", "completeness",
    }  # fmt: skip
    assert p1 | p2 | {"independent_support_count"} <= fields


def test_validation_rejects_incoherent_qualifiers() -> None:
    with pytest.raises(ValidationError, match="valid_to must be after"):
        EdgeQualifiers(valid_from=_t(2026), valid_to=_t(2025))
    with pytest.raises(ValidationError, match="open-ended"):
        EdgeQualifiers(valid_from=_t(2026), valid_to=_t(2027), valid_open_ended=True)
    with pytest.raises(ValidationError, match="only allowed when status is closed"):
        EdgeQualifiers(
            lifecycle_status=LifecycleStatus.OPEN, lifecycle_outcome=LifecycleOutcome.DONE
        )
    EdgeQualifiers(lifecycle_status=LifecycleStatus.CLOSED, lifecycle_outcome=LifecycleOutcome.DONE)
    with pytest.raises(ValidationError):
        EdgeQualifiers(extraction_confidence=1.5)
    with pytest.raises(ValidationError):
        EdgeQualifiers(independent_support_count=-1)
    with pytest.raises(ValidationError):
        EdgeQualifiers.model_validate({"no_such_qualifier": 1})
    with pytest.raises(ValidationError, match="valid_window end"):
        QualifierFilter(valid_window=(_t(2026), _t(2025)))


def test_valid_time_is_half_open_and_unknown_end_is_unknown() -> None:
    closed = EdgeQualifiers(valid_from=_t(2026), valid_to=_t(2026, 6))
    assert closed.valid_at(_t(2026)) is True
    assert closed.valid_at(_t(2026, 6)) is False  # half-open end
    assert closed.valid_at(_t(2025)) is False
    open_ended = EdgeQualifiers(valid_from=_t(2026), valid_open_ended=True)
    assert open_ended.valid_at(_t(2030)) is True
    unknown_end = EdgeQualifiers(valid_from=_t(2026))
    assert unknown_end.valid_at(_t(2030)) is None
    assert unknown_end.valid_at(_t(2025)) is False  # before a known start is definite
    assert EdgeQualifiers().valid_at(_t(2026)) is None
    assert closed.valid_overlaps(_t(2026, 5), _t(2026, 7)) is True
    assert closed.valid_overlaps(_t(2026, 6), _t(2026, 7)) is False
    assert unknown_end.valid_overlaps(_t(2027), _t(2028)) is None


def test_properties_round_trip_and_malformed_reads_unknown() -> None:
    q = EdgeQualifiers(
        valid_from=_t(2026),
        claim_state=ClaimState.CURRENT,
        provenance=Provenance(origin_type="email", source_span=(3, 9)),
        grants=("a", "b"),
    )
    props = with_qualifiers({"weight": 0.4}, q)
    assert props["weight"] == 0.4
    assert edge_qualifiers(props) == q
    assert edge_qualifiers({}) is None
    assert edge_qualifiers({QUALIFIERS_PROP: "nope"}) is None
    assert edge_qualifiers({QUALIFIERS_PROP: {"claim_state": "invented"}}) is None


def test_empty_filter_passes_unqualified_but_any_constraint_fails_it() -> None:
    assert QualifierFilter().is_empty
    assert QualifierFilter().matches(None)
    for flt in (
        QualifierFilter(valid_at=_t(2026)),
        QualifierFilter(known_as_of=_t(2026)),
        QualifierFilter(topical_domains=frozenset({"work"})),
        QualifierFilter(origin_types=frozenset({"email"})),
        QualifierFilter(required_grants=frozenset({"team"})),
        QualifierFilter(min_extraction_confidence=0.0),
        QualifierFilter(min_independent_support=0),
    ):
        assert not flt.matches(None)
        assert not flt.matches(EdgeQualifiers())


def test_filter_matches_and_rejects_on_values() -> None:
    q = EdgeQualifiers(
        topical_domain="work",
        claim_state=ClaimState.SUPERSEDED,
        known_at=_t(2026, 3),
        provenance=Provenance(origin_type="email"),
        grants=("team",),
        extraction_confidence=0.7,
        independent_support_count=1,
    )
    assert QualifierFilter(topical_domains=frozenset({"work", "home"})).matches(q)
    assert not QualifierFilter(topical_domains=frozenset({"home"})).matches(q)
    assert not QualifierFilter(claim_states=frozenset({ClaimState.CURRENT})).matches(q)
    assert QualifierFilter(known_as_of=_t(2026, 3)).matches(q)
    assert not QualifierFilter(known_as_of=_t(2026, 2)).matches(q)
    assert QualifierFilter(origin_types=frozenset({"email"})).matches(q)
    assert not QualifierFilter(principals=frozenset({"user"})).matches(q)  # principal unknown
    assert QualifierFilter(required_grants=frozenset({"team"})).matches(q)
    assert not QualifierFilter(required_grants=frozenset({"team", "org"})).matches(q)
    assert QualifierFilter(min_extraction_confidence=0.7).matches(q)
    assert not QualifierFilter(min_extraction_confidence=0.8).matches(q)
    assert not QualifierFilter(min_independent_support=2).matches(q)
    edge = GraphEdge("a", "b", "r", with_qualifiers({}, q))
    assert QualifierFilter(topical_domains=frozenset({"work"})).admits(edge)
    assert not QualifierFilter(topical_domains=frozenset({"work"})).admits(GraphEdge("a", "b", "r"))
