"""Scoped one-value expectations (RM-45): flag competing values, never resolve them.

A ``one`` or ``contextual`` predicate expects at most one supported value per
subject within its declared scope. Several claims supporting the *same* value
are corroboration, not a conflict: cardinality concerns distinct values, not
the number of source claims. A second distinct supported value is reported as a
visible conflict that keeps every claim id; this module never deletes,
supersedes or picks a winner. Only a supported correction or explicit
transition (handled elsewhere) may end a value, and such ended values are
passed with ``superseded=True``.

Scope values are caller-normalised tokens (e.g. a valid-time bucket, a role
binding id, a precision level). Claims missing a scope token are *incomparable*:
they are reported separately rather than compared across unknown time or
precision contexts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from memspine.semantics.specs import PredicateSpec


class ScopedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    claim_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    value: str = Field(min_length=1)
    scope: Mapping[str, str] = Field(default_factory=dict)
    supported: bool = True
    superseded: bool = False


class CardinalityConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    predicate: str
    subject: str
    scope: tuple[tuple[str, str], ...]
    values: tuple[str, ...]
    claim_ids: tuple[str, ...]
    action: str = "flag"


class CardinalityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    conflicts: tuple[CardinalityConflict, ...] = ()
    incomparable_claim_ids: tuple[str, ...] = ()


def detect_cardinality_conflicts(
    spec: PredicateSpec, values: Iterable[ScopedValue]
) -> CardinalityReport:
    if spec.cardinality not in {"one", "contextual"}:
        return CardinalityReport()
    groups: dict[tuple[str, tuple[tuple[str, str], ...]], list[ScopedValue]] = defaultdict(list)
    incomparable: list[str] = []
    for item in values:
        if not item.supported or item.superseded:
            continue
        if any(not item.scope.get(key) for key in spec.cardinality_scope):
            incomparable.append(item.claim_id)
            continue
        scope: tuple[tuple[str, str], ...] = tuple(
            (str(key), item.scope[key]) for key in spec.cardinality_scope
        )
        groups[(item.subject, scope)].append(item)
    conflicts = []
    for (subject, scope), members in sorted(groups.items()):
        distinct = sorted({member.value for member in members})
        if len(distinct) > 1:
            conflicts.append(
                CardinalityConflict(
                    predicate=spec.name,
                    subject=subject,
                    scope=scope,
                    values=tuple(distinct),
                    claim_ids=tuple(sorted(member.claim_id for member in members)),
                )
            )
    return CardinalityReport(
        conflicts=tuple(conflicts), incomparable_claim_ids=tuple(sorted(incomparable))
    )
