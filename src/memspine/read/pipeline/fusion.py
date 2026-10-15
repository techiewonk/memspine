"""Read path: fusion, pooling and ordering helpers moved out of the engine (F-10a)."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Sequence,
)
from typing import Any

from memspine.config import constants
from memspine.core import trace_sink
from memspine.exceptions import (
    MemspineError,
)
from memspine.models.encoders.query.temporal_query import (
    LegHit,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read import fusion as family_lib
from memspine.read.pipeline.state import (
    _FORENSICS,
)
from memspine.read.registry import leg_family
from memspine.store.records import (
    MemoryRecord,
)

_log = get_logger(__name__)


def _ids(pairs: Iterable[tuple[Any, Any]]) -> list[str] | None:
    """Record ids of ``(record, score)`` pairs, only while a forensics sink is installed."""
    if _FORENSICS.get() is None:
        return None
    return [pair[0].record_id for pair in pairs]


def _cut_missing(
    before: list[str] | None, after: Iterable[tuple[Any, Any]], reason: str, **detail: Any
) -> None:
    """Record every id in ``before`` that ``after`` no longer holds as cut for ``reason``."""
    if before is None:
        return
    kept = {pair[0].record_id for pair in after}
    for rid in before:
        if rid not in kept:
            trace_sink.cut(reason, rid, **detail)


def _log_leg_floor(leg: str, hits: Any, floor: float) -> None:
    """V02: one ``leg_floor`` cut per hit a per-leg score floor (``read.leg_min_scores``)
    drops, with the leg, the hit's score, its rank in that leg and the floor. Called only
    while a forensics sink is active."""
    for pos, hit in enumerate(hits, start=1):
        if hit.score < floor:
            trace_sink.cut(
                "leg_floor",
                hit.record_id,
                leg=leg,
                score=round(float(hit.score), 6),
                leg_rank=pos,
                floor=floor,
            )


def _leg_rank_map(
    legs: Sequence[tuple[str, Sequence[Any]]],
) -> dict[str, tuple[str, int]]:
    """I78/I75a: for each record id, the (leg name, 1-based rank) of its best rank in any leg."""
    best: dict[str, tuple[str, int]] = {}
    for name, hits in legs:
        for rank, hit in enumerate(hits, start=1):
            rid = hit.record_id
            if rid not in best or rank < best[rid][1]:
                best[rid] = (name, rank)
    return best


def _protected_ids(legs: Sequence[tuple[str, Sequence[Any]]], per_leg: int) -> dict[str, str]:
    """I75a: record id -> leg name for the top ``per_leg`` hits of every leg."""
    out: dict[str, str] = {}
    if per_leg <= 0:
        return out
    for name, hits in legs:
        for hit in list(hits)[:per_leg]:
            out.setdefault(hit.record_id, name)
    return out


def _leg_family(name: str) -> str:
    """R02: the source family of a leg name (lexical | semantic | temporal | other)."""
    return leg_family(name)  # MOD-02: the typed ``read_legs`` registry


def _family_slots(
    legs: Sequence[tuple[str, Sequence[Any]]], per_family: int
) -> dict[str, list[str]]:
    """R02: family -> its top ``per_family`` record ids, each id counted once per family.

    The member legs of a family are merged by best rank (ties: the leg listed first, so the
    base leg of the family leads); an id that several correlated legs vote for takes one slot.
    """
    if per_family <= 0:
        return {}
    best: dict[str, dict[str, tuple[int, int]]] = {}
    for order, (name, hits) in enumerate(legs):
        fam = best.setdefault(_leg_family(name), {})
        for rank, hit in enumerate(hits):
            key = (rank, order)
            if hit.record_id not in fam or key < fam[hit.record_id]:
                fam[hit.record_id] = key
    return {
        fam: [rid for rid, _ in sorted(ids.items(), key=lambda kv: kv[1])[:per_family]]
        for fam, ids in best.items()
        if ids
    }


def _cosine(u: list[float], v: list[float]) -> float:
    """Dot product over the unit-normalized vectors every embedder emits."""
    if len(u) != len(v):
        # A dimension mismatch means the embedder changed under us — a silent
        # 0.0 would read as "plan cache has no coverage" forever (D-10).
        raise MemspineError(
            f"embedding dimension mismatch ({len(u)} vs {len(v)}) — was the "
            "embedder model changed without a rebuild?"
        )
    return sum(a * b for a, b in zip(u, v, strict=True))


def _jaccard(a: Collection[str], b: Collection[str]) -> float:
    """Word-set Jaccard (N25)."""
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _unit(vector: Sequence[float]) -> list[float]:
    """N60: ``vector`` scaled to unit length (a zero vector stays zero)."""
    norm = sum(x * x for x in vector) ** 0.5
    return [x / norm for x in vector] if norm > 0 else list(vector)


def _balanced_pool(
    candidates: list[tuple[MemoryRecord, float]], legs: Sequence[Sequence[Any]], keep: int
) -> list[tuple[MemoryRecord, float]]:
    """GR-15: ``candidates`` reordered so the first ``keep`` take each leg's best
    remaining hit in turn (legs in the order given); the rest follow in their order.
    Only candidates already admitted are moved; nothing is added."""
    by_id = {record.record_id: (record, score) for record, score in candidates}
    chosen: list[str] = []
    cursors = [iter(leg) for leg in legs if leg]
    while cursors and len(chosen) < keep:
        alive = []
        for cursor in cursors:
            for hit in cursor:
                rid = getattr(hit, "record_id", None)
                if rid in by_id and rid not in chosen:
                    chosen.append(rid)
                    alive.append(cursor)
                    break
            if len(chosen) >= keep:
                break
        cursors = alive
    picked = set(chosen)
    rest = [pair for pair in candidates if pair[0].record_id not in picked]
    return [by_id[rid] for rid in chosen] + rest


def _minmax_quiet(scores: list[float]) -> list[float]:
    """N41: min-max to [0, 1] without the degenerate-reranker warning (retrieval
    scores may legitimately tie)."""
    lo, hi = min(scores), max(scores)
    if hi - lo <= 1e-12:
        return [0.5] * len(scores)
    return [(score - lo) / (hi - lo) for score in scores]


def _minmax_normalize(scores: list[float]) -> list[float]:
    """Cross-encoder logits → [0, 1] relevance (rank-preserving) so reranked
    scores compose with the M1 composite exactly like cosine similarities."""
    lo, hi = min(scores), max(scores)
    if hi - lo <= 1e-12:
        # SF-2/ADR-018: >1 candidate collapsing to a single score is the
        # signature of a broken/misconfigured cross-encoder. Stay neutral
        # (behavior unchanged) but surface it once so it is not silent.
        if len(scores) > 1:
            _log.warning("rerank.degenerate_scores", count=len(scores), value=lo)
        return [0.5] * len(scores)
    return [(score - lo) / (hi - lo) for score in scores]


async def _mmr_order(
    eng: Any, query: str, scored: list[tuple[MemoryRecord, float]], lam: float
) -> list[tuple[MemoryRecord, float]]:
    """G-10: maximal marginal relevance on embeddings. Greedily takes the hit with
    the best ``lam * relevance - (1 - lam) * max cosine to the hits already taken``;
    scores are kept, only the order changes. Vectors come from the embedder (its
    cache serves the stored documents); a failure keeps the order."""
    if eng._embedder is None:
        return scored
    try:
        vectors = [_unit(v) for v in await eng._embedder.embed([r.content for r, _ in scored])]
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.mmr_failed", error=str(exc))
        return scored
    rel = _minmax_quiet([s for _, s in scored])
    left = list(range(len(scored)))
    order: list[int] = []
    while left:

        def gain(i: int) -> float:
            if not order:
                return rel[i]
            sim = max(
                sum(a * b for a, b in zip(vectors[i], vectors[j], strict=False)) for j in order
            )
            return lam * rel[i] - (1.0 - lam) * sim

        best = max(left, key=lambda i: (gain(i), -i))
        order.append(best)
        left.remove(best)
    return [scored[i] for i in order]


def _penalise_reinjected(
    eng: Any, candidates: list[tuple[MemoryRecord, float]], ns: str, session_id: str
) -> list[tuple[MemoryRecord, float]]:
    """I64: relevance x ``1 - penalty * uses / window`` for records injected in the
    last ``read.reinjection_window`` replies of this (namespace, session)."""
    pen = eng._config().read.reinjection_penalty
    out = [
        (rec, rel * eng._injections.factor(ns, session_id, rec.record_id, pen))
        for rec, rel in candidates
    ]
    out.sort(key=lambda t: t[1], reverse=True)
    return out


async def _tie_keys(eng: Any, record_ids: list[str]) -> dict[str, Any]:
    """#87: the order of tied leg hits, by record id: write order (record time,
    strictly increasing, see :func:`record_time`), then content. Write order is
    what an exact flat scan of an append-only table yields, so a leg whose
    store order was already stable keeps it; one whose order was not (segment
    merges, multi-threaded scans) now gets it too."""
    storage = eng._require_started()
    keys: dict[str, Any] = {}
    for rid in dict.fromkeys(record_ids):
        record = await storage.get_record(rid)
        if record is not None:
            keys[rid] = (record.recorded_at, record.content_fingerprint, record.record_id)
    return keys


async def _session_capped(
    eng: Any,
    query: str,
    ns: str,
    want: int,
    cap: int,
    *,
    session_id: str | None,
    hide: Callable[[MemoryRecord], bool] | None,
    probes: Sequence[str],
    legs: Sequence[list[LegHit]],
) -> list[tuple[MemoryRecord, float]]:
    """W10 (plan v3.2, ``read.session_cap``): ``want`` candidates, best first, with
    at most ``cap`` raw turns from any one session, drawn from a pool
    :data:`constants.SESSION_CAP_POOL` times wider. Evidence spread over sessions
    ("what activities has X done?") then reaches the read instead of the few
    best-matching turns of one or two sessions. Sessions are the episodic
    timeline's (gap-split); without episodic memory, the calendar day. Derived
    records are never capped."""
    pool = await eng._search(
        query,
        ns,
        want * constants.SESSION_CAP_POOL,
        session_id=session_id,
        keep_k=want,
        hide=hide,
        probes=probes,
        fused_legs=legs,
    )
    session_of: dict[str, str] = {}
    if eng._episodic is not None:
        for session in await eng._episodic.sessions(ns, constants.SESSION_GAP_MINUTES):
            for rid in session.record_ids:
                session_of[rid] = session.session_key
    kept: list[tuple[MemoryRecord, float]] = []
    per: dict[str, int] = {}
    for record, score in pool:
        if record.memory_type == "episodic":
            key = session_of.get(record.record_id) or f"{record.valid_from:%Y-%m-%d}"
            if per.get(key, 0) >= cap:
                continue
            per[key] = per.get(key, 0) + 1
        kept.append((record, score))
        if len(kept) >= want:
            break
    return kept


#: R3-06: the leg registry names a leg's *source* family (semantic | lexical | temporal | other);
#: the
#: step schema's ``Family`` literal names the *retrieval* family (dense | lexical | structured |
#: derived).
#: Voting always uses the registry family (R02); this map only renders a leg family in step-
#: schema terms.
SCHEMA_FAMILY: dict[str, str] = {
    "semantic": "dense",
    "lexical": "lexical",
    "temporal": "structured",
    "other": "structured",
}


def schema_family(leg_family_name: str, *, derived: bool = False) -> str:
    """The step-schema ``Family`` of a registry leg family (``derived`` marks a graph/derived
    leg)."""
    return "derived" if derived else SCHEMA_FAMILY.get(leg_family_name, "structured")


def _lib_legs(
    vector_hits: Sequence[Any],
    lexical_hits: Sequence[Any],
    use_hybrid: bool,
    extra_legs: Sequence[Sequence[Any]],
) -> list[family_lib.Leg]:
    """Today's leg lists as LIB-10 legs. An unnamed extra leg is ``extra<i>`` (family ``other``)."""
    named: list[tuple[str, Sequence[Any]]] = [("vector", vector_hits)]
    if use_hybrid:
        named.append(("lexical", lexical_hits))
    for i, leg in enumerate(extra_legs):
        named.append((getattr(leg, "name", None) or f"extra{i}", leg))
    out: list[family_lib.Leg] = []
    used: set[str] = set()
    for name, hits in named:
        if name in used:  # two legs of one name would merge; keep them apart
            name = f"{name}#{len(used)}"
        used.add(name)
        out.append(
            family_lib.Leg(
                name,
                _leg_family(name.split("#")[0]),
                (family_lib.Hit(h.record_id, float(h.score)) for h in hits),
            )
        )
    return out


