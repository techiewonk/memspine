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

Traversal: :func:`qualified_hops` is the one level-by-level walk every adapter uses.
There is one graph port (2026-10-23, ADR-080): ``GraphStore.neighbors`` /
``GraphStore.subgraph`` take ``qualifier_filter`` (None = the unfiltered walk; any
filter, even an empty one, = the qualified walk, which never traverses erased edges).
An adapter supplies only ``frontier_edges(nodes)`` (a port method, also used by the
read-path graph leg to fetch only its seeds' edges), its live (``weight > 0``) edges
touching the frontier inside the namespace and relation filter; the walk then drops
edges that fail the filter and applies the shared fan-out cap
(:func:`~memspine.services.graph.base.capped_neighbors`) to the edges that pass, so the
cap ranks only admissible neighbours.

Write validation (RM-84): every adapter passes edge properties through
:func:`sanitize_qualifiers` on ``upsert_edge``. Malformed qualifiers are dropped (the
edge is stored unqualified, so no filter admits it) and a structured
``graph.qualifiers_dropped`` warning names the edge and the error; the write never fails.

Shared vocabularies (RM-85): lifecycle status/outcome, sensitivity labels and source
origins are the ``memspine.semantics`` types (RM-57, RM-52, RM-48); this module defines
no copy of them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from memspine.config.constants import EDGE_TRUST_TIERS
from memspine.observability.logging import get_logger
from memspine.semantics.domains import DOMAIN_PATTERN, DomainSource, domain_in, is_domain_id
from memspine.semantics.lifecycle import LifecycleOutcome, LifecycleStatus
from memspine.semantics.origins import SourceOrigin
from memspine.semantics.sensitivity import SensitivityLabel
from memspine.services.graph.base import GraphEdge, capped_neighbors

__all__ = [
    "QUALIFIERS_PROP",
    "AssertionModality",
    "BindingStatus",
    "CallerVisibility",
    "ClaimState",
    "Completeness",
    "EdgeQualifiers",
    "ErasureState",
    "Polarity",
    "Provenance",
    "QualifierFilter",
    "TimePrecision",
    "TrustTier",
    "active_graph_filter",
    "edge_qualifiers",
    "graph_filter_scope",
    "qualified_hops",
    "qualified_subgraph",
    "sanitize_qualifiers",
    "trust_tier_for",
    "with_qualifiers",
]

_log = get_logger(__name__)

#: The reserved edge-properties key holding the JSON dump of :class:`EdgeQualifiers`.
QUALIFIERS_PROP = "qualifiers"

#: RM-90: how the compiler bound the claim's mentions to entity nodes.
BindingStatus = Literal["bound", "partly", "unbound"]

#: RM-102: named edge trust tiers, derived from the 0..1 trust by
#: :data:`~memspine.config.constants.EDGE_TRUST_TIERS`.
TrustTier = Literal["untrusted", "low", "normal", "high"]


def trust_tier_for(trust: float) -> TrustTier:
    """The named tier of a 0..1 trust (lower bounds inclusive, RM-102)."""
    for tier, lower in EDGE_TRUST_TIERS:
        if trust >= lower:
            return tier  # type: ignore[return-value]
    return "untrusted"


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


class Completeness(StrEnum):
    COMPLETE = "complete"  # the edge set is the complete set
    LOWER_BOUND = "lower_bound"  # only an observed lower bound


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_aware(value: datetime | None) -> datetime | None:
    """D-100 Q-S2: resolved times are timezone-aware (timezone-free dates stay text)."""
    if value is not None and value.utcoffset() is None:
        raise ValueError("qualifier times must be timezone-aware (D-100)")
    return value


_FIXED_STEPS: dict[TimePrecision, timedelta] = {
    TimePrecision.DAY: timedelta(days=1),
    TimePrecision.HOUR: timedelta(hours=1),
    TimePrecision.MINUTE: timedelta(minutes=1),
    TimePrecision.SECOND: timedelta(seconds=1),
}


def _period(bound: datetime, precision: TimePrecision | None) -> tuple[datetime, datetime]:
    """RM-97: ``[start, end)`` of the period ``bound`` names at ``precision``.

    Computed on the bound's own wall clock and timezone (stored bounds are aware; a
    bound written in UTC has UTC periods). ``instant`` / None: the bound itself."""
    if precision is None or precision is TimePrecision.INSTANT:
        return bound, bound
    if precision is TimePrecision.YEAR:
        start = bound.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start.replace(year=start.year + 1)
    if precision is TimePrecision.MONTH:
        start = bound.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            return start, start.replace(year=start.year + 1, month=1)
        return start, start.replace(month=start.month + 1)
    zero: dict[str, int] = {"microsecond": 0}
    if precision in (TimePrecision.DAY, TimePrecision.HOUR, TimePrecision.MINUTE):
        zero["second"] = 0
    if precision in (TimePrecision.DAY, TimePrecision.HOUR):
        zero["minute"] = 0
    if precision is TimePrecision.DAY:
        zero["hour"] = 0
    start = bound.replace(**zero)  # type: ignore[arg-type]
    return start, start + _FIXED_STEPS[precision]


#: Erasure states that are never traversed, whatever the filter says (RM-83).
_NOT_TRAVERSABLE = frozenset({"erased", "erasure_pending"})


class Provenance(_Frozen):
    """Where an edge came from (P1). Origin, principal and trust stay distinct (RM-48)."""

    principal: str | None = None
    source_id: str | None = None
    source_span: tuple[int, int] | None = None
    #: RM-48 source origin; unknown unless the source channel maps to one (RM-85).
    origin_type: SourceOrigin | None = None
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
    #: RM-89: the claim this edge projects (the source record id) and its evidence ids.
    claim_id: str | None = None
    evidence_ids: tuple[str, ...] | None = None
    provenance: Provenance | None = None
    #: RM-102: the 0..1 trust and its named tier (the tier is derived when only trust is given).
    trust: float | None = Field(default=None, ge=0.0, le=1.0)
    trust_tier: TrustTier | None = None
    #: RM-90: bound / partly / unbound (how the claim's mentions resolved to entity nodes).
    binding_status: BindingStatus | None = None
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
    #: RM-91: hierarchical domain id (``health/condition``); see ``memspine.semantics.domains``.
    topical_domain: str | None = Field(default=None, pattern=DOMAIN_PATTERN)
    #: Which source set ``topical_domain`` (ingest tag, property, predicate, pack, GLiNER topic).
    topical_domain_source: DomainSource | None = None
    #: RM-87/RM-99 sensitivity labels found by the labelling layer (several per edge). Read
    #: together with ``sensitivity_checked`` (three states, RM-86):
    #:
    #: * ``sensitivity_checked=True``: a complete check; the set holds every label found and
    #:   an empty set is *checked_none* (no detector fired; not a proof of harmlessness);
    #: * ``sensitivity_checked=False``: an incomplete check (an enabled detector failed); the
    #:   labels found so far are kept, the absence of others is unknown;
    #: * ``sensitivity_checked=None``: not checked (``sensitivity`` is None too).
    sensitivity: frozenset[SensitivityLabel] | None = None
    sensitivity_checked: bool | None = None
    #: Which labeller produced the labels, and its version (rules + vocabulary + model).
    sensitivity_labeller: str | None = None
    sensitivity_labeller_version: str | None = None
    #: Per-label confidence in ``[0, 1]`` (keys are a subset of ``sensitivity``).
    sensitivity_confidence: dict[SensitivityLabel, float] | None = None
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

    _aware = field_validator("valid_from", "valid_to", "known_at")(_require_aware)

    @model_validator(mode="before")
    @classmethod
    def _derive_trust_tier(cls, data: Any) -> Any:
        """RM-102: a trust without a tier gets its derived tier."""
        if isinstance(data, Mapping) and data.get("trust_tier") is None:
            trust = data.get("trust")
            if isinstance(trust, int | float) and not isinstance(trust, bool) and 0 <= trust <= 1:
                return {**data, "trust_tier": trust_tier_for(float(trust))}
        return data

    @property
    def traversable(self) -> bool:
        """False for erased or erasure-pending edges (never walked, RM-83)."""
        return self.erasure_state not in _NOT_TRAVERSABLE

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
        if self.lifecycle_outcome is not None and self.lifecycle_status not in (None, "closed"):
            raise ValueError("lifecycle_outcome is only allowed when status is closed")
        self._sensitivity_coherent()
        if (
            self.trust is not None
            and self.trust_tier is not None
            and trust_tier_for(self.trust) != self.trust_tier
        ):
            raise ValueError("trust_tier does not match trust (RM-102 thresholds)")
        return self

    def _sensitivity_coherent(self) -> None:
        """RM-86/RM-87: the three sensitivity states stay distinguishable."""
        if self.sensitivity_checked is None:
            if (
                self.sensitivity is not None
                or self.sensitivity_confidence is not None
                or self.sensitivity_labeller is not None
                or self.sensitivity_labeller_version is not None
            ):
                raise ValueError("sensitivity labels need sensitivity_checked (True or False)")
            return
        if self.sensitivity is None:
            raise ValueError("a sensitivity check records its label set (empty = none found)")
        if self.sensitivity_confidence is not None:
            if not set(self.sensitivity_confidence) <= self.sensitivity:
                raise ValueError("sensitivity_confidence names a label the edge does not carry")
            if any(not 0.0 <= c <= 1.0 for c in self.sensitivity_confidence.values()):
                raise ValueError("sensitivity_confidence values must lie in [0, 1]")

    @field_serializer("sensitivity")
    def _sorted_labels(self, value: frozenset[str] | None) -> list[str] | None:
        """Sorted, so the stored form is stable across processes (idempotent re-projection)."""
        return None if value is None else sorted(value)

    def to_properties(self) -> dict[str, object]:
        """JSON-mode dump with unknown fields omitted (what adapters persist)."""
        dumped: dict[str, object] = self.model_dump(mode="json", exclude_none=True)
        return dumped

    def _periods(self) -> tuple[datetime, datetime, datetime | None, datetime | None]:
        """RM-97: ``(from_lo, from_hi, to_lo, to_hi)``, each bound widened to its period.

        A bound stated at ``valid_precision`` covers its whole period (month precision =
        the whole month), computed in the bound's own timezone. ``instant`` (and no
        precision) is the bound itself. ``to_*`` are None when ``valid_to`` is."""
        assert self.valid_from is not None
        precision = self.valid_precision
        from_lo, from_hi = _period(self.valid_from, precision)
        if self.valid_to is None:
            return from_lo, from_hi, None, None
        to_lo, to_hi = _period(self.valid_to, precision)
        return from_lo, from_hi, to_lo, to_hi

    def valid_at(self, at: datetime) -> bool | None:
        """Whether ``at`` falls in ``[valid_from, valid_to)``; None when unknown.

        RM-97: each bound covers its whole precision period, so the interval is
        ``[start of valid_from's period, end of valid_to's period)``. With
        ``valid_approximate`` a point inside a bound's period is unknown (None)."""
        if self.valid_from is None:
            return None
        from_lo, from_hi, to_lo, to_hi = self._periods()
        if at < from_lo:
            return False
        if to_hi is not None and at >= to_hi:
            return False
        if to_lo is None and not self.valid_open_ended:
            return None  # the end is unknown
        on_boundary = at < from_hi or (to_lo is not None and at >= to_lo)
        if on_boundary and self.valid_approximate:
            return None
        return True

    def valid_overlaps(self, start: datetime, end: datetime) -> bool | None:
        """Whether ``[valid_from, valid_to)`` overlaps ``[start, end)``; None when unknown.

        RM-97: the interval is widened to whole precision periods as in :meth:`valid_at`.
        With ``valid_approximate`` the overlap must hold for the certain core
        ``[end of valid_from's period, start of valid_to's period)``, else it is unknown."""
        if self.valid_from is None:
            return None
        from_lo, from_hi, to_lo, to_hi = self._periods()
        if from_lo >= end:
            return False
        if to_hi is not None and to_hi <= start:
            return False
        if to_lo is None and not self.valid_open_ended:
            return None
        if not self.valid_approximate:
            return True
        core_end = end if to_lo is None else min(to_lo, end)
        return True if max(from_hi, start) < core_end else None


def edge_qualifiers(properties: Mapping[str, object]) -> EdgeQualifiers | None:
    """The qualifiers carried by an edge-properties mapping (None = none / malformed)."""
    raw = properties.get(QUALIFIERS_PROP)
    if not isinstance(raw, Mapping):
        return None
    try:
        return EdgeQualifiers.model_validate(dict(raw))
    except ValidationError:
        return None


def sanitize_qualifiers(
    properties: Mapping[str, object] | None,
    *,
    src: str,
    dst: str,
    rel_type: str,
    provider: str,
) -> dict[str, object]:
    """RM-84: a copy of ``properties`` whose qualifiers are valid, or carry none.

    Malformed qualifiers are dropped (the edge is stored unqualified, so no filter
    admits it) and a structured ``graph.qualifiers_dropped`` warning names the edge
    and the error. The write itself never fails on qualifiers."""
    props = dict(properties or {})
    if QUALIFIERS_PROP not in props:
        return props
    raw = props[QUALIFIERS_PROP]
    error: str | None = None
    if not isinstance(raw, Mapping):
        error = f"qualifiers must be a mapping, got {type(raw).__name__}"
    else:
        try:
            EdgeQualifiers.model_validate(dict(raw))
        except ValidationError as exc:
            error = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            )
    if error is None:
        return props
    del props[QUALIFIERS_PROP]
    _log.warning(
        "graph.qualifiers_dropped",
        provider=provider,
        src=src,
        dst=dst,
        rel_type=rel_type,
        error=error[:500],
    )
    return props


