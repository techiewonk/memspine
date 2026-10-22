"""Immutable definitions, independent of models, providers and storage."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.semantics.domains import DOMAIN_PATTERN
from memspine.semantics.origins import SourceOrigin
from memspine.semantics.sensitivity import SensitivityLabel

# RM-51 (2026-10-22) adds concept, document, software and event. Each selected
# pair names one type: "concept" covers topics, "document" covers artifacts,
# "software" covers systems. ``event`` is a *named* event entity (a festival,
# a meeting series); it is never an event-log id or an occurrence id.
EntityType = Literal[
    "person",
    "agent",
    "group",
    "organisation",
    "place",
    "object",
    "work",
    "project",
    "species",
    "concept",
    "document",
    "software",
    "event",
    "other",
]
ENTITY_TYPES: tuple[str, ...] = get_args(EntityType)
ValueKind = Literal["entity", "text", "number", "boolean", "temporal"]
# ``qualifier`` keys describe a claim of another predicate (priority of a
# commitment, locator of a citation); they are never subject attributes.
PropertyCategory = Literal["descriptive", "measurement", "quantity", "frequency", "qualifier"]
# RM-45: one = at most one supported value per subject within the declared scope;
# contextual = one per explicit context (scope required); many/unknown = no
# expectation. A one-value expectation only *flags* competing values.
Cardinality = Literal["one", "many", "contextual", "unknown"]
CardinalityScopeKey = Literal["valid_time", "role", "precision", "attribute_key", "context"]
# RM-47 evidence requirement for one role binding.
RoleEvidence = Literal["exact_span", "span_or_envelope", "temporal_ref"]
# assertion: a reported world statement; directive: an instruction addressed to
# the assistant, never a user fact; attributed: a reported feeling/belief held
# by its subject, never verified psychology or world truth.
StatementKind = Literal["assertion", "directive", "attributed"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)


class RoleSpec(_Spec):
    """One role definition shared by event and claim bindings.

    Binding evidence, identities and permissions are checked by the canonical
    compiler/commit path; matching a role specification cannot authorise a fact.
    ``max_bindings`` bounds how many bindings one claim/occurrence may carry for
    this slot (``None`` = unbounded; several participants need not be invented
    groups). Whether a slot is required is declared per predicate.
    """

    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    entity_types: tuple[EntityType, ...] = ()
    max_bindings: int | None = Field(default=1, ge=1)
    evidence_requirement: RoleEvidence = "exact_span"
    requires_unit: bool = False

    @model_validator(mode="after")
    def _role_contract(self) -> RoleSpec:
        temporal = "temporal" in self.value_kinds
        if temporal != (self.evidence_requirement == "temporal_ref"):
            raise ValueError("temporal roles, and only they, require a canonical TemporalRef")
        if temporal and self.value_kinds != ("temporal",):
            raise ValueError("temporal roles cannot mix value kinds")
        if self.requires_unit and self.value_kinds != ("number",):
            raise ValueError("unit-bearing roles require numbers")
        if self.entity_types and "entity" not in self.value_kinds:
            raise ValueError("entity types require the entity value kind")
        return self


class PropertySpec(_Spec):
    category: PropertyCategory
    predicates: tuple[str, ...] = Field(min_length=1)
    subject_types: tuple[EntityType, ...] = ()
    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    unit_dimension: str | None = None
    allowed_units: tuple[str, ...] = ()
    requires_currency: bool = False
    nonnegative_integer: bool = False
    context_requirements: tuple[str, ...] = ()
    allowed_values: tuple[str, ...] = ()
    sensitivity: tuple[SensitivityLabel, ...] = ()
    # RM-91: default topical domain of claims keyed by this property (hierarchical id).
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)

    @model_validator(mode="after")
    def _numeric_contract(self) -> PropertySpec:
        if self.category in {"measurement", "quantity", "frequency"} and self.value_kinds != (
            "number",
        ):
            raise ValueError("measurement, quantity and frequency properties require numbers")
        if self.allowed_units and not self.unit_dimension:
            raise ValueError("units require an explicit dimension")
        if self.requires_currency and self.unit_dimension != "currency":
            raise ValueError("currency requirement needs currency dimension")
        if (self.category == "qualifier") == bool(self.subject_types):
            raise ValueError("qualifiers have no subject types; other properties require them")
        if self.allowed_values and "text" not in self.value_kinds:
            raise ValueError("enumerated values require the text value kind")
        if len(set(self.sensitivity)) != len(self.sensitivity):
            raise ValueError("duplicate sensitivity label")
        return self


class PredicateSpec(_Spec):
    subject_types: tuple[EntityType, ...] = Field(min_length=1)
    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    object_entity_types: tuple[EntityType, ...] = ()
    semantics: Literal["state", "event", "relation", "attribute"]
    cardinality: Cardinality
    cardinality_scope: tuple[CardinalityScopeKey, ...] = ()
    transition_policy: Literal["explicit_only", "append_only"]
    closed_world: Literal[False] = False
    allowed_source_origins: tuple[SourceOrigin, ...] = Field(min_length=1)
    property_category: PropertyCategory | None = None
    role_names: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()
    required_qualifiers: tuple[str, ...] = ()
    extraction_rule_ids: tuple[str, ...] = ()
    # RM-46: stored once as asserted; reverse direction is only a read view.
    symmetric: bool = False
    inverse_view: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    event_family: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    # RM-57: claims of this predicate carry the status/outcome lifecycle axes.
    lifecycle: bool = False
    statement_kind: StatementKind = "assertion"
    sensitivity: tuple[SensitivityLabel, ...] = ()
    # Possession-style events may open/close a state predicate's validity, but
    # only through an explicit, evidenced transition.
    state_effect: Literal["opens", "closes"] | None = None
    affects_state: str | None = None
    # RM-91: default topical domain of claims of this predicate (hierarchical id).
    domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)

    @model_validator(mode="after")
    def _roles(self) -> PredicateSpec:
        if len(set(self.role_names)) != len(self.role_names):
            raise ValueError("duplicate role declaration")
        if not set(self.required_roles) <= set(self.role_names):
            raise ValueError("required roles must be declared")
        scope = self.cardinality_scope
        if len(set(scope)) != len(scope):
            raise ValueError("duplicate cardinality scope key")
        if self.cardinality in {"many", "unknown"} and scope:
            raise ValueError("cardinality scope applies only to one/contextual predicates")
        if self.cardinality == "contextual" and not scope:
            raise ValueError("contextual cardinality requires an explicit scope")
        if "attribute_key" in scope and self.property_category is None:
            raise ValueError("attribute_key scope requires a property predicate")
        if "role" in scope and not self.role_names:
            raise ValueError("role scope requires declared roles")
        if self.symmetric or self.inverse_view:
            # Views render entity-valued claims only; text values have no reverse.
            if "entity" not in self.value_kinds or not self.object_entity_types:
                raise ValueError("symmetric/inverse views require typed entity objects")
            if self.symmetric and self.value_kinds != ("entity",):
                raise ValueError("symmetric predicates relate entities only")
            if self.symmetric and self.inverse_view:
                raise ValueError("a symmetric predicate is its own inverse view")
            if self.symmetric and set(self.subject_types) != set(self.object_entity_types):
                raise ValueError("symmetric predicate needs identical subject and object types")
            if self.inverse_view == self.name:
                raise ValueError("inverse view must differ from the predicate name")
        if self.event_family and self.semantics != "event":
            raise ValueError("event families group event predicates only")
        if (self.state_effect is None) != (self.affects_state is None):
            raise ValueError("state effect and affected state must be declared together")
        if self.state_effect and self.semantics != "event":
            raise ValueError("only events may open or close a state")
        if len(set(self.sensitivity)) != len(self.sensitivity):
            raise ValueError("duplicate sensitivity label")
        return self