def fuse_families(
    vector_hits: Sequence[Any],
    lexical_hits: Sequence[Any],
    use_hybrid: bool,
    extra_legs: Sequence[Sequence[Any]],
    *,
    pool_size: int,
    protect_n: int,
    rrf_k: int,
    floors: dict[str, float] | None,
) -> tuple[list[tuple[str, float]], dict[str, str], dict[str, list[str]]]:
    """R3-06 (``read.family_fusion``): family fusion with a protected pool and floors (LIB-10).

    Returns ``(ranked, protected, family_slots)``: ``ranked`` is ``(record_id, score in [0, 1])``
    best first; ``protected`` maps a protected record to its first family; ``family_slots`` maps a
    family to its protected ids (what the ``source_family`` pool block of ``_search`` consumes).
    """
    legs = _lib_legs(vector_hits, lexical_hits, use_hybrid, extra_legs)
    cfg = family_lib.FusionConfig(
        pool_size=max(pool_size, 1), protect_per_family=protect_n, k=rrf_k
    )
    result = family_lib.fuse(legs, cfg, family_lib.Floors(dict(floors or {})))
    ranked = [(c.record_id, c.fused_score) for c in result.candidates]
    protected = {c.record_id: c.protected_in[0] for c in result.candidates if c.protected}
    family_slots = {fam: list(ids) for fam, ids in result.trace.protected.items()}
    if tap.enabled():
        tr = result.trace
        tap.event(
            "read.fusion.pool",
            legs_in=dict(tr.legs_in),
            legs_kept=dict(tr.legs_kept),
            families=list(tr.families),
            per_family={
                f: sum(1 for c in result.candidates if f in c.families) for f in tr.families
            },
            protected={f: list(ids) for f, ids in tr.protected.items()},
            floored=[list(p) for p in tr.floored],
            evicted=len(tr.evicted),
            protected_trimmed=list(tr.protected_trimmed),
            fused_total=tr.fused_total,
            pool=len(ranked),
            schema_families={f: schema_family(f) for f in tr.families},
        )
    return ranked, protected, family_slots
