"""LIB-10: family fusion, protected pool, calibrated floors (R-11, R-12, R-13).

Pure and unwired: wiring into the read path is R3-06. No I/O, no model call, no engine imports.

Pipeline (``docs/v2/pipelines/retrieve.md`` steps 7 and 9):

1. Each leg's hits are normalised (rank defaults to list position, 1-based; a record a leg lists
   twice keeps its best rank there).
2. Legs of one *source family* are merged first: per record, the best (smallest) rank over the
   family's legs. A second leg of a family therefore never adds a vote.
3. The top ``protect_per_family`` records of every family are *protected* (votes de-duplicated:
   a record takes one slot per family however many legs found it).
4. Per-leg score floors (:func:`calibrate`) drop hits below floor unless the record is protected in
   that hit's family. Unscored hits (``score is None``) are never dropped by a floor.
5. Families are fused with RRF (``1 / (k + rank)``, equal weights, k = 60). With one leg per family
   this equals plain RRF; ``mode="rrf"`` treats every leg as its own family (plain RRF fallback).
6. The pool is capped to ``pool_size`` (decided default 64): protected records are never evicted,
   the rest fill by fused rank.

Determinism: nothing depends on the order the legs are passed in. Legs and families are visited in
sorted-name order, so even float sums are reproducible; ties break on the per-family best ranks
(families in name order, absent ranks last) and finally on the record id.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from memspine.schemas.public import ScoredItem

__all__ = [
    "DEFAULT_POOL_SIZE",
    "DEFAULT_PROTECT_PER_FAMILY",
    "DEFAULT_RRF_K",
    "Floors",
    "FusedCandidate",
    "FusionConfig",
    "FusionResult",
    "FusionTrace",
    "Hit",
    "Leg",
    "Provenance",
    "calibrate",
    "fuse",
    "percentile",
    "to_scored_items",
]

DEFAULT_RRF_K = 60
DEFAULT_POOL_SIZE = 64
DEFAULT_PROTECT_PER_FAMILY = 3  # the reference arm runs per_leg 3 (R-12)
DEFAULT_FLOOR_PERCENTILE = 95.0  # mirrors the p95 of StoreCalibration

Mode = Literal["family_rrf", "rrf"]


# --- inputs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """One ranked hit of a leg. ``rank`` is 1-based; None means "position in the leg's list".
    ``score`` is the leg-local raw score; None means unscored (never compared with a floor)."""

    record_id: str
    score: float | None = None
    rank: int | None = None


@dataclass(frozen=True)
class Leg:
    """A named leg of a family (e.g. ``bm25`` of ``lexical``) with its hits in rank order."""

    name: str
    family: str
    hits: tuple[Hit, ...] = ()

    def __init__(self, name: str, family: str, hits: Iterable[Hit] = ()) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "family", family)
        object.__setattr__(self, "hits", tuple(hits))


@dataclass(frozen=True)
class FusionConfig:
    """Knobs of one fusion. Defaults are the decided ones (R-11, R-12, R-13)."""

    mode: Mode = "family_rrf"
    k: int = DEFAULT_RRF_K
    pool_size: int = DEFAULT_POOL_SIZE
    protect_per_family: int = DEFAULT_PROTECT_PER_FAMILY
    normalise: bool = True  # divide by (non-empty families / (k + 1)) so scores sit in [0, 1]

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError("k must be at least 1")
        if self.pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        if self.protect_per_family < 0:
            raise ValueError("protect_per_family must not be negative")


# --- floors ----------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float:
    """The ``p``-th percentile (0..100) by linear interpolation. ``values`` must be non-empty."""
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile must be within 0..100")
    xs = sorted(values)
    pos = (len(xs) - 1) * p / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


@dataclass(frozen=True)
class Floors:
    """Per-leg score floors. A leg without an entry has no floor."""

    by_leg: Mapping[str, float] = field(default_factory=dict)

    def floor(self, leg: str) -> float | None:
        return self.by_leg.get(leg)


def calibrate(
    samples: Mapping[str, Sequence[float]],
    percentiles: Mapping[str, float] | float = DEFAULT_FLOOR_PERCENTILE,
) -> Floors:
    """Floors from a store-level calibration sample.

    ``samples`` maps a leg name to scores that leg produced on the store (for example for probe
    queries with no relevant record). A leg's floor is its ``p``-th percentile; ``percentiles`` is
    one number for every leg or a per-leg map (legs missing from the map use the default). Legs with
    an empty sample get no floor. Non-finite scores are ignored.
    """
    out: dict[str, float] = {}
    for leg in sorted(samples):
        vals = [float(v) for v in samples[leg] if math.isfinite(v)]
        if not vals:
            continue
        p = (
            percentiles
            if isinstance(percentiles, int | float)
            else percentiles.get(leg, DEFAULT_FLOOR_PERCENTILE)
        )
        out[leg] = percentile(vals, float(p))
    return Floors(by_leg=out)


# --- outputs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """What one leg said about one record."""

    leg: str
    family: str
    rank: int
    score: float | None


@dataclass(frozen=True)
class FusedCandidate:
    record_id: str
    fused_score: float
    families: tuple[str, ...]  # families that voted, sorted
    legs: tuple[str, ...]  # legs that found it, sorted
    protected: bool
    protected_in: tuple[str, ...]  # families in which it is a top-n hit
    best_leg: str
    best_rank: int
    provenance: tuple[Provenance, ...]  # every surviving leg hit, sorted by (leg, rank)


@dataclass(frozen=True)
class FusionTrace:
    mode: Mode
    k: int
    pool_size: int
    protect_per_family: int
    legs_in: Mapping[str, int]  # leg -> hits received
    legs_kept: Mapping[str, int]  # leg -> hits that survived floors
    floored: tuple[tuple[str, str], ...]  # (leg, record_id) dropped below floor
    families: tuple[str, ...]  # non-empty families that voted
    protected: Mapping[str, tuple[str, ...]]  # family -> protected record ids
    fused_total: int  # distinct records before the pool cut
    evicted: tuple[str, ...]  # unprotected (or trimmed) records cut at pool_size
    protected_trimmed: tuple[str, ...]  # protected records that did not fit pool_size


@dataclass(frozen=True)
class FusionResult:
    candidates: tuple[FusedCandidate, ...]  # best first, at most pool_size
    trace: FusionTrace


# --- fusion ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Norm:
    name: str
    family: str
    hits: tuple[
        tuple[str, int, float | None], ...
    ]  # (record_id, rank, score), best rank per record


def _normalise(leg: Leg, family_of_leg: str) -> _Norm:
    best: dict[str, tuple[int, int, float | None]] = {}
    for pos, h in enumerate(leg.hits):
        rank = pos + 1 if h.rank is None else h.rank
        if rank < 1:
            raise ValueError("rank is 1-based")
        key = (rank, pos)
        cur = best.get(h.record_id)
        if cur is None or key < (cur[0], cur[1]):
            best[h.record_id] = (rank, pos, h.score)
    ordered = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    return _Norm(leg.name, family_of_leg, tuple((rid, v[0], v[2]) for rid, v in ordered))


def fuse(
    legs: Sequence[Leg],
    config: FusionConfig | None = None,
    floors: Floors | None = None,
) -> FusionResult:
    """Family fusion with a protected pool. See the module docstring for the steps.

    Two legs with the same name are one leg (their hits are concatenated before ranking only if they
    carry explicit ranks; give legs unique names). Duplicate legs, meaning the same name and family
    seen twice with the same hits, change nothing.
    """
    cfg = config or FusionConfig()
    flo = floors or Floors()

    # leg name -> normalised leg; same-named legs merge by best rank
    merged: dict[str, _Norm] = {}
    for leg in legs:
        fam = leg.family if cfg.mode == "family_rrf" else leg.name
        n = _normalise(leg, fam)
        prev = merged.get(leg.name)
        if prev is None:
            merged[leg.name] = n
        else:
            best: dict[str, tuple[int, float | None]] = {r: (rk, s) for r, rk, s in prev.hits}
            for r, rk, s in n.hits:
                if r not in best or rk < best[r][0]:
                    best[r] = (rk, s)
            merged[leg.name] = _Norm(
                leg.name,
                prev.family,
                tuple(
                    (r, rk, s)
                    for r, (rk, s) in sorted(best.items(), key=lambda kv: (kv[1][0], kv[0]))
                ),
            )
    names = sorted(merged)

    # 2/3: family-level best rank per record (before floors) and protected top-n
    fam_best: dict[str, dict[str, int]] = {}
    for name in names:
        n = merged[name]
        fb = fam_best.setdefault(n.family, {})
        for rid, rank, _ in n.hits:
            if rid not in fb or rank < fb[rid]:
                fb[rid] = rank
    protected_in: dict[str, list[str]] = {}
    for fam in sorted(fam_best):
        order = sorted(fam_best[fam].items(), key=lambda kv: (kv[1], kv[0]))
        protected_in[fam] = [rid for rid, _ in order[: cfg.protect_per_family]]
    protected_sets = {fam: set(ids) for fam, ids in protected_in.items()}

    # 4: floors
    provenance: dict[str, list[Provenance]] = {}
    floored: list[tuple[str, str]] = []
    legs_in: dict[str, int] = {}
    legs_kept: dict[str, int] = {}
    kept_rank: dict[str, dict[str, int]] = {}  # family -> record -> best surviving rank
    for name in names:
        n = merged[name]
        legs_in[name] = len(n.hits)
        floor = flo.floor(name)
        kept = 0
        for rid, rank, score in n.hits:
            if (
                floor is not None
                and score is not None
                and score < floor
                and rid not in protected_sets.get(n.family, ())
            ):
                floored.append((name, rid))
                continue
            kept += 1
            provenance.setdefault(rid, []).append(Provenance(name, n.family, rank, score))
            fk = kept_rank.setdefault(n.family, {})
            if rid not in fk or rank < fk[rid]:
                fk[rid] = rank
        legs_kept[name] = kept

    # 5: RRF across families
    fams = sorted(kept_rank)
    n_fams = len(fams)
    denom = (n_fams / (cfg.k + 1)) if (cfg.normalise and n_fams) else 1.0
    scores: dict[str, float] = {}
    for rid in provenance:
        total = 0.0
        for fam in fams:  # sorted: float sums are order-stable
            rk = kept_rank[fam].get(rid)
            if rk is not None:
                total += 1.0 / (cfg.k + rk)
        scores[rid] = total / denom
    absent = 1 + max((rk for d in kept_rank.values() for rk in d.values()), default=0)

    def tie_key(rid: str) -> tuple:
        return (-scores[rid], tuple(kept_rank[f].get(rid, absent) for f in fams), rid)

    ranked = sorted(scores, key=tie_key)

    # 6: pool. Protected records survive; the rest fill by fused rank.
    prot_of: dict[str, list[str]] = {}
    for fam, ids in protected_in.items():
        for rid in ids:
            if rid in scores:
                prot_of.setdefault(rid, []).append(fam)
    protected_ids = [rid for rid in ranked if rid in prot_of]
    trimmed: list[str] = []
    if len(protected_ids) > cfg.pool_size:
        # Family coverage first: each family's best protected hit, then its second, and so on.
        def cover_key(rid: str) -> tuple:
            depth = min(protected_in[f].index(rid) for f in prot_of[rid])
            return (depth, *tie_key(rid))

        by_cover = sorted(protected_ids, key=cover_key)
        keep = set(by_cover[: cfg.pool_size])
        trimmed = [rid for rid in ranked if rid in prot_of and rid not in keep]
        protected_ids = [rid for rid in protected_ids if rid in keep]
    chosen = set(protected_ids)
    for rid in ranked:
        if len(chosen) >= cfg.pool_size:
            break
        chosen.add(rid)
    pool = [rid for rid in ranked if rid in chosen]
    evicted = [rid for rid in ranked if rid not in chosen and rid not in trimmed]

    cands: list[FusedCandidate] = []
    for rid in pool:
        prov = tuple(sorted(provenance[rid], key=lambda p: (p.leg, p.rank)))
        best = min(prov, key=lambda p: (p.rank, p.leg))
        cands.append(
            FusedCandidate(
                record_id=rid,
                fused_score=scores[rid],
                families=tuple(sorted({p.family for p in prov})),
                legs=tuple(sorted({p.leg for p in prov})),
                protected=rid in prot_of,
                protected_in=tuple(sorted(prot_of.get(rid, ()))),
                best_leg=best.leg,
                best_rank=best.rank,
                provenance=prov,
            )
        )
    trace = FusionTrace(
        mode=cfg.mode,
        k=cfg.k,
        pool_size=cfg.pool_size,
        protect_per_family=cfg.protect_per_family,
        legs_in=dict(legs_in),
        legs_kept=dict(legs_kept),
        floored=tuple(floored),
        families=tuple(fams),
        protected={f: tuple(ids) for f, ids in protected_in.items() if ids},
        fused_total=len(ranked),
        evicted=tuple(evicted),
        protected_trimmed=tuple(trimmed),
    )
    return FusionResult(candidates=tuple(cands), trace=trace)


def to_scored_items(result: FusionResult) -> list[ScoredItem]:
    """The fused order as public :class:`ScoredItem` (every fused candidate is scored)."""
    return [
        ScoredItem(record_id=c.record_id, scored=True, score=c.fused_score)
        for c in result.candidates
    ]
