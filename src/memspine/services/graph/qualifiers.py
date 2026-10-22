"""Typed graph edge qualifiers and qualifier-filtered traversal (RM-41, inside BI-08).

Qualifiers are typed context fields on an edge (and the claim it projects): when an
edge was valid, when it became known, how it was asserted, its lifecycle and claim
state, where it came from, how trusted it is, whom it is about, who may see it, what
role it plays in an event, which vocabulary pack defined it, and so on. The tiers
follow the 2026-10-22 survey (``docs/BI01_CONTRACT_REWRITE_PLAN.md``, "Edge and claim
qualifiers for filtered traversal"):

* P1: valid time, known time, assertion status, lifecycle stage, claim state,
  provenance/origin, authority/trust, subject binding, scope/grant/erasure state,
  event role, vocabulary pack.
* P2: topical domain, sensitivity, context/location, applicability condition,
  extraction confidence/method, recurrence series, completeness.
* P3: independent-support count.

**Unknown never passes.** Every field defaults to ``None`` (unknown). Qualifiers are
filled only from supported evidence or system state; nothing here infers a value. A
:class:`QualifierFilter` constraint on a field the edge does not carry fails, so a
filtered walk never reaches a node through an edge whose context is unknown. An edge
with no qualifiers at all passes only the empty filter.

Storage: qualifiers ride inside the edge ``properties`` mapping under the reserved
key :data:`QUALIFIERS_PROP` (JSON-mode dump, ``None`` fields omitted). Every adapter
already persists ``properties`` losslessly as a JSON blob, so the contract needs no
schema migration and survives projector rebuilds unchanged (D0.1). Malformed stored
qualifiers read back as unknown.

Traversal: :func:`qualified_hops` is the one level-by-level walk every supporting
adapter uses. An adapter supplies only ``frontier_edges(frontier)``, its live
(``weight > 0``) edges touching the frontier inside the namespace and relation
filter; the walk then drops edges that fail the filter and applies the shared fan-out
cap (:func:`~memspine.services.graph.base.capped_neighbors`) to the edges that pass,
so the cap ranks only admissible neighbours. Adapters declare support with
``supports_qualifier_filters``; an adapter that cannot honour the contract raises
:class:`UnsupportedQualifierFilterError` rather than silently ignoring the filter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from memspine.exceptions import MemspineError
from memspine.services.graph.base import GraphEdge, GraphNode, capped_neighbors

__all__ = [
    "QUALIFIERS_PROP",
    "AssertionModality",
    "ClaimState",
    "Completeness",
    "EdgeQualifiers",
    "ErasureState",
    "LifecycleOutcome",
    "LifecycleStatus",
    "Polarity",
    "Provenance",
    "QualifiedGraphStore",
    "QualifierFilter",
    "SensitivityClass",
    "TimePrecision",
    "UnsupportedQualifierFilterError",
    "edge_qualifiers",
    "qualified_hops",
    "qualified_subgraph",
    "require_qualifier_support",
    "with_qualifiers",
]

#: The reserved edge-properties key holding the JSON dump of :class:`EdgeQualifiers`.
QUALIFIERS_PROP = "qualifiers"


class TimePrecision(StrEnum):
    """Source-local precision of a valid-time bound (D-100)."""

    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    HOUR = "hour"
    MINUTE = "minute"
    SECOND = "second"
    INSTANT = "instant"


class AssertionModality(StrEnum):
    """How the claim is held by its source (assertion status, P1)."""

    ASSERTED = "asserted"
    REPORTED = "reported"
    HYPOTHETICAL = "hypothetical"
    INTENDED = "intended"
    QUESTIONED = "questioned"
    UNASSERTED = "unasserted"  # D-98 source-bound mention, never an asserted fact


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class LifecycleStatus(StrEnum):
    """The shared RM-57 ``status`` axis (commitments, work status, event lifecycle)."""

    PROPOSED = "proposed"
    PLANNED = "planned"
    SCHEDULED = "scheduled"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    POSTPONED = "postponed"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class LifecycleOutcome(StrEnum):
    """The RM-57 ``outcome`` axis: required when status is closed, absent otherwise."""

    DONE = "done"
    MISSED = "missed"
    FAILED = "failed"
    DECLINED = "declined"


class ClaimState(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"
    CONTESTED = "contested"
    CORRECTED = "corrected"
    RETRACTED = "retracted"


class ErasureState(StrEnum):
    ACTIVE = "active"
    ERASURE_PENDING = "erasure_pending"
    ERASED = "erased"


class SensitivityClass(StrEnum):
    """RM-52 optional sensitivity classes (can only tighten policy, never weaken it)."""

    HEALTH = "health"
    FINANCIAL = "financial"
    CONTACT = "contact"
    IDENTITY = "identity"
    LOCATION = "location"


class Completeness(StrEnum):
    COMPLETE = "complete"  # the edge set is the complete set
    LOWER_BOUND = "lower_bound"  # only an observed lower bound


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Provenance(_Frozen):
    """Where an edge came from (P1). Origin, principal and trust stay distinct (RM-48)."""

    principal: str | None = None
    source_id: str | None = None
    source_span: tuple[int, int] | None = None
    origin_type: str | None = None
    lineage_root: str | None = None


class EdgeQualifiers(_Frozen):
    """Typed qualifiers of one edge; every field ``None`` means unknown."""

    # ── P1 ──
    #: Valid time, half-open ``[valid_from, valid_to)``. ``valid_to=None`` is unknown
    #: unless ``valid_open_ended`` is True (supported evidence that it still holds).
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    valid_open_ended: bool | None = None
    valid_precision: TimePrecision | None = None
    valid_approximate: bool | None = None
    #: Known/recorded time: when the system learned the claim (as-of reads).
    known_at: datetime | None = None
    assertion_modality: AssertionModality | None = None
    polarity: Polarity | None = None
    lifecycle_status: LifecycleStatus | None = None
    lifecycle_outcome: LifecycleOutcome | None = None
    claim_state: ClaimState | None = None
    supersedes: str | None = None
    provenance: Provenance | None = None
    trust_tier: str | None = None
    subject_binding: str | None = None
    perspective: str | None = None
    scope: str | None = None
    grants: tuple[str, ...] | None = None
    erasure_state: ErasureState | None = None
    event_role: str | None = None
    occurrence_id: str | None = None
    vocabulary_pack: str | None = None
    vocabulary_version: str | None = None
    # ── P2 ──
    topical_domain: str | None = None
    sensitivity: SensitivityClass | None = None
    context_location: str | None = None
    participation_mode: str | None = None
    applicability_condition: str | None = None
    extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_method: str | None = None
    extraction_model: str | None = None
    extraction_version: str | None = None
    recurrence_series_id: str | None = None
    completeness: Completeness | None = None
    # ── P3 ──
    #: Distinct lineage roots supporting the claim.
    independent_support_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> EdgeQualifiers:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("valid_to must be after valid_from (half-open interval)")
        if self.valid_open_ended and self.valid_to is not None:
            raise ValueError("an open-ended interval has no valid_to")
        if self.lifecycle_outcome is not None and self.lifecycle_status not in (
            None,
            LifecycleStatus.CLOSED,
        ):
            raise ValueError("lifecycle_outcome is only allowed when status is closed")
        return self

    def to_properties(self) -> dict[str, object]:
        """JSON-mode dump with unknown fields omitted (what adapters persist)."""
        dumped: dict[str, object] = self.model_dump(mode="json", exclude_none=True)
        return dumped

    def valid_at(self, at: datetime) -> bool | None:
        """Whether ``at`` falls in ``[valid_from, valid_to)``; None when unknown."""
        if self.valid_from is None:
            return None
        if at < self.valid_from:
            return False
        if self.valid_to is not None:
            return at < self.valid_to
        return True if self.valid_open_ended else None

    def valid_overlaps(self, start: datetime, end: datetime) -> bool | None:
        """Whether ``[valid_from, valid_to)`` overlaps ``[start, end)``; None when unknown."""
        if self.valid_from is None:
            return None
        if self.valid_from >= end:
            return False
        if self.valid_to is not None:
            return self.valid_to > start
        return True if self.valid_open_ended else None


def edge_qualifiers(properties: Mapping[str, object]) -> EdgeQualifiers | None:
    """The qualifiers carried by an edge-properties mapping (None = none / malformed)."""
    raw = properties.get(QUALIFIERS_PROP)
    if not isinstance(raw, Mapping):
        return None
    try:
        return EdgeQualifiers.model_validate(dict(raw))
    except ValidationError:
        return None


def with_qualifiers(
    properties: Mapping[str, object] | None, qualifiers: EdgeQualifiers
) -> dict[str, object]:
    """A copy of ``properties`` carrying ``qualifiers`` under :data:`QUALIFIERS_PROP`."""
    merged = dict(properties or {})
    merged[QUALIFIERS_PROP] = qualifiers.to_properties()
    return merged


#: Allow-set filter field -> the qualifier attribute it constrains.
_SET_FIELDS: dict[str, str] = {
    "assertion_modalities": "assertion_modality",
    "polarities": "polarity",
    "lifecycle_statuses": "lifecycle_status",
    "lifecycle_outcomes": "lifecycle_outcome",
    "claim_states": "claim_state",
    "trust_tiers": "trust_tier",
    "subject_bindings": "subject_binding",
    "perspectives": "perspective",
    "scopes": "scope",
    "erasure_states": "erasure_state",
    "event_roles": "event_role",
    "occurrence_ids": "occurrence_id",
    "vocabulary_packs": "vocabulary_pack",
    "topical_domains": "topical_domain",
    "sensitivities": "sensitivity",
    "context_locations": "context_location",
    "participation_modes": "participation_mode",
    "applicability_conditions": "applicability_condition",
    "extraction_methods": "extraction_method",
    "recurrence_series_ids": "recurrence_series_id",
    "completeness": "completeness",
}

#: Allow-set filter field -> the :class:`Provenance` attribute it constrains.
_PROVENANCE_FIELDS: dict[str, str] = {
    "principals": "principal",
    "origin_types": "origin_type",
    "source_ids": "source_id",
    "lineage_roots": "lineage_root",
}


class QualifierFilter(_Frozen):
    """Conjunctive constraints on :class:`EdgeQualifiers`; ``None`` = unconstrained.

    Allow-set fields pass when the edge's value is in the set; every constraint on a
    qualifier the edge does not carry fails (unknown never passes).
    """

    #: Valid-time point: the edge must be valid at this instant.
    valid_at: datetime | None = None
    #: Valid-time window ``[start, end)``: the edge's interval must overlap it.
    valid_window: tuple[datetime, datetime] | None = None
    #: As-of read: the edge must have been known at or before this instant.
    known_as_of: datetime | None = None
    assertion_modalities: frozenset[AssertionModality] | None = None
    polarities: frozenset[Polarity] | None = None
    lifecycle_statuses: frozenset[LifecycleStatus] | None = None
    lifecycle_outcomes: frozenset[LifecycleOutcome] | None = None
    claim_states: frozenset[ClaimState] | None = None
    principals: frozenset[str] | None = None
    origin_types: frozenset[str] | None = None
    source_ids: frozenset[str] | None = None
    lineage_roots: frozenset[str] | None = None
    trust_tiers: frozenset[str] | None = None
    subject_bindings: frozenset[str] | None = None
    perspectives: frozenset[str] | None = None
    scopes: frozenset[str] | None = None
    #: Every grant listed here must be present on the edge.
    required_grants: frozenset[str] | None = None
    erasure_states: frozenset[ErasureState] | None = None
    event_roles: frozenset[str] | None = None
    occurrence_ids: frozenset[str] | None = None
    vocabulary_packs: frozenset[str] | None = None
    topical_domains: frozenset[str] | None = None
    sensitivities: frozenset[SensitivityClass] | None = None
    context_locations: frozenset[str] | None = None
    participation_modes: frozenset[str] | None = None
    applicability_conditions: frozenset[str] | None = None
    min_extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_methods: frozenset[str] | None = None
    recurrence_series_ids: frozenset[str] | None = None
    completeness: frozenset[Completeness] | None = None
    min_independent_support: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _window_order(self) -> QualifierFilter:
        if self.valid_window is not None and self.valid_window[1] <= self.valid_window[0]:
            raise ValueError("valid_window end must be after start")
        return self

    @property
    def is_empty(self) -> bool:
        return not self.model_dump(exclude_none=True)

    def matches(self, qualifiers: EdgeQualifiers | None) -> bool:
        """Whether an edge with ``qualifiers`` passes every constraint."""
        if self.is_empty:
            return True
        if qualifiers is None:
            return False
        q = qualifiers
        if self.valid_at is not None and q.valid_at(self.valid_at) is not True:
            return False
        if self.valid_window is not None and q.valid_overlaps(*self.valid_window) is not True:
            return False
        if self.known_as_of is not None and (q.known_at is None or q.known_at > self.known_as_of):
            return False
        for field_name, attr in _SET_FIELDS.items():
            allowed: frozenset[Any] | None = getattr(self, field_name)
            if allowed is not None and getattr(q, attr) not in allowed:
                return False
        for field_name, attr in _PROVENANCE_FIELDS.items():
            allowed = getattr(self, field_name)
            if allowed is not None and (
                q.provenance is None or getattr(q.provenance, attr) not in allowed
            ):
                return False
        if self.required_grants is not None and (
            q.grants is None or not self.required_grants.issubset(q.grants)
        ):
            return False
        if self.min_extraction_confidence is not None and (
            q.extraction_confidence is None
            or q.extraction_confidence < self.min_extraction_confidence
        ):
            return False
        return self.min_independent_support is None or (
            q.independent_support_count is not None
            and q.independent_support_count >= self.min_independent_support
        )

    def admits(self, edge: GraphEdge) -> bool:
        return self.matches(edge_qualifiers(edge.properties))


class UnsupportedQualifierFilterError(MemspineError):
    """The graph provider cannot honour a qualifier filter (never silently ignored)."""

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"graph provider {provider!r} does not support qualifier-filtered traversal "
            "(RM-41); use sqlite_adjacency or ladybug, or drop the qualifier filter"
        )
        self.provider = provider


@runtime_checkable
class QualifiedGraphStore(Protocol):
    """The optional qualifier-filtered traversal surface of a graph adapter."""

    supports_qualifier_filters: bool

    async def neighbors_qualified(
        self,
        node_id: str,
        qualifier_filter: QualifierFilter,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        """Like ``neighbors``, traversing only edges the filter admits."""
        ...

    async def subgraph_qualified(
        self,
        seeds: Sequence[str],
        qualifier_filter: QualifierFilter,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphEdge]:
        """Like ``subgraph``, over admitted edges only (the induced edges too)."""
        ...


def require_qualifier_support(store: object) -> QualifiedGraphStore:
    """``store`` as a :class:`QualifiedGraphStore`, or a clear error naming it."""
    if isinstance(store, QualifiedGraphStore) and store.supports_qualifier_filters:
        return store
    raise UnsupportedQualifierFilterError(type(store).__name__)


#: ``frontier -> live edges touching it`` (namespace / relation already applied).
FrontierEdgesFn = Callable[[Sequence[str]], Awaitable[list[GraphEdge]]]


async def qualified_hops(
    frontier_edges: FrontierEdgesFn,
    seeds: Sequence[str],
    depth: int,
    qualifier_filter: QualifierFilter,
    max_degree: int | None,
) -> dict[str, int]:
    """node id -> hop count from the nearest seed over admitted edges (seeds at 0)."""
    hops = {seed: 0 for seed in seeds}
    frontier = list(dict.fromkeys(seeds))
    for level in range(1, max(depth, 0) + 1):
        if not frontier:
            break
        edges = [e for e in await frontier_edges(frontier) if qualifier_filter.admits(e)]
        next_frontier: list[str] = []
        for node_id in frontier:
            for other in capped_neighbors(edges, node_id, max_degree):
                if other not in hops:
                    hops[other] = level
                    next_frontier.append(other)
        frontier = next_frontier
    return hops


async def qualified_subgraph(
    frontier_edges: FrontierEdgesFn,
    seeds: Sequence[str],
    depth: int,
    qualifier_filter: QualifierFilter,
    max_degree: int | None,
) -> list[GraphEdge]:
    """Every admitted live edge with both endpoints in the qualified walk."""
    if not seeds:
        return []
    reached = await qualified_hops(frontier_edges, seeds, depth, qualifier_filter, max_degree)
    edges = await frontier_edges(list(reached))
    induced = {
        (e.src, e.dst, e.rel_type): e
        for e in edges
        if e.src in reached and e.dst in reached and e.src != e.dst and qualifier_filter.admits(e)
    }
    return sorted(induced.values(), key=lambda e: (e.src, e.dst, e.rel_type))
