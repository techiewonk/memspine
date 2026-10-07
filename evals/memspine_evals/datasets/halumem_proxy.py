"""HaluMem retrieval proxies: turn-level gold evidence from memory points (a **proxy**).

HaluMem gives a question's evidence as *memory points* (``memory_content`` lines written
in the third person), not as turn ids, so :class:`~.halumem.HaluMemDataset` leaves
``gold_turn_ids`` empty. This module wraps that adapter (unchanged) and maps every
evidence line to the dialogue turn that most plausibly stated it, so the harness's
coverage / R@k machinery can run without the official LLM judge.

**The mapping is deterministic lexical overlap, not ground truth:**

- tokens = lower-cased alphanumeric words of length >= 2, minus a small stop-word list
  and first/second-person pronouns (dialogue says "I", memory points say the name);
- a turn's score for an evidence line = |evidence tokens ∩ turn tokens| / |evidence
  tokens|; candidates are the turns at or before the query's ``after_turn`` with a role
  in ``roles`` (default user + assistant: the assistant often restates the user's fact;
  on the first 2 HaluMem-Medium users this maps 407/496 evidence lines at 0.5, against
  186/496 with user turns only; mapping precision is not hand-checked);
- the best-scoring turn is gold if its score >= ``min_overlap`` (ties: the latest turn,
  since updates supersede); otherwise that evidence line is unmapped.

Per-query audit fields in ``Query.meta``: ``proxy_gold`` = True, ``evidence_mapped`` /
``evidence_total``, and ``proxy_scores`` (overlap per mapped line). Questions with no
evidence (Memory Boundary: the answer is "not mentioned") keep empty gold and are marked
``abstention`` so they can be read as an abstention set. Report the share of mapped
evidence next to any R@k computed on this proxy.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..contracts import DatasetInfo, EvalItem, Query, Turn
from .halumem import HaluMemDataset

__all__ = ["HaluMemProxyDataset", "map_evidence", "proxy_tokens"]

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    re.findall(
        r"\S+",
        "a an and are as at be been but by for from has have he her him his i in is it its "
        "me my of on or our she so that the their them they this to was we were with you "
        "your user users",
    )
)


def proxy_tokens(text: str) -> frozenset[str]:
    """Content tokens used for the overlap mapping (see module docstring)."""
    return frozenset(w for w in _WORD.findall(text.lower()) if len(w) >= 2 and w not in _STOP)


def map_evidence(
    evidence: str, turns: tuple[Turn, ...], min_overlap: float = 0.5
) -> tuple[str | None, float]:
    """Best turn id for one evidence line and its overlap score (``None`` below threshold)."""
    want = proxy_tokens(evidence)
    if not want:
        return None, 0.0
    best_id, best = None, 0.0
    for turn in turns:  # later turns win ties: ">=" keeps the latest
        score = len(want & proxy_tokens(turn.text)) / len(want)
        if score >= best and score > 0:
            best_id, best = turn.turn_id, score
    return (best_id, best) if best >= min_overlap else (None, best)


class HaluMemProxyDataset:
    """HaluMem Memory QA with proxy ``gold_turn_ids`` (lexical memory-point -> turn map)."""

    def __init__(
        self,
        path: str | Path,
        revision_id: str,
        max_users: int | None = None,
        min_overlap: float = 0.5,
        roles: tuple[str, ...] = ("user", "assistant"),
    ) -> None:
        self.base = HaluMemDataset(path, revision_id=revision_id, max_users=max_users)
        self.min_overlap, self.roles = min_overlap, roles
        self._items = [self._map(item) for item in self.base.items()]

    def info(self) -> DatasetInfo:
        base = self.base.info()
        mapped = sum(int(q.meta["evidence_mapped"]) for i in self._items for q in i.queries)
        total = sum(int(q.meta["evidence_total"]) for i in self._items for q in i.queries)
        return replace(
            base,
            dataset_id=f"{base.dataset_id}_proxy",
            subset=f"{base.subset},proxy_overlap>={self.min_overlap},roles={'/'.join(self.roles)}",
            notes=(
                "retrieval proxy: gold_turn_ids from lexical memory-point -> turn overlap "
                f"(mapped {mapped}/{total} evidence lines); not the official LLM-judged metric"
            ),
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _map(self, item: EvalItem) -> EvalItem:
        index = {t.turn_id: n for n, t in enumerate(item.history)}
        queries: list[Query] = []
        for query in item.queries:
            stop = (
                index.get(query.after_turn, len(item.history) - 1)
                if query.after_turn
                else (len(item.history) - 1)
            )
            pool = tuple(t for t in item.history[: stop + 1] if t.speaker in self.roles)
            lines = [ln for ln in str(query.meta.get("key_memory_points", "")).split("\n") if ln]
            gold: list[str] = []
            scores: list[float] = []
            for line in lines:
                tid, score = map_evidence(line, pool, self.min_overlap)
                if tid is not None:
                    scores.append(round(score, 3))
                    if tid not in gold:
                        gold.append(tid)
            meta: dict[str, Any] = {
                **query.meta,
                "proxy_gold": True,
                "evidence_total": len(lines),
                "evidence_mapped": len(scores),
                "proxy_scores": scores,
                "abstention": not lines,
            }
            queries.append(replace(query, gold_turn_ids=tuple(gold), meta=meta))
        return replace(item, queries=tuple(queries))
