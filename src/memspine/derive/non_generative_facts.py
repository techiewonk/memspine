"""BI-06 non-generative, source-grounded fact candidates.

This is deliberately a candidate stage, not a writer.  It adapts the existing
conservative English rule miner to the current semantic vocabulary, pins each
candidate to an exact source span, and leaves every unsupported or ambiguous
outcome unresolved for the canonical compiler/integration door.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.core.rule_miner import mine_rules
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.store.records import MemoryRecord

__all__ = [
    "FactStageResult",
    "GroundedFactCandidate",
    "UnresolvedFactCandidate",
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


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GroundedFactCandidate(_Candidate):
    scope: OwnerScope
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    modality: Literal["asserted", "planned"]
    polarity: Literal["positive"] = "positive"
    assertion_owner: str = Field(min_length=1)
    evidence: Evidence
    source_time: str | None = None
    producer_revision: Literal["personal-rules:1"] = "personal-rules:1"


class UnresolvedFactCandidate(_Candidate):
    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    reason: Literal[
        "assistant_proposal",
        "missing_assertion_owner",
        "correction_requires_resolution",
        "unsupported_predicate",
        "unsupported_relation",
        "ambiguous_support_span",
    ]
    attribute: str | None = None
    value: str | None = None


class FactStageResult(_Candidate):
    facts: tuple[GroundedFactCandidate, ...] = ()
    unresolved: tuple[UnresolvedFactCandidate, ...] = ()
    generative_calls: Literal[0] = 0


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
