"""G26 (plan v3.2, TWIST): vet a draft reply against current keyed facts. Rules only.

A draft sentence that names an entity and one of its fact attributes but not the
fact's current value, or that negates the current value ("Jon no longer teaches"),
is flagged with the fact (and its history) that it may contradict. Lexical, no model:
the caller decides what to do with a flag (revise, ask, or ignore).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord

__all__ = ["VetFlag", "draft_sentences", "vet_sentences"]

_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NEGATION = re.compile(r"\b(?:not|no longer|never|isn't|aren't|wasn't|doesn't|didn't|no)\b", re.I)


@dataclass(frozen=True)
class VetFlag:
    """One draft sentence that may contradict a current fact."""

    sentence: str
    fact_id: str
    fact: str
    reason: str  # "different_value" | "negated_value"
    history: list[str] = field(default_factory=list)


def draft_sentences(draft: str) -> list[str]:
    return [s.strip() for s in _SPLIT.split(draft) if s.strip()]


def _value_words(fact: MemoryRecord) -> frozenset[str]:
    text = fact.content.split(":", 1)[1] if ":" in fact.content else fact.content
    return (
        content_words(text)
        - content_words(fact.entity or "")
        - content_words((fact.attribute or "").replace("_", " "))
    )


def vet_sentences(
    sentences: Sequence[str],
    current: Iterable[MemoryRecord],
    history: dict[str, list[str]] | None = None,
) -> list[VetFlag]:
    """Flags for ``sentences`` against the ``current`` keyed facts."""
    facts = [f for f in current if f.entity and f.attribute]
    flags: list[VetFlag] = []
    for sentence in sentences:
        words = content_words(sentence)
        for fact in facts:
            if not content_words(fact.entity or "") <= words:
                continue
            attribute_words = content_words((fact.attribute or "").replace("_", " "))
            value = _value_words(fact)
            if not value:
                continue
            mentions_value = bool(value & words)
            if mentions_value and _NEGATION.search(sentence):
                reason = "negated_value"
            elif attribute_words and attribute_words <= words and not mentions_value:
                reason = "different_value"
            else:
                continue
            flags.append(
                VetFlag(
                    sentence=sentence,
                    fact_id=fact.record_id,
                    fact=fact.content,
                    reason=reason,
                    history=list((history or {}).get(fact.record_id, [])),
                )
            )
    return flags
