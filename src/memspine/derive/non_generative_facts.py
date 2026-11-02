"""BI-06 non-generative, source-grounded fact candidates.

This is deliberately a candidate stage, not a writer.  It adapts the existing
conservative English rule miner to the current semantic vocabulary, pins each
candidate to an exact source span, and leaves every unsupported or ambiguous
outcome unresolved for the canonical compiler/integration door.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import PurePath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from memspine.core.rule_miner import mine_rules
from memspine.derive.compile import WindowLine
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.gliner_mentions import MentionSpan
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.ingestion import IngestionSourceSnapshot
from memspine.schemas.durable.semantic import (
    ClaimOutcome,
    ClaimProducer,
    ClaimQualifier,
    PredicateTyping,
    SemanticEnvelope,
)
from memspine.schemas.durable.semantic import RoleBinding as DurableRoleBinding
from memspine.schemas.durable.temporal import Interval, TemporalRef
from memspine.semantics import (
    QualifierValue,
    RoleBindingCheck,
    SemanticRegistry,
    load_builtin_registry,
)
from memspine.semantics.specs import EntityType, ValueKind
from memspine.store.records import MemoryRecord
from memspine.store.source_binding import source_binding

__all__ = [
    "ClaimTarget",
    "FactStageResult",
    "GroundedFactCandidate",
    "NonGenerativeFactExtractor",
    "PendingClaimChange",
    "UnresolvedAnnotation",
    "UnresolvedFactCandidate",
    "assertion_owner",
    "compile_grounded_facts",
    "document_owner",
    "mine_grounded_facts",
    "source_ineligible",
    "support_span",
]

_RULE_TO_PREDICATE = {
    "home": "resides_in",
    "employer": "employed_by",
    "likes": "likes",
    "dislikes": "dislikes",
    "plans": "has_goal",
}
_CORRECTION = re.compile(r"\b(?:correction|I meant|rather than|not .+ but)\b", re.IGNORECASE)
UnresolvedReason = Literal[
    #: the source is not authored by a user (an assistant, agent, tool or system turn): its
    #: text is never a user assertion (ADR-088 amendment 2026-11-02; renamed, no alias)
    "source_not_user",
    "missing_assertion_owner",
    "correction_requires_resolution",
    "unsupported_predicate",
    "unsupported_relation",
    "ambiguous_support_span",
    "scope_mismatch",
    "invalid_evidence",
    "invalid_assertion_owner",
    "semantic_validation_failed",
    "compiler_rejected",
    "extractor_failed",
    "grounding_unsupported",
    "subject_not_speaker",
    # RM-22 / RM-54 conservative rules (ADR-088 amendment 2026-11-02): each ambiguity stays an
    # unresolved, unasserted annotation naming why
    "correction_target_not_found",
    "correction_target_ambiguous",
    "cancellation_target_not_found",
    "cancellation_target_ambiguous",
    "ambiguous_antecedent",
    "ambiguous_move",
    "ambiguous_measurement",
    "approximate_value",
    "missing_unit",
    "missing_currency",
]
_OBJECT_TYPES: Mapping[str, tuple[ValueKind, EntityType | None]] = {
    "resides_in": ("entity", "place"),
    "employed_by": ("entity", "organisation"),
    "likes": ("text", None),
    "dislikes": ("text", None),
    "has_goal": ("text", None),
}


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ClaimTarget(_Candidate):
    """RM-22 / RM-54: the prior claim a candidate acts on, resolved against the store to one
    record before the candidate is compiled. ``supersede``: the candidate's claim replaces the
    target (an explicit correction by the same sender); ``close``: the candidate is the target
    with a lifecycle ``outcome`` (an explicit cancellation of a commitment)."""

    action: Literal["supersede", "close"]
    record_id: str = Field(min_length=1)


class GroundedFactCandidate(_Candidate):
    scope: OwnerScope
    subject: str = Field(min_length=1)
    subject_type: EntityType = "person"
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    object_kind: ValueKind = "text"
    object_entity_type: EntityType | None = None
    #: ``reported``: the statement belongs to its source (a document) and is attributed to it,
    #: never an assertion of whoever supplied the source
    modality: Literal["asserted", "planned", "reported"]
    polarity: Literal["positive", "negative"] = "positive"
    assertion_owner: str = Field(min_length=1)
    evidence: Evidence
    context_evidence: tuple[Evidence, ...] = ()
    role_bindings: tuple[DurableRoleBinding, ...] = ()
    #: RM-157: registry-checked claim qualifiers (``participation_mode``, ...), passed through.
    qualifiers: Mapping[str, ClaimQualifier] = Field(default_factory=dict)
    cardinality_scope: Mapping[str, str] = Field(default_factory=dict)
    source_time: str | None = None
    #: RM-54: the typed value of a property predicate, passed to registry validation as is:
    #: the registered property key, the explicit unit or ISO 4217 code, the Decimal amount as
    #: written (never converted or guessed) and the property's explicit context qualifiers
    attribute_key: str | None = None
    unit: str | None = None
    currency: str | None = None
    amount: Decimal | None = None
    context: Mapping[str, str] = Field(default_factory=dict)
    #: RM-57: the lifecycle outcome the claim carries (``none`` unless it closes a claim)
    outcome: ClaimOutcome = "none"
    #: RM-22 / RM-54: the resolved prior claim this candidate supersedes or closes
    target: ClaimTarget | None = None
    #: D-100 Q-S2: the event start at source precision, resolved only against an aware source
    #: time; None when the source states no event time the rules can read
    event_start: TemporalRef | None = None
    #: the source origin the registry checks for the predicate
    source_origin: Literal["raw_message", "document"] = "raw_message"
    producer_revision: str = Field(default="personal-rules:1", min_length=1)


class PendingClaimChange(_Candidate):
    """RM-22 / RM-54: an explicit change to a prior claim of the same sender that the source
    names but cannot resolve alone: a correction (``I meant X, not Y``) or the cancellation of
    a commitment. The window resolves it against the stored claims
    (:func:`memspine.derive.discourse_rules.resolve_pending`) to one grounded candidate with a
    :class:`ClaimTarget`, or to an unresolved annotation naming why."""

    kind: Literal["correction", "cancellation"]
    scope: OwnerScope
    owner: str = Field(min_length=1)
    evidence: Evidence
    #: correction: the value replaced; cancellation: the commitment's object as written
    old_value: str = Field(min_length=1)
    #: correction: the value that replaces it
    new_value: str | None = None
    #: cancellation: the RM-57 outcome it sets
    outcome: Literal["declined", "missed"] | None = None
    #: the message id the source replies to; it narrows the target to that source's claims
    reply_to: str | None = None
    producer_revision: str = Field(default="personal-rules:1", min_length=1)


class UnresolvedFactCandidate(_Candidate):
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    reason: UnresolvedReason
    attribute: str | None = None
    value: str | None = None
    #: RM-42: where in the source the candidate was read (None when it has no exact span)
    span: Span | None = None
    producer_revision: str = Field(default="personal-rules:1", min_length=1)


class UnresolvedAnnotation(BaseModel):
    """ADR-090 item 5 (RM-42): an unresolved or unmapped fact candidate, kept as a source-bound,
    unasserted annotation (reason, span, producer revision). It is never read as a fact: it is
    stored only as the window effect's artifact, so it is erased with its source like every
    effect, and the next rules run over the source (policy or registry activation) replaces it.
    The span is a character range of the source; the excerpt itself is not copied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: IngestionSourceSnapshot
    reason: UnresolvedReason
    attribute: str | None = None
    value: str | None = None
    span: Span | None = None
    producer_revision: str = Field(min_length=1)
    asserted: Literal[False] = False

    @classmethod
    def of(
        cls, candidate: UnresolvedFactCandidate, source: IngestionSourceSnapshot
    ) -> UnresolvedAnnotation:
        if candidate.source_id != source.record_id:
            raise ValueError("unresolved candidate is not bound to this source")
        span = candidate.span
        return cls(
            source=source,
            reason=candidate.reason,
            attribute=candidate.attribute,
            value=candidate.value,
            span=Span(start=span.start, end=span.end) if span is not None else None,
            producer_revision=candidate.producer_revision,
        )


