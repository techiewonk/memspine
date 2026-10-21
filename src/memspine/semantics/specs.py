"""Immutable definitions, independent of models, providers and storage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "other",
]
ValueKind = Literal["entity", "text", "number", "boolean", "temporal"]
PropertyCategory = Literal["descriptive", "measurement", "quantity", "frequency"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)


class RoleSpec(_Spec):
    """One role definition shared by future event and claim bindings.

    Binding evidence, identities and permissions are checked by the canonical
    compiler/commit path; matching a role specification cannot authorise a fact.
    """

    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    entity_types: tuple[EntityType, ...] = ()


class PropertySpec(_Spec):
    category: PropertyCategory
    predicates: tuple[str, ...] = Field(min_length=1)
    subject_types: tuple[EntityType, ...] = Field(min_length=1)
    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    unit_dimension: str | None = None
    allowed_units: tuple[str, ...] = ()
    requires_currency: bool = False
    nonnegative_integer: bool = False
    context_requirements: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _numeric_contract(self) -> PropertySpec:
        if self.category != "descriptive" and self.value_kinds != ("number",):
            raise ValueError("measurement, quantity and frequency properties require numbers")
        if self.allowed_units and not self.unit_dimension:
            raise ValueError("units require an explicit dimension")
        if self.requires_currency and self.unit_dimension != "currency":
            raise ValueError("currency requirement needs currency dimension")
        return self


class PredicateSpec(_Spec):
    subject_types: tuple[EntityType, ...] = Field(min_length=1)
    value_kinds: tuple[ValueKind, ...] = Field(min_length=1)
    object_entity_types: tuple[EntityType, ...] = ()
    semantics: Literal["state", "event", "relation", "attribute"]
    cardinality: Literal["one", "many", "unknown"]
    transition_policy: Literal["explicit_only", "append_only"]
    closed_world: Literal[False] = False
    allowed_source_origins: tuple[str, ...] = Field(min_length=1)
    property_category: PropertyCategory | None = None
    role_names: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()
    extraction_rule_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _roles(self) -> PredicateSpec:
        if len(set(self.role_names)) != len(self.role_names):
            raise ValueError("duplicate role declaration")
        if not set(self.required_roles) <= set(self.role_names):
            raise ValueError("required roles must be declared")
        return self
