"""RM-86 / RM-87: sensitivity label sets, the three check states and exclusion filters."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    AssertionModality,
    ClaimState,
    EdgeQualifiers,
    ErasureState,
    Provenance,
    QualifierFilter,
    edge_qualifiers,
    with_qualifiers,
)


def _q(labels: set[str] | None, checked: bool | None, **kw: Any) -> EdgeQualifiers:
    return EdgeQualifiers(
        sensitivity=None if labels is None else frozenset(labels),  # type: ignore[arg-type]
        sensitivity_checked=checked,
        **kw,
    )


HEALTH_LOC = _q({"health", "location"}, True)
CONTACT = _q({"contact"}, True)
CHECKED_NONE = _q(set(), True)
NOT_CHECKED = _q(None, None)
INCOMPLETE_CONTACT = _q({"contact"}, False)
INCOMPLETE_HEALTH = _q({"health"}, False)


# ------------------------------------------------------------------ model coherence


def test_label_set_round_trips_sorted() -> None:
    q = EdgeQualifiers(
        sensitivity=frozenset({"location", "health"}),
        sensitivity_checked=True,
        sensitivity_labeller="memspine.sensitivity",
        sensitivity_labeller_version="rules-1",
        sensitivity_confidence={"health": 0.7, "location": 1.0},
    )
    props = with_qualifiers({}, q)
    assert props[QUALIFIERS_PROP]["sensitivity"] == ["health", "location"]  # type: ignore[index]
    assert edge_qualifiers(props) == q


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sensitivity": frozenset({"health"})},  # labels without a check state
        {"sensitivity_labeller": "x"},  # labeller without a check state
        {"sensitivity_checked": True},  # a check records its (maybe empty) set
        {
            "sensitivity": frozenset({"health"}),
            "sensitivity_checked": True,
            "sensitivity_confidence": {"contact": 0.9},
        },
        {"sensitivity": frozenset({"religion"}), "sensitivity_checked": True},  # unknown label
    ],
)
def test_incoherent_sensitivity_is_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EdgeQualifiers(**kwargs)


# ------------------------------------------------------------------ allow-set: ANY label


def test_allow_set_matches_any_label() -> None:
    flt = QualifierFilter(sensitivities=frozenset({"location"}))
    assert flt.matches(HEALTH_LOC)
    assert not flt.matches(CONTACT)
    assert not flt.matches(CHECKED_NONE)  # none found never satisfies an allow-set
    assert not flt.matches(NOT_CHECKED)  # unknown never passes an allow-set
    assert not flt.matches(None)


# ------------------------------------------------------------------ exclusion: three states


@pytest.mark.parametrize(
    ("qualifiers", "unchecked", "expected"),
    [
        (HEALTH_LOC, "fail", False),  # labelled match: dropped
        (HEALTH_LOC, "pass", False),  # ...whatever the unchecked rule
        (CONTACT, "fail", True),  # labelled, but not excluded
        (CHECKED_NONE, "fail", True),  # checked_none passes
        (NOT_CHECKED, "fail", False),  # not checked: caller decides (default fail)
        (NOT_CHECKED, "pass", True),
        (None, "fail", False),  # an unqualified edge is not checked
        (None, "pass", True),
        (INCOMPLETE_CONTACT, "fail", False),  # incomplete check counts as not checked
        (INCOMPLETE_CONTACT, "pass", True),
        (INCOMPLETE_HEALTH, "pass", False),  # but a found excluded label always drops
    ],
)
def test_exclude_sensitivities(
    qualifiers: EdgeQualifiers | None, unchecked: str, expected: bool
) -> None:
    flt = QualifierFilter(
        exclude_sensitivities=frozenset({"health"}),
        unchecked_sensitivity=unchecked,  # type: ignore[arg-type]
    )
    assert flt.matches(qualifiers) is expected


def test_exclusion_and_allow_set_combine() -> None:
    flt = QualifierFilter(
        sensitivities=frozenset({"contact", "location"}),
        exclude_sensitivities=frozenset({"health"}),
    )
    assert flt.matches(CONTACT)
    assert not flt.matches(HEALTH_LOC)  # location allowed, health excluded: dropped


# ------------------------------------------------------------------ other exclusions


@pytest.mark.parametrize(
    ("field", "excluded", "known_hit", "known_miss"),
    [
        (
            "exclude_claim_states",
            {ClaimState.RETRACTED},
            {"claim_state": ClaimState.RETRACTED},
            {"claim_state": ClaimState.CURRENT},
        ),
        (
            "exclude_lifecycle_statuses",
            {"cancelled"},
            {"lifecycle_status": "cancelled"},
            {"lifecycle_status": "open"},
        ),
        (
            "exclude_assertion_modalities",
            {AssertionModality.HYPOTHETICAL},
            {"assertion_modality": AssertionModality.HYPOTHETICAL},
            {"assertion_modality": AssertionModality.ASSERTED},
        ),
        (
            "exclude_topical_domains",
            {"health"},
            {"topical_domain": "health"},
            {"topical_domain": "work"},
        ),
        (
            "exclude_origin_types",
            {"tool_output"},
            {"provenance": Provenance(origin_type="tool_output")},
            {"provenance": Provenance(origin_type="raw_message")},
        ),
    ],
)
def test_exclusion_fields(
    field: str, excluded: set[Any], known_hit: dict[str, Any], known_miss: dict[str, Any]
) -> None:
    flt = QualifierFilter.model_validate({field: excluded})
    assert not flt.matches(EdgeQualifiers(**known_hit))  # known excluded value: dropped
    assert flt.matches(EdgeQualifiers(**known_miss))
    assert flt.matches(EdgeQualifiers())  # unknown passes an exclusion (documented)
    assert flt.matches(None)
    if field == "exclude_origin_types":
        assert flt.matches(EdgeQualifiers(provenance=Provenance(source_id="m1")))


def test_unknown_still_fails_allow_sets_alongside_exclusions() -> None:
    flt = QualifierFilter(
        claim_states=frozenset({ClaimState.CURRENT}),
        exclude_claim_states=frozenset({ClaimState.RETRACTED}),
    )
    assert flt.matches(EdgeQualifiers(claim_state=ClaimState.CURRENT))
    assert not flt.matches(EdgeQualifiers())


def test_erased_never_passes_exclusion_only_filters() -> None:
    erased = EdgeQualifiers(
        erasure_state=ErasureState.ERASED, sensitivity_checked=True, sensitivity=frozenset()
    )
    flt = QualifierFilter(
        exclude_claim_states=frozenset({ClaimState.RETRACTED}), unchecked_sensitivity="pass"
    )
    assert not flt.matches(erased)


def test_unchecked_rule_alone_is_no_constraint() -> None:
    assert QualifierFilter(unchecked_sensitivity="pass").is_empty
    assert QualifierFilter().is_empty
    assert not QualifierFilter(exclude_topical_domains=frozenset({"x"})).is_empty