class FactStageResult(_Candidate):
    facts: tuple[GroundedFactCandidate, ...] = ()
    unresolved: tuple[UnresolvedFactCandidate, ...] = ()
    #: DS-18: exact, unasserted source mentions the extractor found (the window binds them to
    #: the source as its annotation batch; they never key or assert a fact)
    mentions: tuple[MentionSpan, ...] = ()
    #: RM-22 / RM-54: corrections and cancellations awaiting resolution against the store
    pending: tuple[PendingClaimChange, ...] = ()
    generative_calls: Literal[0] = 0


class CompiledFactStage(_Candidate):
    envelopes: tuple[SemanticEnvelope, ...] = ()
    unresolved: tuple[UnresolvedFactCandidate, ...] = ()
    #: index into ``envelopes`` -> the resolved prior claim that envelope supersedes or closes
    targets: Mapping[int, ClaimTarget] = Field(default_factory=dict)
    generative_calls: Literal[0] = 0


class NonGenerativeFactExtractor(Protocol):
    """Optional configured extractor contract; deliberately has no LLM fallback."""

    identity: str

    async def extract(self, record: MemoryRecord, scope: OwnerScope) -> FactStageResult: ...


def document_owner(record: MemoryRecord) -> str | None:
    """RM-54: the attribution of an ingested document chunk (``document:<file name>``); None
    for every other source. A document's statements are its own, never the user's."""
    path = record.source.doc_path
    if (
        record.memory_type != "resource"
        or record.source.channel != "ingest"
        or not path
        or record.source.message is not None
    ):
        return None
    name = PurePath(path.replace("\\", "/")).name
    return f"document:{name}" if name else None


