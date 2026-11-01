"""BI-06 non-generative, source-grounded fact candidates.

This is deliberately a candidate stage, not a writer.  It adapts the existing
conservative English rule miner to the current semantic vocabulary, pins each
candidate to an exact source span, and leaves every unsupported or ambiguous
outcome unresolved for the canonical compiler/integration door.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from memspine.core.rule_miner import mine_rules
from memspine.derive.compile import WindowLine
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import ClaimProducer, PredicateTyping, SemanticEnvelope
from memspine.schemas.durable.semantic import RoleBinding as DurableRoleBinding
from memspine.semantics import RoleBindingCheck, SemanticRegistry, load_builtin_registry
from memspine.semantics.specs import EntityType, ValueKind
from memspine.store.records import MemoryRecord
from memspine.store.source_binding import source_binding

__all__ = [
    "FactStageResult",
    "GroundedFactCandidate",
    "NonGenerativeFactExtractor",
    "UnresolvedFactCandidate",
    "compile_grounded_facts",
    "mine_grounded_facts",
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
    "assistant_proposal",
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


class GroundedFactCandidate(_Candidate):
    scope: OwnerScope
    subject: str = Field(min_length=1)
    subject_type: EntityType = "person"
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    object_kind: ValueKind = "text"
    object_entity_type: EntityType | None = None
    modality: Literal["asserted", "planned"]
    polarity: Literal["positive", "negative"] = "positive"
    assertion_owner: str = Field(min_length=1)
    evidence: Evidence
    context_evidence: tuple[Evidence, ...] = ()
    role_bindings: tuple[DurableRoleBinding, ...] = ()
    cardinality_scope: Mapping[str, str] = Field(default_factory=dict)
    source_time: str | None = None
    producer_revision: str = Field(default="personal-rules:1", min_length=1)


class UnresolvedFactCandidate(_Candidate):
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    reason: UnresolvedReason
    attribute: str | None = None
    value: str | None = None


class FactStageResult(_Candidate):
    facts: tuple[GroundedFactCandidate, ...] = ()
    unresolved: tuple[UnresolvedFactCandidate, ...] = ()
    generative_calls: Literal[0] = 0


class CompiledFactStage(_Candidate):
    envelopes: tuple[SemanticEnvelope, ...] = ()
    unresolved: tuple[UnresolvedFactCandidate, ...] = ()
    generative_calls: Literal[0] = 0


class NonGenerativeFactExtractor(Protocol):
    """Optional configured extractor contract; deliberately has no LLM fallback."""

    identity: str

    async def extract(self, record: MemoryRecord, scope: OwnerScope) -> FactStageResult: ...


def _owner(record: MemoryRecord) -> str | None:
    message = record.source.message or {}
    sender = message.get("sender") if isinstance(message, dict) else None
    value = sender.get("id") if isinstance(sender, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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
        try:
            spec = registry.validate_compatibility(
                candidate.predicate,
                subject_type=candidate.subject_type,
                value_kind=candidate.object_kind,
                object_entity_type=candidate.object_entity_type,
                role_names=(binding.role_name for binding in candidate.role_bindings),
                source_origin="raw_message",
            )
            registry.validate_role_bindings(
                candidate.predicate,
                (_binding_check(binding) for binding in candidate.role_bindings),
            )
            for binding in candidate.role_bindings:
                role = registry.roles[binding.role_name]
                if binding.role_version != role.version:
                    raise ValueError("role binding version differs from active role")
            if any(not candidate.cardinality_scope.get(key) for key in spec.cardinality_scope):
                raise ValueError("predicate cardinality requires explicit scope")
        except (KeyError, ValueError):
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
                role=record.source.role,
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
                    "kind": ("state" if spec.cardinality in {"one", "contextual"} else "event"),
                    "s": candidate.subject,
                    "r": candidate.predicate,
                    "o": candidate.object,
                    "mode": candidate.modality,
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
            cardinality_scope=dict(candidate.cardinality_scope),
        )
        envelope = envelope.model_copy(
            update={
                "registry": typing,
                "role_bindings": [
                    DurableRoleBinding.model_validate(binding.model_dump(), strict=True)
                    for binding in candidate.role_bindings
                ],
                "producer": ClaimProducer(kind="rules", revision=candidate.producer_revision),
            }
        )
        # Revalidate after attaching the registry typing; do not trust model_copy.
        envelopes.append(SemanticEnvelope.model_validate(envelope.model_dump(), strict=True))
    return CompiledFactStage(envelopes=tuple(envelopes), unresolved=tuple(unresolved))


def mine_grounded_facts(record: MemoryRecord, scope: OwnerScope) -> FactStageResult:
    """Return deterministic grounded candidates for one authoritative raw turn.

    Only user-authored records are eligible.  The miner is invoked directly and
    has no LLM/provider seam, making the zero-generative-call guarantee
    structural rather than an accounting convention.
    """
    source_id = record.record_id
    source_version = record.version
    if record.source.role != "user":
        return FactStageResult(
            unresolved=(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="assistant_proposal",
                ),
            )
        )
    owner = _owner(record)
    if owner is None:
        return FactStageResult(
            unresolved=(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="missing_assertion_owner",
                ),
            )
        )
    if _CORRECTION.search(record.content):
        return FactStageResult(
            unresolved=(
                UnresolvedFactCandidate(
                    source_id=source_id,
                    source_version=source_version,
                    reason="correction_requires_resolution",
                ),
            )
        )

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
    return FactStageResult(facts=tuple(facts), unresolved=tuple(unresolved))
