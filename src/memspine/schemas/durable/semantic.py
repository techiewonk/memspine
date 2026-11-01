"""The one canonical claim: :class:`SemanticEnvelope` (ADR-088, schema sessions 2-6; W-15 C, M-06).

Every claim, rules-only or model-produced, is one ``SemanticEnvelope``. The model authors content
only, with citations (S2-1); everything else here is system-set. Unknown model fields live in
``extras`` (not indexed, not used by supersede, graph or profile logic).

The claim carries three separate typed axes, each with its existing vocabulary
(docs/ENRICHMENT_SCHEMA_CONTRACTS.md section 3, RM-57): ``assessment.modality`` (asserted, negated,
hypothetical, planned, reported, question), ``status`` (active, tentative, contested, superseded,
hidden) and ``outcome`` (none, done, missed, failed, declined). ADR-088 moves them from edge-only
qualifiers onto the claim; graph edges are compiled from the claim (RM-92). Only an asserted
claim reads as an asserted fact (:attr:`SemanticEnvelope.asserts_fact`).
Registry typing (``registry``) and role bindings are present when the predicate resolved in the
semantic registry (the rules-only producer); a model-path claim may leave them empty.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar, Literal, get_args

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from memspine.schemas.base import DurableModel, DurablePart
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import Fingerprint, OwnerScope, Provenance, SrcClass
from memspine.schemas.durable.entity_mentions import EntityMention as GlinerMentionAnnotation
from memspine.schemas.durable.evidence import Evidence
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes
from memspine.schemas.reserved import reserved_hits

__all__ = [
    "CLAIM_OUTCOMES",
    "AssertionStatus",
    "ClaimOutcome",
    "ClaimProducer",
    "GroundingChecks",
    "IntegrationVerdict",
    "Mention",
    "PredicateTyping",
    "RoleBinding",
    "SemanticEnvelope",
    "inherit_trust",
]

#: Claim status (ENRICHMENT_SCHEMA_CONTRACTS section 3); changes require canonical events.
AssertionStatus = Literal["active", "tentative", "contested", "superseded", "hidden"]
#: Claim outcome: ``none`` or the RM-57 lifecycle outcome (done, missed, failed, declined). The
#: schemas tier imports no other memspine module, so this spells out
#: ``memspine.semantics.lifecycle.LifecycleOutcome``; a unit test pins the two equal.
ClaimOutcome = Literal["none", "done", "missed", "failed", "declined"]
CLAIM_OUTCOMES: tuple[str, ...] = get_args(ClaimOutcome)
Grounding = Literal["grounded", "uncertain", "rejected"]
Scalar = str | int | float | bool
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


class Mention(DurablePart):
    """A surface mention as written, with its system-assigned mention id and resolved entity (if any)."""

    surface: str = Field(min_length=1)
    mention_id: str = Field(min_length=1)
    entity_id: str | None = None  # None until resolved; a provisional id may be set (I-5)


class GroundingChecks(DurablePart):
    """Per-check grounding results (W-17); ``None`` = check not run."""

    span: bool | None = None
    numbers: bool | None = None
    polarity: bool | None = None
    names: bool | None = None
    endpoints: bool | None = None


class IntegrationVerdict(DurablePart):
    """How the conflict ladder integrated this claim (G0 operator split), set at integration."""

    op: Literal["append", "supersede", "merge", "coexist", "contest", "noop", "defer_kc"]
    rung: str | None = None
    hint: str | None = None


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


class PredicateTyping(DurablePart):
    """The registry interpretation of the claim's predicate (pinned version and digest)."""

    predicate: str = Field(min_length=1)
    predicate_version: int = Field(ge=1)
    registry_digest: str = Field(min_length=64, max_length=64)
    subject_type: EntityType
    object_kind: ValueKind
    object_entity_type: EntityType | None = None
    cardinality: Literal["one", "many", "contextual", "unknown"]
    cardinality_scope: dict[str, str] = Field(default_factory=dict)


class ClaimProducer(DurablePart):
    """Which producer wrote the claim: the rules-only miner, the model path, or a record projection."""

    kind: Literal["rules", "model", "record"]
    revision: str = Field(min_length=1)