def _owner(record: MemoryRecord) -> str | None:
    message = record.source.message or {}
    sender = message.get("sender") if isinstance(message, dict) else None
    value = sender.get("id") if isinstance(sender, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return document_owner(record)


def _support_span(text: str, value: str) -> Span | None:
    starts = [match.start() for match in re.finditer(re.escape(value), text, re.IGNORECASE)]
    if len(starts) != 1:
        return None
    value_start = starts[0]
    left = max(text.rfind(mark, 0, value_start) for mark in ".!?;\n") + 1
    right_candidates = [pos for mark in ".!?;\n" if (pos := text.find(mark, value_start)) >= 0]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    while left < right and text[left].isspace():
        left += 1
    quote = text[left:right]
    return Span(start=left, end=right, quote=quote)


def _unresolved(
    candidate: GroundedFactCandidate, reason: UnresolvedReason
) -> UnresolvedFactCandidate:
    turn = candidate.evidence.turn
    return UnresolvedFactCandidate(
        source_id=turn.source_id if turn is not None else "uncited",
        source_version=turn.source_version if turn is not None else 1,
        reason=reason,
        attribute=candidate.predicate,
        value=candidate.object,
        span=candidate.evidence.span,
        producer_revision=candidate.producer_revision,
    )


def _evidence_kind(evidence: Sequence[Evidence]) -> Literal["exact_span", "envelope"] | None:
    if any(item.span is not None for item in evidence):
        return "exact_span"
    if any(item.turn is not None for item in evidence):
        return "envelope"
    return None


def _evidence_is_valid(
    evidence: Evidence,
    records: Mapping[str, MemoryRecord],
    scope: OwnerScope,
) -> bool:
    turn = evidence.turn
    if turn is None:
        return False
    record = records.get(turn.source_id)
    if record is None or record.version != turn.source_version or record.namespace != scope.owner:
        return False
    span = evidence.span
    if span is None:
        return True
    if not (0 <= span.start < span.end <= len(record.content)):
        return False
    return bool(span.quote == record.content[span.start : span.end])


def _binding_check(binding: DurableRoleBinding) -> RoleBindingCheck:
    return RoleBindingCheck(
        role=binding.role_name,
        value_kind=binding.value_kind,
        entity_type=binding.entity_type,
        evidence=_evidence_kind(binding.evidence),
        unit=binding.unit,
        amount=binding.amount,
    )


def compile_grounded_facts(
    result: FactStageResult,
    sources: Sequence[MemoryRecord],
    scope: OwnerScope,
    *,
    compiler: EnvelopeCompiler | None = None,
    registry: SemanticRegistry | None = None,
) -> CompiledFactStage:
    """Validate and compile deterministic candidates through the canonical envelope path.

    Support evidence must cite the authoritative turn in ``sources``. Binding/context
    evidence may cite another supplied turn, but it must have the same owner scope and
    pinned version. Every failure stays unresolved; this adapter never retries through a
    generative service and never writes a record.
    """
    compiler = compiler or EnvelopeCompiler(prompt_hash="non-generative:1")
    registry = registry or load_builtin_registry()
    records = {record.record_id: record for record in sources}
    envelopes: list[SemanticEnvelope] = []
    targets: dict[int, ClaimTarget] = {}
    unresolved = list(result.unresolved)
    for candidate in result.facts:
        turn = candidate.evidence.turn
        source = records.get(turn.source_id) if turn is not None else None
        if candidate.scope != scope or source is None or source.namespace != scope.owner:
            unresolved.append(_unresolved(candidate, "scope_mismatch"))
            continue
        all_evidence = (
            candidate.evidence,
            *candidate.context_evidence,
            *(item for binding in candidate.role_bindings for item in binding.evidence),
        )
        if any(not _evidence_is_valid(item, records, scope) for item in all_evidence):
            unresolved.append(_unresolved(candidate, "invalid_evidence"))
            continue
        if _owner(source) != candidate.assertion_owner:
            unresolved.append(_unresolved(candidate, "invalid_assertion_owner"))
            continue
        if candidate.modality == "reported" and candidate.source_origin != "document":
            # Only a document's own statement is reported by the rules (RM-54); a person's
            # report of another source asserts nothing and is never compiled.
            unresolved.append(_unresolved(candidate, "semantic_validation_failed"))
            continue
        cardinality_scope = dict(candidate.cardinality_scope)
        if candidate.attribute_key is not None:
            cardinality_scope.setdefault("attribute_key", candidate.attribute_key)
        try:
            spec = registry.validate_compatibility(
                candidate.predicate,
                subject_type=candidate.subject_type,
                value_kind=candidate.object_kind,
                object_entity_type=candidate.object_entity_type,
                attribute_key=candidate.attribute_key,
                unit=candidate.unit,
                currency=candidate.currency,
                amount=candidate.amount,
                role_names=(binding.role_name for binding in candidate.role_bindings),
                qualifiers={
                    key: QualifierValue.model_validate(value.model_dump())
                    for key, value in candidate.qualifiers.items()
                },
                context=dict(candidate.context) or None,
                source_origin=candidate.source_origin,
            )
            if (
                candidate.amount is not None
                and candidate.object_kind == "number"
                and Decimal(candidate.object) != candidate.amount
            ):
                raise ValueError("the claim object must be the typed amount as written")
            registry.validate_role_bindings(
                candidate.predicate,
                (_binding_check(binding) for binding in candidate.role_bindings),
            )
            for binding in candidate.role_bindings:
                role = registry.roles[binding.role_name]
                if binding.role_version != role.version:
                    raise ValueError("role binding version differs from active role")
            if any(not cardinality_scope.get(key) for key in spec.cardinality_scope):
                raise ValueError("predicate cardinality requires explicit scope")
        except (KeyError, ValueError, ArithmeticError):
            unresolved.append(_unresolved(candidate, "semantic_validation_failed"))
            continue

        aliases: dict[tuple[str, int], int] = {}
        window: dict[int, WindowLine] = {}
        cited: list[int] = []
        for evidence in all_evidence:
            cited_turn = evidence.turn
            assert cited_turn is not None
            key = (cited_turn.source_id, cited_turn.source_version)
            alias = aliases.setdefault(key, len(aliases) + 1)
            cited.append(alias)
            record = records[cited_turn.source_id]
            if alias in window:
                continue
            window[alias] = WindowLine(
                turn_id=f"{record.record_id}:{record.version}",
                source_id=record.record_id,
                source_version=record.version,
                seg_index=cited_turn.seg_index,
                # a document chunk is document content, whoever ingested it (role policy)
                role="document" if document_owner(record) else record.source.role,
                speaker=_owner(record),
                source_at=record.source_at or record.valid_from,
                content=record.content,
                is_context=evidence is not candidate.evidence,
                binding_hash=source_binding(record).binding_hash,
            )
        ctx = compiler.context(
            scope,
            source.record_id,
            window,
            source_version=source.version,
            source_time=source.source_at or source.valid_from,
            principal=candidate.assertion_owner,
            # ADR-088: the claim binds to the source record snapshot it was mined from.
            source_binding_hash=source_binding(source).binding_hash,
        )
        compiled = compiler.compile(
            [
                {
                    "text": f"{candidate.subject} {candidate.predicate} {candidate.object}",
                    # an append-only contextual property (a measurement) never supersedes
                    "kind": (
                        "state"
                        if spec.cardinality == "one"
                        or (
                            spec.cardinality == "contextual"
                            and spec.transition_policy != "append_only"
                        )
                        else "event"
                    ),
                    "s": candidate.subject,
                    "r": candidate.predicate,
                    "o": candidate.object,
                    # a reported claim compiles as the source's assertion; its modality is
                    # set to ``reported`` on the assessment below
                    "mode": "asserted" if candidate.modality == "reported" else candidate.modality,
                    "neg": candidate.polarity == "negative",
                    "lines": sorted(set(cited)),
                    "span": candidate.evidence.span.quote if candidate.evidence.span else "",
                }
            ],
            ctx,
        )
        if len(compiled.envelopes) != 1:
            unresolved.append(_unresolved(candidate, "compiler_rejected"))
            continue
        envelope = compiled.envelopes[0]
        typing = PredicateTyping(
            predicate=candidate.predicate,
            predicate_version=spec.version,
            registry_digest=registry.digest,
            subject_type=candidate.subject_type,
            object_kind=candidate.object_kind,
            object_entity_type=candidate.object_entity_type,
            cardinality=spec.cardinality,
            cardinality_scope=cardinality_scope,
            attribute_key=candidate.attribute_key,
            unit=candidate.unit,
            currency=candidate.currency,
            amount=candidate.amount,
        )
        update: dict[str, object] = {
            "registry": typing,
            "role_bindings": [
                DurableRoleBinding.model_validate(binding.model_dump(), strict=True)
                for binding in candidate.role_bindings
            ],
            "producer": ClaimProducer(kind="rules", revision=candidate.producer_revision),
            "outcome": candidate.outcome,
            # RM-157: qualifiers and the statement kind pass through to the claim
            "qualifiers": dict(candidate.qualifiers),
            "statement_kind": spec.statement_kind,
        }
        assessment_update: dict[str, object] = {}
        if candidate.modality == "reported":
            assessment_update["modality"] = "reported"
        if candidate.event_start is not None:
            update["time"] = envelope.time.model_copy(
                update={"event": Interval(start=candidate.event_start)}
            )
            assessment_update["time_precision"] = candidate.event_start.precision
        if assessment_update:
            update["assessment"] = envelope.assessment.model_copy(update=assessment_update)
        envelope = envelope.model_copy(update=update)
        # Revalidate after attaching the registry typing; do not trust model_copy.
        try:
            checked = SemanticEnvelope.model_validate(envelope.model_dump(), strict=True)
        except ValueError:
            unresolved.append(_unresolved(candidate, "compiler_rejected"))
            continue
        if candidate.target is not None:
            targets[len(envelopes)] = candidate.target
        envelopes.append(checked)
    return CompiledFactStage(
        envelopes=tuple(envelopes), unresolved=tuple(unresolved), targets=targets
    )


def assertion_owner(record: MemoryRecord) -> str | None:
    """The source's sender id (the only assertion owner a non-generative candidate may have)."""
    return _owner(record)


def support_span(text: str, start: int, end: int) -> Span | None:
    """The sentence of ``text`` holding ``[start, end)``; None when a sentence mark falls
    inside the range (the parts belong to different sentences)."""
    if not (0 <= start < end <= len(text)):
        return None
    if any(mark in text[start:end] for mark in ".!?;\n"):
        return None
    left = max(text.rfind(mark, 0, start) for mark in ".!?;\n") + 1
    rights = [pos for mark in ".!?;\n" if (pos := text.find(mark, end)) >= 0]
    right = min(rights) + 1 if rights else len(text)
    while left < start and text[left].isspace():
        left += 1
    return Span(start=left, end=right, quote=text[left:right])


def source_ineligible(record: MemoryRecord) -> FactStageResult | None:
    """The shared eligibility gate of every non-generative producer: only a user-authored
    message with a sender id and no correction cue yields user candidates. The unresolved
    result when the source is not eligible, else None. A document chunk is not a user source
    (``source_not_user``): only :func:`mine_grounded_facts` reads its own reported statements."""
    reason: UnresolvedReason | None = None
    if record.source.role != "user" or document_owner(record) is not None:
        reason = "source_not_user"
    elif _owner(record) is None:
        reason = "missing_assertion_owner"
    elif _CORRECTION.search(record.content):
        reason = "correction_requires_resolution"
    if reason is None:
        return None
    return FactStageResult(
        unresolved=(
            UnresolvedFactCandidate(
                source_id=record.record_id, source_version=record.version, reason=reason
            ),
        )
    )


def mine_grounded_facts(record: MemoryRecord, scope: OwnerScope) -> FactStageResult:
    """Return deterministic grounded candidates for one authoritative raw turn.

    Only user-authored records yield user facts; an ingested document chunk yields only its own
    ``reported`` statements, attributed to the document (RM-54). An explicit correction or
    cancellation is returned as a pending change for the window to resolve against the store
    (RM-22). The miner is invoked directly and has no LLM/provider seam, making the
    zero-generative-call guarantee structural rather than an accounting convention.
    """
    from memspine.derive import discourse_rules

    source_id = record.record_id
    source_version = record.version
    document = document_owner(record)
    if document is not None:
        ruled = discourse_rules.document_rules(record, scope, document)
        return FactStageResult(facts=tuple(ruled.facts), unresolved=tuple(ruled.unresolved))
    ineligible = source_ineligible(record)
    if ineligible is not None:
        owner = _owner(record)
        if (
            owner is not None
            and record.source.role == "user"
            and ineligible.unresolved[0].reason == "correction_requires_resolution"
        ):
            change = discourse_rules.pending_changes(record, scope, owner)
            if change.pending:
                return FactStageResult(pending=tuple(change.pending))
        return ineligible
    owner = _owner(record)
    assert owner is not None  # source_ineligible checked it

    source_at = record.source_at or record.valid_from
    day = source_at.date().isoformat()
    transcript = f"[1] [{day}] {owner}: {record.content}"
    facts: list[GroundedFactCandidate] = []
    unresolved: list[UnresolvedFactCandidate] = []
    for mined in mine_rules(transcript):
        predicate = _RULE_TO_PREDICATE.get(mined.attribute)
        if predicate is None:
            unresolved.append(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="unsupported_predicate",
                    attribute=mined.attribute,
                    value=mined.value,
                    span=_support_span(record.content, mined.value),
                )
            )
            continue
        if predicate == "employed_by" and re.search(r"\b(?:in|at|for)\b", mined.value):
            unresolved.append(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="unsupported_relation",
                    attribute=mined.attribute,
                    value=mined.value,
                    span=_support_span(record.content, mined.value),
                )
            )
            continue
        support = _support_span(record.content, mined.value)
        if support is None:
            unresolved.append(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="ambiguous_support_span",
                    attribute=mined.attribute,
                    value=mined.value,
                )
            )
            continue
        facts.append(
            GroundedFactCandidate(
                scope=scope,
                subject=owner,
                predicate=predicate,
                object=mined.value,
                object_kind=_OBJECT_TYPES[predicate][0],
                object_entity_type=_OBJECT_TYPES[predicate][1],
                modality="planned" if mined.attribute == "plans" else "asserted",
                assertion_owner=owner,
                evidence=Evidence(
                    role="support",
                    turn=TurnRef(
                        source_id=source_id,
                        source_version=source_version,
                        seg_index=0,
                    ),
                    span=support,
                    source_family=owner,
                ),
                source_time=source_at.isoformat(),
            )
        )
    # RM-22 / RM-54 (ADR-088 amendment 2026-11-02): moves, visits, pronoun coreference and
    # typed measurements; an explicit cancellation is a pending change of a prior claim.
    ruled = discourse_rules.message_rules(record, scope, owner)
    held = {(f.subject, f.predicate, f.object.casefold()) for f in facts}
    for fact in ruled.facts:
        if (fact.subject, fact.predicate, fact.object.casefold()) not in held:
            held.add((fact.subject, fact.predicate, fact.object.casefold()))
            facts.append(fact)
    unresolved.extend(ruled.unresolved)
    change = discourse_rules.pending_changes(record, scope, owner)
    return FactStageResult(
        facts=tuple(facts), unresolved=tuple(unresolved), pending=tuple(change.pending)
    )
