"""I67 (``read.agentic``): the pure parts of the agentic multi-step read.

The engine owns the loop (LLM calls, searches, forensics); this module holds what needs no
service: the compact evidence view the action step sees, the RRF fusion of the extra
searches, and the budget merge that keeps the step-0 hits.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from memspine.config import constants
from memspine.core.policies.assembly import estimate_tokens
from memspine.core.records import MemoryRecord

__all__ = ["clean_query", "evidence_view", "fuse_new", "merge_budget"]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean_query(text: str) -> str:
    """An LLM-written search string: control characters out, whitespace collapsed, cut."""
    return " ".join(_CONTROL.sub(" ", text).split())[: constants.AGENTIC_QUERY_CHARS]


def evidence_view(
    records: Sequence[MemoryRecord],
    *,
    max_tokens: int = constants.AGENTIC_VIEW_TOKENS,
    line_chars: int = constants.AGENTIC_LINE_CHARS,
) -> str:
    """One line per record, in order, each cut to ``line_chars`` and the whole view to
    ``max_tokens`` (estimated); an empty view reads ``(none)``."""
    lines: list[str] = []
    used = 0
    for record in records:
        line = "- " + " ".join(record.content.split())[:line_chars]
        cost = estimate_tokens(line)
        if lines and used + cost > max_tokens:
            break
        lines.append(line)
        used += cost
    return "\n".join(lines) or "(none)"


def fuse_new(
    rankings: Sequence[Sequence[MemoryRecord]], seen: set[str], rrf_k: int
) -> list[MemoryRecord]:
    """RRF over the extra searches' rankings, de-duplicated by record id, leaving out the
    ids in ``seen`` (the evidence so far). Best fused score first; ties keep first seen."""
    score: dict[str, float] = {}
    record: dict[str, MemoryRecord] = {}
    order: dict[str, int] = {}
    for ranking in rankings:
        for rank, rec in enumerate(ranking, start=1):
            if rec.record_id in seen:
                continue
            score[rec.record_id] = score.get(rec.record_id, 0.0) + 1.0 / (rrf_k + rank)
            record.setdefault(rec.record_id, rec)
            order.setdefault(rec.record_id, len(order))
    ranked = sorted(score, key=lambda rid: (-score[rid], order[rid]))
    return [record[rid] for rid in ranked]


def merge_budget(
    step0: Sequence[MemoryRecord],
    new: Sequence[MemoryRecord],
    budget_tokens: int,
    *,
    share: float,
    max_new: int,
) -> tuple[list[MemoryRecord], list[MemoryRecord], list[MemoryRecord]]:
    """Fit ``new`` records (best first, at most ``max_new``) beside the step-0 records.

    The step-0 records, in order, are protected while they fit ``share`` of the budget
    (the first one always). The rest may be displaced, last first, but only as far as the
    new records need room; a new record that cannot fit even then is skipped.
    Returns ``(kept step-0, added new, displaced step-0)``."""

    def cost(r: MemoryRecord) -> int:
        return estimate_tokens(r.content)

    kept = list(step0)
    used = sum(cost(r) for r in kept)
    protected = 0
    spent = 0
    for r in kept:
        if protected and spent + cost(r) > share * budget_tokens:
            break
        spent += cost(r)
        protected += 1
    added: list[MemoryRecord] = []
    displaced: list[MemoryRecord] = []
    for rec in new[:max_new]:
        need = cost(rec)
        if spent + sum(cost(a) for a in added) + need > budget_tokens:
            continue  # not even with every displaceable record gone
        while used + need > budget_tokens and len(kept) > protected:
            gone = kept.pop()
            displaced.append(gone)
            used -= cost(gone)
        if used + need > budget_tokens:
            continue
        added.append(rec)
        used += need
    return kept, added, displaced