def with_qualifiers(
    properties: Mapping[str, object] | None, qualifiers: EdgeQualifiers
) -> dict[str, object]:
    """A copy of ``properties`` carrying ``qualifiers`` under :data:`QUALIFIERS_PROP`."""
    merged = dict(properties or {})
    merged[QUALIFIERS_PROP] = qualifiers.to_properties()
    return merged


#: An edge without qualifiers reads as all-unknown.
_UNKNOWN = EdgeQualifiers()

#: Allow-set filter field -> the qualifier attribute it constrains.
_SET_FIELDS: dict[str, str] = {
    "assertion_modalities": "assertion_modality",
    "polarities": "polarity",
    "lifecycle_statuses": "lifecycle_status",
    "lifecycle_outcomes": "lifecycle_outcome",
    "claim_states": "claim_state",
    "trust_tiers": "trust_tier",
    "binding_statuses": "binding_status",
    "subject_bindings": "subject_binding",
    "perspectives": "perspective",
    "scopes": "scope",
    "erasure_states": "erasure_state",
    "event_roles": "event_role",
    "occurrence_ids": "occurrence_id",
    "vocabulary_packs": "vocabulary_pack",
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

#: RM-86 exclusion (deny-set) filter field -> the qualifier attribute it constrains.
_EXCLUDE_FIELDS: dict[str, str] = {
    "exclude_claim_states": "claim_state",
    "exclude_lifecycle_statuses": "lifecycle_status",
    "exclude_assertion_modalities": "assertion_modality",
}

#: Filter fields that only modify another constraint (never a constraint on their own).
_MODIFIER_FIELDS = frozenset({"unchecked_sensitivity"})


class QualifierFilter(_Frozen):
    """Conjunctive constraints on :class:`EdgeQualifiers`; ``None`` = unconstrained.

    Allow-set fields pass when the edge's value is in the set; every constraint on a
    qualifier the edge does not carry fails (unknown never passes an allow-set).

    Exclusion fields (``exclude_*``, RM-86) drop an edge whose known value is in the set.
    Per-field unknown rules:

    * ``exclude_claim_states`` / ``exclude_lifecycle_statuses`` /
      ``exclude_assertion_modalities`` / ``exclude_topical_domains`` /
      ``exclude_origin_types``: an unknown value **passes** the exclusion (it is not known to
      be excluded). Combine with the matching allow-set to also require a known value.
    * ``exclude_sensitivities``: a labelled match drops the edge; a complete check that found
      none of the excluded labels (including *checked_none*) passes; an edge that was not
      checked, or checked incompletely, follows ``unchecked_sensitivity`` (``"fail"`` by
      default: an unchecked edge is dropped).

    ``sensitivities`` (allow-set) passes when the edge carries ANY listed label; an unchecked
    edge or a checked_none edge fails it. Erased edges never pass (RM-83).
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
    origin_types: frozenset[SourceOrigin] | None = None
    source_ids: frozenset[str] | None = None
    lineage_roots: frozenset[str] | None = None
    #: RM-102: the edge's 0..1 trust must be at least this.
    min_trust: float | None = Field(default=None, ge=0.0, le=1.0)
    trust_tiers: frozenset[TrustTier] | None = None
    binding_statuses: frozenset[BindingStatus] | None = None
    subject_bindings: frozenset[str] | None = None
    perspectives: frozenset[str] | None = None
    scopes: frozenset[str] | None = None
    #: Every grant listed here must be present on the edge.
    required_grants: frozenset[str] | None = None
    erasure_states: frozenset[ErasureState] | None = None
    event_roles: frozenset[str] | None = None
    occurrence_ids: frozenset[str] | None = None
    vocabulary_packs: frozenset[str] | None = None
    #: RM-91: a domain matches itself and its descendants (``health`` admits
    #: ``health/condition``, never ``healthcare``); an unknown domain never passes.
    topical_domains: frozenset[str] | None = None
    #: The edge must carry at least one of these labels (unchecked / none found fails).
    sensitivities: frozenset[SensitivityLabel] | None = None
    context_locations: frozenset[str] | None = None
    participation_modes: frozenset[str] | None = None
    applicability_conditions: frozenset[str] | None = None
    min_extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_methods: frozenset[str] | None = None
    recurrence_series_ids: frozenset[str] | None = None
    completeness: frozenset[Completeness] | None = None
    min_independent_support: int | None = Field(default=None, ge=0)
    # ── RM-86 exclusions (deny-sets) ──
    exclude_sensitivities: frozenset[SensitivityLabel] | None = None
    exclude_claim_states: frozenset[ClaimState] | None = None
    exclude_lifecycle_statuses: frozenset[LifecycleStatus] | None = None
    exclude_assertion_modalities: frozenset[AssertionModality] | None = None
    exclude_topical_domains: frozenset[str] | None = None
    exclude_origin_types: frozenset[str] | None = None
    #: What ``exclude_sensitivities`` does with an edge whose sensitivity was not (fully)
    #: checked: ``"fail"`` drops it (default, the safe choice), ``"pass"`` keeps it.
    unchecked_sensitivity: Literal["pass", "fail"] = "fail"

    _aware = field_validator("valid_at", "known_as_of")(_require_aware)

    @field_validator("valid_window")
    @classmethod
    def _aware_window(
        cls, value: tuple[datetime, datetime] | None
    ) -> tuple[datetime, datetime] | None:
        if value is not None:
            _require_aware(value[0])
            _require_aware(value[1])
        return value

    @field_validator("topical_domains")
    @classmethod
    def _domain_ids(cls, value: frozenset[str] | None) -> frozenset[str] | None:
        bad = sorted(d for d in value or () if not is_domain_id(d))
        if bad:
            raise ValueError(f"topical_domains must be hierarchical domain ids: {bad}")
        return value

    @model_validator(mode="after")
    def _window_order(self) -> QualifierFilter:
        if self.valid_window is not None and self.valid_window[1] <= self.valid_window[0]:
            raise ValueError("valid_window end must be after start")
        return self

    @property
    def is_empty(self) -> bool:
        """No constraint set (``unchecked_sensitivity`` alone constrains nothing)."""
        return not self.model_dump(exclude_none=True, exclude=set(_MODIFIER_FIELDS))

    def matches(self, qualifiers: EdgeQualifiers | None) -> bool:
        """Whether an edge with ``qualifiers`` passes every constraint.

        An erased or erasure-pending edge never passes, not even the empty filter (RM-83).
        An edge without qualifiers is read as all-unknown: it fails every allow-set and
        passes the exclusions whose unknown rule passes."""
        if qualifiers is not None and not qualifiers.traversable:
            return False
        if self.is_empty:
            return True
        q = qualifiers if qualifiers is not None else _UNKNOWN
        if not self._passes_exclusions(q):
            return False
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
        if self.topical_domains is not None and not domain_in(
            q.topical_domain, self.topical_domains
        ):
            return False
        for field_name, attr in _PROVENANCE_FIELDS.items():
            allowed = getattr(self, field_name)
            if allowed is not None and (
                q.provenance is None or getattr(q.provenance, attr) not in allowed
            ):
                return False
        if self.sensitivities is not None and not (q.sensitivity or frozenset()) & (
            self.sensitivities
        ):
            return False
        if self.min_trust is not None and (q.trust is None or q.trust < self.min_trust):
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

    def _passes_exclusions(self, q: EdgeQualifiers) -> bool:
        """RM-86 deny-sets: a known excluded value drops; unknown follows the field's rule."""
        for field_name, attr in _EXCLUDE_FIELDS.items():
            excluded: frozenset[Any] | None = getattr(self, field_name)
            value = getattr(q, attr)
            if excluded is not None and value is not None and value in excluded:
                return False
        if (
            self.exclude_topical_domains is not None
            and q.topical_domain is not None
            and domain_in(q.topical_domain, self.exclude_topical_domains)
        ):
            return False  # RM-91: excluding a domain also excludes its descendants
        origin = q.provenance.origin_type if q.provenance is not None else None
        if self.exclude_origin_types is not None and origin in self.exclude_origin_types:
            return False
        if self.exclude_sensitivities is None:
            return True
        if q.sensitivity and q.sensitivity & self.exclude_sensitivities:
            return False  # labelled match
        if q.sensitivity_checked is True:
            return True  # complete check, no excluded label (incl. checked_none)
        return self.unchecked_sensitivity == "pass"  # not (fully) checked: caller decides

    def admits(self, edge: GraphEdge) -> bool:
        return self.matches(edge_qualifiers(edge.properties))


class CallerVisibility(_Frozen):
    """RM-88: who is reading, for the edge-scope layer of caller visibility.

    An edge passes this layer only when its ``scope`` qualifier is one of the caller's
    own scopes (the read namespace, or its typed owner key) or a scope the caller holds
    an active (non-revoked) grant on. An unknown scope never passes. The second layer,
    the source record's viewer check, is applied by the read path on the record itself.
    A grantee-only merge edge (RM-55) carries the grantee's scope, so its grantor, who
    holds no grant from the grantee, never sees it."""

    namespace: str
    #: Further scope strings that are the caller's own (e.g. the typed owner key).
    own_scopes: frozenset[str] = frozenset()
    #: Scopes the caller holds an active grant on (resolved fresh for every read).
    granted_scopes: frozenset[str] = frozenset()

    def admits_scope(self, qualifiers: EdgeQualifiers | None) -> bool:
        scope = qualifiers.scope if qualifiers is not None else None
        if scope is None:
            return False
        return scope == self.namespace or scope in self.own_scopes or scope in self.granted_scopes

    def admits(self, edge: GraphEdge) -> bool:
        return self.admits_scope(edge_qualifiers(edge.properties))


#: The qualifier filter of the read in progress (RM-93); None = no graph filter.
_ACTIVE: ContextVar[QualifierFilter | None] = ContextVar("memspine_graph_filter", default=None)


def active_graph_filter() -> QualifierFilter | None:
    """The graph qualifier filter of the current read, if any."""
    return _ACTIVE.get()


@contextmanager
def graph_filter_scope(qualifier_filter: QualifierFilter | None) -> Iterator[None]:
    """Bind ``qualifier_filter`` for the reads inside the block (an empty filter binds none)."""
    token = _ACTIVE.set(
        None if qualifier_filter is None or qualifier_filter.is_empty else qualifier_filter
    )
    try:
        yield
    finally:
        _ACTIVE.reset(token)


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
