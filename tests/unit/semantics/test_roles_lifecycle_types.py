"""RM-47 role slots, RM-57 lifecycle axes, RM-51 entity types, RM-48 origins."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from memspine.semantics import (
    SOURCE_ORIGINS,
    LifecycleState,
    RescheduleLink,
    RoleBindingCheck,
    RoleSpec,
    SemanticRegistry,
    SourceProvenance,
    is_achieved,
    load_builtin_registry,
    validate_transition,
)
from memspine.semantics.specs import ENTITY_TYPES

GENERAL_ROLES = {
    "actor",
    "location",
    "time",
    "instrument",
    "beneficiary",
    "topic",
    "companion",
    "origin",
    "destination",
    "theme",
    "cause",
    "result",
    "participant",
    "manner",
    "amount",
}


@pytest.fixture
def registry() -> SemanticRegistry:
    return load_builtin_registry()


def test_general_roles_registered_with_types_cardinality_and_evidence(registry):
    assert registry.roles.keys() >= GENERAL_ROLES
    assert len(registry.roles) == 33
    assert registry.roles["time"].evidence_requirement == "temporal_ref"
    assert registry.roles["amount"].requires_unit
    assert registry.roles["participant"].max_bindings is None
    assert registry.roles["location"].max_bindings == 1
    assert registry.roles["actor"].evidence_requirement == "span_or_envelope"


def _b(role, kind="entity", etype="person", evidence="exact_span", **extra):
    return RoleBindingCheck(
        role=role, value_kind=kind, entity_type=etype, evidence=evidence, **extra
    )


def test_valid_role_bindings_pass(registry):
    registry.validate_role_bindings(
        "committed_to",
        [_b("obligor", evidence="envelope"), _b("action", etype="work"), _b("beneficiary")],
    )


@pytest.mark.parametrize(
    ("bindings", "match"),
    [
        ([_b("action", etype="work")], "required"),
        ([_b("obligor", evidence=None), _b("action", etype="work")], "evidence"),
        ([_b("obligor", etype=None), _b("action", etype="work")], "entity type"),
        ([_b("obligor"), _b("obligor"), _b("action", etype="work")], "cardinality"),
        ([_b("obligor"), _b("action", etype="work"), _b("location", etype="place")], "declared"),
        ([_b("obligor", kind="text", etype=None), _b("action", etype="work")], "value kind"),
    ],
)
def test_invalid_role_bindings_rejected(registry, bindings, match):
    with pytest.raises(ValueError, match=match):
        registry.validate_role_bindings("committed_to", bindings)


def test_unit_and_temporal_role_contracts():
    with pytest.raises(ValidationError, match="TemporalRef"):
        RoleSpec(name="when", version=1, value_kinds=("temporal",))
    with pytest.raises(ValidationError, match="numbers"):
        RoleSpec(name="qty", version=1, value_kinds=("text",), requires_unit=True)
    amount = RoleSpec(name="amount", version=1, value_kinds=("number",), requires_unit=True)
    probe = SemanticRegistry(
        [
            load_builtin_registry()
            .predicates["gave"]
            .model_copy(
                update={"role_names": ("amount",), "required_roles": (), "event_family": None}
            )
        ],
        [],
        [amount],
    )
    ok = RoleBindingCheck(
        role="amount", value_kind="number", evidence="exact_span", amount=Decimal(5), unit="EUR"
    )
    probe.validate_role_bindings("gave", [ok])
    with pytest.raises(ValueError, match="unit"):
        probe.validate_role_bindings("gave", [ok.model_copy(update={"unit": None})])
    with pytest.raises(ValueError, match="Decimal"):
        probe.validate_role_bindings("gave", [ok.model_copy(update={"amount": Decimal("NaN")})])


def test_duplicate_role_via_compatibility_respects_slot_cardinality(registry):
    with pytest.raises(ValueError, match="cardinality"):
        registry.validate_compatibility(
            "gave",
            subject_type="person",
            value_kind="entity",
            object_entity_type="object",
            role_names=("giver", "giver", "recipient", "item"),
        )


def test_lifecycle_closed_requires_outcome_and_only_closed_has_one():
    LifecycleState(status="closed", outcome="done", evidence_ids=("e1",))
    with pytest.raises(ValidationError, match="requires an outcome"):
        LifecycleState(status="closed", evidence_ids=("e1",))
    with pytest.raises(ValidationError, match="only allowed"):
        LifecycleState(status="open", outcome="done", evidence_ids=("e1",))
    with pytest.raises(ValidationError):
        LifecycleState(status="rescheduled", evidence_ids=("e1",))  # not a status
    with pytest.raises(ValidationError):
        LifecycleState(status="open", evidence_ids=())


@pytest.mark.parametrize("outcome", ["done", "missed", "failed", "declined"])
def test_each_terminal_closed_path(outcome):
    opened = LifecycleState(status="open", evidence_ids=("e1",))
    closed = validate_transition(
        opened, LifecycleState(status="closed", outcome=outcome, evidence_ids=("e1", "e2"))
    )
    assert closed.terminal and is_achieved(closed) == (outcome == "done")


@pytest.mark.parametrize("terminal", ["cancelled", "superseded"])
def test_terminal_statuses_admit_no_transition(terminal):
    end = validate_transition(
        LifecycleState(status="scheduled", evidence_ids=("e1",)),
        LifecycleState(status=terminal, evidence_ids=("e2",)),
    )
    with pytest.raises(ValueError, match="terminal"):
        validate_transition(end, LifecycleState(status="open", evidence_ids=("e3",)))


def test_transition_needs_new_evidence_and_a_real_change():
    opened = LifecycleState(status="open", evidence_ids=("e1",))
    with pytest.raises(ValueError, match="evidence"):
        validate_transition(opened, LifecycleState(status="in_progress", evidence_ids=("e1",)))
    with pytest.raises(ValueError, match="repeated"):
        validate_transition(opened, LifecycleState(status="open", evidence_ids=("e2",)))


def test_rescheduling_is_a_time_change_plus_link():
    link = RescheduleLink(
        item_id="c1", previous_time_ref="t1", new_time_ref="t2", evidence_ids=("e2",)
    )
    assert link.link_kind == "rescheduled_from"
    with pytest.raises(ValidationError, match="different time"):
        RescheduleLink(item_id="c1", previous_time_ref="t1", new_time_ref="t1", evidence_ids=("e",))


def test_lifecycle_predicates_flagged(registry):
    flagged = {n for n, p in registry.predicates.items() if p.lifecycle}
    assert flagged == {"committed_to", "requested_of", "works_on", "has_goal", "wishes"}


def test_new_entity_types_are_usable_and_distinct(registry):
    assert {"concept", "document", "software", "event"} <= set(ENTITY_TYPES)
    registry.validate_compatibility(
        "attended", subject_type="person", value_kind="entity", object_entity_type="event"
    )
    registry.validate_compatibility(
        "cites", subject_type="document", value_kind="entity", object_entity_type="software"
    )
    with pytest.raises(ValueError, match="subject"):
        registry.validate_compatibility(
            "born_in", subject_type="software", value_kind="entity", object_entity_type="place"
        )
    with pytest.raises(ValidationError):
        RoleSpec(name="x", version=1, value_kinds=("entity",), entity_types=("event_log",))


def test_origin_taxonomy_and_distinct_provenance_axes(registry):
    assert len(SOURCE_ORIGINS) == 18
    assert {"tool_output", "system_event", "web_page", "code_repository"} <= SOURCE_ORIGINS
    tool = SourceProvenance(origin="tool_output", source_id="s1")
    assert tool.sender_id is None and tool.trust_tier is None and tool.assertion_owner_id is None
    assert not tool.is_independent_corroboration
    with pytest.raises(ValidationError, match="asset"):
        SourceProvenance(origin="ocr", source_id="s2")
    with pytest.raises(ValidationError, match="explicit fetch"):
        SourceProvenance(origin="web_page", source_id="s3")
    with pytest.raises(ValidationError):
        SourceProvenance(origin="rumour", source_id="s4")
    registry.validate_compatibility(
        "committed_to",
        subject_type="person",
        value_kind="text",
        role_names=("obligor", "action"),
        source_origin="email",
    )
    with pytest.raises(ValueError, match="origin"):
        registry.validate_compatibility(
            "resides_in",
            subject_type="person",
            value_kind="entity",
            object_entity_type="place",
            source_origin="tool_output",
        )