class SemanticEnvelope(DurableModel):
    """One stored claim (ADR-088).

    Identities are separate fields (ER-13, S3): ``write_id`` (retry id of the write that produced it),
    mention ids (on :class:`Mention`), ``assertion_key``, ``occurrence_id`` and ``record_id`` (``None``
    only until the write door assigns it). ``scope`` is tenant plus owner; the session is provenance.
    Modality lives on ``assessment`` (with binding and trust); ``status`` and ``outcome`` are
    claim fields.
    """

    __kind__: ClassVar[str | None] = "semantic_envelope"

    # --- identities (system-authored) ---
    write_id: str = Field(min_length=1)
    assertion_key: AssertionKey
    key_aliases: list[str] = Field(default_factory=list)
    occurrence_id: str = Field(min_length=1)
    record_id: str | None = None
    scope: OwnerScope
    provenance: Provenance = Field(default_factory=Provenance)

    # --- content (model-authored, S2-1) ---
    text: str = Field(min_length=1)
    kind: str = Field(min_length=1)  # open set (S2-3); normalised by the kind registry (MOD-02)
    subject: Mention | None = None
    object: Mention | None = None
    attribute: str | None = None
    value: Scalar | None = None
    speaker: str | None = None
    change_hint: bool = (
        False  # I-3: extraction marked that the cited lines say the earlier value no longer holds
    )

    # --- time, evidence, assessment ---
    time: TimeAxes = Field(default_factory=TimeAxes)
    #: ADR-088: when the claim became known (evidence-known time, D-100), timezone-aware only.
    #: The producer sets it to the time the claim was recorded; ``None`` only before a producer
    #: stamps it.
    known_at: AwareDatetime | None = None
    #: ADR-088: the binding hash of the source snapshot the claim was derived from (record id,
    #: version, text fingerprint and message envelope; ``memspine.store.source_binding``), the
    #: same hash a GLiNER mention carries. ``None`` when the producer has no single source record.
    source_binding_hash: str | None = Field(default=None, min_length=16, max_length=16)
    evidence: list[Evidence] = Field(default_factory=list)
    assessment: Assessment

    # --- claim outcome (ADR-088, RM-57); modality is on ``assessment``, status below ---
    outcome: ClaimOutcome = "none"

    # --- registry typing, role bindings and producer (ADR-088) ---
    registry: PredicateTyping | None = None
    role_bindings: list[RoleBinding] = Field(default_factory=list)
    producer: ClaimProducer | None = None

    # --- system state ---
    src_class: SrcClass
    parents: list[str] = Field(default_factory=list)
    status: AssertionStatus = "active"
    verified: bool = False
    epoch: int | None = Field(default=None, ge=1)  # fencing epoch of the job that wrote it (ER-07)
    erasure_generation: int = Field(default=0, ge=0)
    grounding: Grounding | None = None
    checks: GroundingChecks | None = None
    prompt_hash: str | None = None
    derivation: Fingerprint | None = None
    graph: dict[str, Any] | None = None  # code-compiled projection, never model output
    verdict: IntegrationVerdict | None = None

    extras: dict[str, Any] = Field(default_factory=dict)

    @property
    def mention_ids(self) -> list[str]:
        return [m.mention_id for m in (self.subject, self.object) if m is not None]

    @property
    def asserts_fact(self) -> bool:
        """True only for an asserted, visible claim (ADR-088): planned, hypothetical, question,
        reported and negated claims never read as asserted facts, nor do hidden ones."""
        return self.assessment.modality == "asserted" and self.status != "hidden"

    def _check_outcome(self) -> None:
        # RM-57: an outcome closes a commitment, work item or event; a hypothetical or an open
        # question has nothing to close.
        if self.outcome != "none" and self.assessment.modality in ("hypothetical", "question"):
            raise ValueError("a hypothetical or question claim carries no outcome")

    def _check_registry(self) -> None:
        if self.registry is not None and self.attribute != self.registry.predicate:
            raise ValueError("registry predicate must equal the canonical claim attribute")
        if not self.role_bindings:
            return
        ids = [binding.binding_id for binding in self.role_bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("role binding ids must be unique")
        cited = {
            (item.turn.source_id, item.turn.source_version)
            for item in self.evidence
            if item.turn is not None
        }
        for binding in self.role_bindings:
            if any(
                item.turn is None or (item.turn.source_id, item.turn.source_version) not in cited
                for item in binding.evidence
            ):
                raise ValueError("role binding evidence is not cited by the claim")

    def _check_gliner_extras(self) -> None:
        gliner_key = next(
            (
                key
                for key in self.extras
                if isinstance(key, str) and key.lower() == "gliner_mentions"
            ),
            None,
        )
        if gliner_key is not None and gliner_key != "gliner_mentions":
            raise ValueError("system annotation extras key must use canonical spelling")
        ordinary_extras = {
            key: value for key, value in self.extras.items() if key != "gliner_mentions"
        }
        hits = reserved_hits(ordinary_extras)
        if hits:
            raise ValueError(f"extras may not contain reserved names: {hits}")
        if gliner_key is None:
            return
        raw_mentions = self.extras["gliner_mentions"]
        if not isinstance(raw_mentions, list):
            raise ValueError("gliner_mentions must be a list of source-bound annotations")
        cited_versions = {
            evidence.turn.source_id: evidence.turn.source_version
            for evidence in self.evidence
            if evidence.turn is not None
        }
        try:
            for annotation in raw_mentions:
                mention = GlinerMentionAnnotation.model_validate(annotation, strict=True)
                if mention.source_namespace != self.scope.owner:
                    raise ValueError("GLiNER annotation namespace differs from envelope scope")
                if cited_versions.get(mention.source_record_id) != mention.source_version:
                    raise ValueError("GLiNER annotation source is not cited at this version")
        except (ValidationError, TypeError) as exc:
            raise ValueError("gliner_mentions contains an invalid annotation") from exc

    @model_validator(mode="after")
    def _invariants(self) -> SemanticEnvelope:
        # ER-32 / S2-1: nothing is stored without a citation (derived records cite through lineage).
        cited = [e for e in self.evidence if e.counts_as_evidence]
        derived_with_parents = self.src_class == "derived" and bool(self.parents)
        if not cited and not derived_with_parents:
            raise ValueError("a claim needs at least one support or binding evidence")
        if (
            self.assessment.modality == "asserted"
            and not derived_with_parents
            and not any(e.role == "support" for e in self.evidence)
        ):
            raise ValueError("an asserted item requires support evidence")
        # S5-1: time_precision is the precision of the event time, not a second source of truth.
        if self.assessment.time_precision != self.time.event_precision:
            raise ValueError(
                f"assessment.time_precision {self.assessment.time_precision!r} disagrees with the "
                f"event time precision {self.time.event_precision!r}"
            )
        self._check_outcome()
        # Session 2 condition 1: reserved system names are never stored as extras.
        self._check_gliner_extras()
        self._check_registry()
        ids = self.mention_ids
        if len(set(ids)) != len(ids):
            raise ValueError("mention ids must be distinct")
        return self


def inherit_trust(parent_trusts: list[float], proposed: float) -> float:
    """G1: a deposit inherits ``tau <= min over its parents``. Returns the bounded trust."""
    if not parent_trusts:
        return proposed
    return min(proposed, min(parent_trusts))
