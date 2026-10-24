"""Typed semantic identity attached to a canonical assertion envelope."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from memspine.schemas.base import DurablePart
from memspine.schemas.durable.evidence import Evidence

ValueKind = Literal["entity", "text", "number", "boolean", "temporal"]
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


class RoleBinding(DurablePart):
    """A registry-versioned role value with its pinned evidence."""

    binding_id: str = Field(min_length=1)
    role_name: str = Field(min_length=1)
    role_version: int = Field(ge=1)
    value_kind: ValueKind
    value: str = Field(min_length=1)
    entity_type: EntityType | None = None
    evidence: list[Evidence] = Field(min_length=1)
    unit: str | None = None
    amount: Decimal | None = None

    @model_validator(mode="after")
    def _typed_value_and_evidence(self) -> RoleBinding:
        if not any(item.counts_as_evidence for item in self.evidence):
            raise ValueError("a role binding requires support or binding evidence")
        if self.value_kind != "entity" and self.entity_type is not None:
            raise ValueError("only entity role values carry an entity type")
        if self.value_kind == "number":
            if self.amount is None or not self.amount.is_finite():
                raise ValueError("a numeric role value requires a finite amount")
        elif self.amount is not None or self.unit is not None:
            raise ValueError("only numeric role values carry an amount or unit")
        return self


class SemanticEnvelope(DurablePart):
    """Registry interpretation of an assertion, never an alternate writer."""

    predicate: str = Field(min_length=1)
    predicate_version: int = Field(ge=1)
    registry_digest: str = Field(min_length=64, max_length=64)
    subject_type: EntityType
    object_kind: ValueKind
    object_entity_type: EntityType | None = None
    cardinality: Literal["one", "many", "contextual", "unknown"]
    cardinality_scope: dict[str, str] = Field(default_factory=dict)
    role_bindings: list[RoleBinding] = Field(default_factory=list)
    assertion_owner: str = Field(min_length=1)
    producer_revision: str = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_bindings(self) -> SemanticEnvelope:
        ids = [binding.binding_id for binding in self.role_bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("semantic role binding ids must be unique")
        return self


__all__ = ["RoleBinding", "SemanticEnvelope"]
