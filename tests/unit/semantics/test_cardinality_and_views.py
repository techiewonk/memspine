"""RM-45 scoped one-value expectations and RM-46 symmetric/inverse read views."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memspine.semantics import (
    PredicateSpec,
    ScopedValue,
    SemanticRegistry,
    StoredEdge,
    detect_cardinality_conflicts,
    edge_views,
    load_builtin_registry,
)


@pytest.fixture
def registry() -> SemanticRegistry:
    return load_builtin_registry()


def _sv(claim, value, subject="p1", supported=True, superseded=False, **scope):
    return ScopedValue(
        claim_id=claim,
        subject=subject,
        value=value,
        scope=scope,
        supported=supported,
        superseded=superseded,
    )


def test_reviewed_one_value_predicates_and_multiple_homes(registry):
    scoped = {n for n, p in registry.predicates.items() if p.cardinality in {"one", "contextual"}}
    assert scoped == {
        "born_in",
        "located_in",
        "started_on",
        "opened_on",
        "completed_on",
        "occurred_on",
        "has_measurement",
        "has_quantity",
        "due_by",
    }
    assert registry.predicates["resides_in"].cardinality == "many"  # several homes allowed
    assert registry.predicates["born_in"].cardinality_scope == ("precision",)


def test_second_supported_value_is_flagged_never_resolved(registry):
    spec = registry.predicates["born_in"]
    report = detect_cardinality_conflicts(
        spec,
        [
            _sv("c1", "lisbon", precision="city"),
            _sv("c2", "porto", precision="city"),
            _sv("c3", "lisbon", precision="city"),
        ],
    )
    (conflict,) = report.conflicts
    assert conflict.values == ("lisbon", "porto")
    assert conflict.claim_ids == ("c1", "c2", "c3")  # all evidence retained
    assert conflict.action == "flag"


def test_same_value_from_many_claims_and_different_precision_do_not_conflict(registry):
    spec = registry.predicates["born_in"]
    report = detect_cardinality_conflicts(
        spec,
        [
            _sv("c1", "lisbon", precision="city"),
            _sv("c2", "lisbon", precision="city"),
            _sv("c3", "portugal", precision="country"),
        ],
    )
    assert report.conflicts == ()


def test_missing_scope_is_incomparable_and_unsupported_or_superseded_ignored(registry):
    spec = registry.predicates["located_in"]
    report = detect_cardinality_conflicts(
        spec,
        [
            _sv("c1", "kitchen", valid_time="t1", precision="room"),
            _sv("c2", "garage", precision="room"),
            _sv("c3", "attic", supported=False, valid_time="t1", precision="room"),
            _sv("c4", "hall", superseded=True, valid_time="t1", precision="room"),
        ],
    )
    assert report.conflicts == ()
    assert report.incomparable_claim_ids == ("c2",)


def test_many_cardinality_never_flags(registry):
    spec = registry.predicates["resides_in"]
    report = detect_cardinality_conflicts(spec, [_sv("c1", "porto"), _sv("c2", "lisbon")])
    assert report.conflicts == () and report.incomparable_claim_ids == ()


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"cardinality": "many", "cardinality_scope": ("valid_time",)}, "scope"),
        ({"cardinality": "contextual", "cardinality_scope": ()}, "contextual"),
        ({"cardinality": "one", "cardinality_scope": ("attribute_key",)}, "attribute_key"),
        ({"cardinality": "one", "cardinality_scope": ("role",)}, "role"),
    ],
)
def test_invalid_cardinality_scopes_rejected(registry, update, match):
    raw = registry.predicates["born_in"].model_dump() | update
    with pytest.raises(ValidationError, match=match):
        PredicateSpec.model_validate(raw)


def test_symmetric_and_inverse_metadata(registry):
    for name in ("sibling_of", "spouse_of", "friend_of", "partner_of", "neighbour_of"):
        assert registry.predicates[name].symmetric
    assert registry.predicates["parent_of"].inverse_view == "child_of"
    assert "child_of" not in registry.predicates  # a view, not a 78th predicate
    assert registry.predicates["gave"].event_family == registry.predicates["received"].event_family


def test_reverse_view_is_not_a_second_fact(registry):
    edges = [
        StoredEdge(
            claim_id="c1", subject="ana", predicate="parent_of", object="ben", evidence_ids=("e1",)
        ),
        StoredEdge(
            claim_id="c2", subject="ana", predicate="sibling_of", object="cy", evidence_ids=("e2",)
        ),
        StoredEdge(
            claim_id="c3", subject="ana", predicate="likes", object="ben", evidence_ids=("e3",)
        ),
    ]
    (child,) = edge_views(registry.predicates, edges, focus="ben")
    assert (child.subject, child.label, child.object) == ("ben", "child_of", "ana")
    assert child.claim_id == "c1" and child.evidence_ids == ("e1",) and not child.asserted
    (sib,) = edge_views(registry.predicates, edges, focus="cy")
    assert (sib.label, sib.direction) == ("sibling_of", "reverse")
    forward = edge_views(registry.predicates, edges, focus="ana")
    assert len(forward) == 3 and all(v.asserted for v in forward)


def test_invalid_symmetry_pairs_rejected(registry):
    base = registry.predicates["parent_of"].model_dump()
    with pytest.raises(ValidationError, match="own inverse"):
        PredicateSpec.model_validate(base | {"symmetric": True})
    with pytest.raises(ValidationError, match="identical subject"):
        PredicateSpec.model_validate(
            registry.predicates["employed_by"].model_dump()
            | {"symmetric": True, "inverse_view": None}
        )
    with pytest.raises(ValidationError, match="differ"):
        PredicateSpec.model_validate(base | {"inverse_view": "parent_of"})
    clash = registry.predicates["mentor_of"].model_copy(update={"inverse_view": "parent_of"})
    with pytest.raises(ValueError, match="collides"):
        SemanticRegistry([registry.predicates["parent_of"], clash], [], [])
    twin = registry.predicates["mentor_of"].model_copy(update={"inverse_view": "child_of"})
    with pytest.raises(ValueError, match="collides"):
        SemanticRegistry([registry.predicates["parent_of"], twin], [], [])


def test_event_family_members_must_share_roles(registry):
    roles = list(registry.roles.values())
    odd = registry.predicates["received"].model_copy(update={"role_names": ("giver", "item")})
    odd = odd.model_copy(update={"required_roles": ("giver",)})
    with pytest.raises(ValueError, match="share roles"):
        SemanticRegistry([registry.predicates["gave"], odd], [], roles)
