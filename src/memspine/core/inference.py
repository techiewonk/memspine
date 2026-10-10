"""I48: inferred provenance and the explicit / implicit split (MemOS preference rules).

The record's provenance class (``MemoryRecord.source``: role and channel) says who wrote it;
it never said whether the text is the user's own statement or something the engine concluded.
``src:inferred`` marks the second kind: a fact mined, reflected or consolidated from turns
that do not state it, or proposed by the assistant with no user turn behind it.

Rules (all opt-in, ``write.inferred`` / ``read.inferred_gate`` /
``policies.conflict.inferred_defers``):

* an inferred record's trust is capped (``write.inferred_trust_cap``, below user-stated 0.7
  and assistant 0.5);
* it never overrides a stated record on the same (entity, attribute) key (conflict ladder);
* it is used by a read only once ``read.inferred_min_support`` distinct user turns support it;
* until then it is on the review list (``Engine.review_inferred``).

Explicit versus inferred is decided without a model: a mined fact whose content words are
mostly contained in one user turn is that user's statement. Support is counted as distinct
user parent turns that share enough content words with the fact. Tags are the carrier
(``src:inferred``, ``support:<turn id>``), like the other governance labels.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from memspine.core.query_shape import content_words

__all__ = [
    "INFERRED_CHANNELS",
    "INFERRED_TAG",
    "SUPPORT_PREFIX",
    "classify",
    "containment",
    "is_inferred",
    "support_count",
    "support_tags",
]

INFERRED_TAG = "src:inferred"
SUPPORT_PREFIX = "support:"

#: Channels whose records are the engine's own conclusions.
INFERRED_CHANNELS = frozenset({"mining", "reflection", "consolidation"})


def is_inferred(tags: Iterable[str]) -> bool:
    return INFERRED_TAG in tags


def support_tags(turn_ids: Iterable[str]) -> list[str]:
    return [f"{SUPPORT_PREFIX}{t}" for t in dict.fromkeys(turn_ids)]


def support_count(tags: Iterable[str]) -> int:
    """Distinct supporting user turns a record's tags carry."""
    return len({t for t in tags if t.startswith(SUPPORT_PREFIX)})


def containment(fact: str, turn: str) -> float:
    """Share of ``fact``'s content words that ``turn`` also has (0 for an empty fact)."""
    mine = content_words(fact)
    return len(mine & content_words(turn)) / len(mine) if mine else 0.0


def classify(
    fact: str,
    parents: Sequence[tuple[str, str, str]],
    *,
    explicit_overlap: float = 0.6,
    support_overlap: float = 0.3,
) -> tuple[bool, list[str]]:
    """``(inferred, supporting user turn ids)`` for a derived ``fact``.

    ``parents`` are ``(record_id, role, text)`` of the turns it was derived from. Not inferred
    when one user parent contains at least ``explicit_overlap`` of the fact's words. Otherwise
    inferred, supported by every distinct user parent with at least ``support_overlap``."""
    users = [(rid, text) for rid, role, text in parents if role == "user"]
    if any(containment(fact, text) >= explicit_overlap for _, text in users):
        return False, []
    support = [rid for rid, text in users if containment(fact, text) >= support_overlap]
    return True, list(dict.fromkeys(support))
