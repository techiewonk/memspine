"""Lift derived hits to source turns, re-admit, dedupe, cap, THEN rerank once (ER-21 B, ER-22, POL-2).

Pure and deterministic: no I/O, no model calls. The rerank function, the viewer's visibility predicate and
the text lookup are passed in. Fixes two defects of the earlier order: lifted turns were never scored
(lift ran after rerank), and the reranker tail was given 0.0, which reads as a measured zero. An item the
reranker did not score stays ``scored=False, score=None`` and sorts after every scored item.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from memspine.schemas.durable.lineage import LineageEdge
from memspine.schemas.public.read import ScoredItem

__all__ = [
    "STAGES",
    "Candidate",
    "Dropped",
    "LiftPolicy",
    "PipelineResult",
    "PipelineTrace",
    "Provenance",
    "StageCount",
    "cap",
    "dedupe",
    "lift",
    "pipeline",
    "rerank_pool",
]

STAGES: tuple[str, ...] = ("lift", "readmit", "dedupe", "cap", "rerank")

Visibility = Callable[[str], bool]
RerankFn = Callable[[str, list[str]], Sequence[float | None]]
Lineage = Mapping[str, Sequence[LineageEdge]]  # child record id -> its edges to parents


@dataclass(frozen=True)
class Provenance:
    """Why a turn is in the pool: which derived hit lifted it, through which role, after how many hops."""

    via: str
    role: str
    hops: int = 1


@dataclass(frozen=True)
class Candidate:
    """One pool entry. ``rank`` is the prior (fusion) rank, lower is better; ``prior_score`` may be None."""

    record_id: str
    kind: str = "turn"
    rank: float = 0.0
    prior_score: float | None = None
    text: str | None = None
    protected: bool = False  # kept by cap() regardless of rank (family coverage, from fusion)
    family: str | None = None
    lifted: bool = False
    provenance: tuple[Provenance, ...] = ()
    scored: bool = False
    score: float | None = None

    def to_scored_item(self) -> ScoredItem:
        if self.scored and self.score is not None:
            return ScoredItem(record_id=self.record_id, scored=True, score=self.score)
        return ScoredItem(record_id=self.record_id, scored=False, score=None)


@dataclass(frozen=True)
class LiftPolicy:
    derived_kinds: frozenset[str] = frozenset({"fact", "summary", "episode", "edge"})
    lift_roles: tuple[str, ...] = ("binding", "support")  # walked in this order; binding first
    max_depth: int = 4
    # "drop" (strict, POL-2): every source turn of a derived hit must be visible, else the hit loses its
    # slot. "keep_visible" admits the hit through its visible turns only.
    partial_lineage: Literal["drop", "keep_visible"] = "drop"
    keep_derived: bool = False  # the derived hit is replaced by its turns unless set
    # A parent with no lift edges of its own is a root turn. If given, a root for which is_turn is False
    # is a dead end (no live support) rather than a source.
    is_turn: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class Dropped:
    record_id: str
    reason: str
    stage: str


@dataclass(frozen=True)
class StageCount:
    stage: str
    n_in: int
    n_out: int


@dataclass
class PipelineTrace:
    stages: list[StageCount] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(s.stage for s in self.stages)

    def count(self, stage: str) -> StageCount:
        return next(s for s in self.stages if s.stage == stage)


@dataclass(frozen=True)
class PipelineResult:
    items: tuple[Candidate, ...]
    trace: PipelineTrace

    def scored_items(self) -> list[ScoredItem]:
        return [c.to_scored_item() for c in self.items]


def _order_key(c: Candidate) -> tuple[float, int, str]:
    return (c.rank, 1 if c.lifted else 0, c.record_id)


def _walk(
    start: str, lineage: Lineage, policy: LiftPolicy
) -> tuple[list[tuple[str, Provenance]], list[str]]:
    """Source turns reachable from ``start`` through lift roles. Returns (turns, dead_end_ids)."""
    turns: list[tuple[str, Provenance]] = []
    dead: list[str] = []
    seen: set[str] = {start}
    frontier: list[tuple[str, str, int]] = [(start, "", 0)]
    while frontier:
        nxt: list[tuple[str, str, int]] = []
        for node, top_role, depth in frontier:
            edges = [e for e in lineage.get(node, ()) if e.role in policy.lift_roles]
            edges.sort(key=lambda e: policy.lift_roles.index(e.role))
            for e in edges:
                if e.parent in seen:
                    continue
                seen.add(e.parent)
                role = top_role or e.role
                hop = depth + 1
                if any(x.role in policy.lift_roles for x in lineage.get(e.parent, ())):
                    if hop < policy.max_depth:
                        nxt.append((e.parent, role, hop))
                    else:
                        dead.append(e.parent)
                elif policy.is_turn is not None and not policy.is_turn(e.parent):
                    dead.append(e.parent)
                else:
                    turns.append((e.parent, Provenance(via=start, role=role, hops=hop)))
        frontier = nxt
    return turns, dead


def lift(
    candidates: Sequence[Candidate],
    lineage: Lineage,
    visibility: Visibility,
    *,
    max_turns_per_hit: int,
    policy: LiftPolicy | None = None,
    dropped: list[Dropped] | None = None,
) -> list[Candidate]:
    """Replace each derived hit by its visible source turns (POL-2). Non-derived candidates pass through
    (visibility is re-checked at re-admission). A derived hit with no live visible support loses its slot."""
    if max_turns_per_hit < 1:
        raise ValueError("max_turns_per_hit must be >= 1")
    pol = policy or LiftPolicy()
    drops = dropped if dropped is not None else []
    out: list[Candidate] = []
    for c in candidates:
        if c.kind not in pol.derived_kinds:
            out.append(c)
            continue
        if not visibility(c.record_id):
            drops.append(Dropped(c.record_id, "derived_hit_not_visible", "lift"))
            continue
        turns, dead = _walk(c.record_id, lineage, pol)
        if not turns:
            reason = "no_live_lineage" if not dead else "lineage_unresolvable"
            drops.append(Dropped(c.record_id, reason, "lift"))
            continue
        hidden = {tid for tid, _ in turns if not visibility(tid)}
        visible = [(tid, pv) for tid, pv in turns if tid not in hidden]
        for tid in sorted(hidden):
            drops.append(Dropped(tid, "lifted_turn_not_visible", "lift"))
        if not visible:
            drops.append(Dropped(c.record_id, "no_visible_support", "lift"))
            continue
        if hidden and pol.partial_lineage == "drop":
            drops.append(Dropped(c.record_id, "partial_lineage_hidden", "lift"))
            continue
        if pol.keep_derived:
            out.append(c)
        for tid, pv in visible[:max_turns_per_hit]:
            out.append(
                Candidate(
                    record_id=tid,
                    kind="turn",
                    rank=c.rank,
                    prior_score=c.prior_score,
                    protected=c.protected,
                    family=c.family,
                    lifted=True,
                    provenance=(pv,),
                )
            )
    return out


def dedupe(pool: Sequence[Candidate]) -> list[Candidate]:
    """One entry per record id: best rank, provenance merged (first-seen order), protected if any copy is.
    A direct hit stays direct (lifted=False) even if also lifted. Result is in prior order."""
    merged: dict[str, Candidate] = {}
    for c in pool:
        cur = merged.get(c.record_id)
        if cur is None:
            merged[c.record_id] = c
            continue
        prov = cur.provenance + tuple(p for p in c.provenance if p not in cur.provenance)
        scores = [s for s in (cur.prior_score, c.prior_score) if s is not None]
        best = min((cur, c), key=_order_key)
        merged[c.record_id] = replace(
            best,
            provenance=prov,
            protected=cur.protected or c.protected,
            prior_score=max(scores) if scores else None,
            lifted=cur.lifted and c.lifted,
            text=cur.text if cur.text is not None else c.text,
            family=best.family or cur.family or c.family,
        )
    return sorted(merged.values(), key=_order_key)


def cap(pool: Sequence[Candidate], n: int) -> list[Candidate]:
    """Keep at most ``n``, in prior order. Protected items are kept first; if they alone exceed ``n`` the
    best-ranked ``n`` protected survive. Remaining room goes to the best-ranked unprotected items."""
    if n < 0:
        raise ValueError("n must be >= 0")
    ordered = sorted(pool, key=_order_key)
    keep = {c.record_id for c in [c for c in ordered if c.protected][:n]}
    for c in ordered:
        if len(keep) >= n:
            break
        if not c.protected:
            keep.add(c.record_id)
    return [c for c in ordered if c.record_id in keep]


def rerank_pool(
    query: str,
    pool: Sequence[Candidate],
    rerank_fn: RerankFn,
    *,
    max_rerank: int,
) -> list[Candidate]:
    """Rerank the first ``max_rerank`` (prior order) candidates that have text, once. Scored items come
    first by descending score; everything else keeps scored=False, score=None and follows by prior rank."""
    if max_rerank < 0:
        raise ValueError("max_rerank must be >= 0")
    ordered = sorted(pool, key=_order_key)
    window = [c for c in ordered[:max_rerank] if c.text is not None]
    scores: dict[str, float] = {}
    if window:
        got = list(rerank_fn(query, [c.text or "" for c in window]))
        if len(got) != len(window):
            raise ValueError(f"rerank_fn returned {len(got)} scores for {len(window)} texts")
        for c, s in zip(window, got, strict=True):
            if s is not None and not isinstance(s, bool) and math.isfinite(s):
                scores[c.record_id] = float(s)
    scored = [
        replace(c, scored=True, score=scores[c.record_id]) for c in ordered if c.record_id in scores
    ]
    rest = [replace(c, scored=False, score=None) for c in ordered if c.record_id not in scores]
    scored.sort(key=lambda c: (-(c.score or 0.0), *_order_key(c)))
    return scored + rest


def pipeline(
    query: str,
    candidates: Sequence[Candidate],
    lineage: Lineage,
    visibility: Visibility,
    rerank_fn: RerankFn,
    *,
    max_turns_per_hit: int,
    pool_cap: int,
    max_rerank: int,
    text_of: Callable[[str], str | None] | None = None,
    policy: LiftPolicy | None = None,
) -> PipelineResult:
    """lift -> re-admit -> dedupe -> cap -> rerank, in exactly that order, with a trace."""
    trace = PipelineTrace()

    def record(stage: str, n_in: int, n_out: int) -> None:
        trace.stages.append(StageCount(stage, n_in, n_out))

    lifted = lift(
        candidates,
        lineage,
        visibility,
        max_turns_per_hit=max_turns_per_hit,
        policy=policy,
        dropped=trace.dropped,
    )
    record("lift", len(candidates), len(lifted))

    admitted: list[Candidate] = []
    for c in lifted:  # ER-22: every expansion is admitted again, direct hits included
        if not visibility(c.record_id):
            trace.dropped.append(Dropped(c.record_id, "not_visible_at_readmit", "readmit"))
            continue
        if c.text is None and text_of is not None:
            c = replace(c, text=text_of(c.record_id))
        admitted.append(c)
    record("readmit", len(lifted), len(admitted))

    deduped = dedupe(admitted)
    record("dedupe", len(admitted), len(deduped))

    capped = cap(deduped, pool_cap)
    kept = {c.record_id for c in capped}
    for c in deduped:
        if c.record_id not in kept:
            trace.dropped.append(Dropped(c.record_id, "over_cap", "cap"))
    record("cap", len(deduped), len(capped))

    ranked = rerank_pool(query, capped, rerank_fn, max_rerank=max_rerank)
    record("rerank", len(capped), len(ranked))
    return PipelineResult(items=tuple(ranked), trace=trace)
