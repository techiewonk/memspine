"""LIB-34: entity leg scoring with anti-hub damping (R-08).

Pure and unwired: wiring into the read path is R3-05a. No I/O, no model call, no clock read
(``now`` is passed in), no import of ``derive``. Entity resolution happens elsewhere; this module
takes *resolved entity ids* and an index from entity id to the records that mention it.

The leg belongs to the ``structured`` fusion family (``docs/v2/pipelines/retrieve.md`` step 5).

Score of a record ``r`` for the query entities ``Q`` (deduplicated, unknown ids ignored)::

    score(r) = coverage(r) * sum over e in Q matched in r of
                  role_weight(e, r) * damp(e) * recency(e, r)

* ``role_weight``: the strongest role the entity plays in ``r`` (subject > object > mention).
  Several mentions of one entity in one record count once.
* ``damp(e)``: the anti-hub factor, a function of the entity's *degree* (distinct records that
  mention it) and the index size ``N`` (``index.n_records``):

  - ``"idf"`` (default): ``log(1 + N / degree)``. Smoothed IDF; always positive, so an entity
    that appears in every record is damped to ``log 2`` rather than to zero.
  - ``"inv_sqrt"``: ``1 / sqrt(degree)``.
  - ``"mem0"``: ``1 / (1 + c * (degree - 1) ** 2)`` with ``c = mem0_c`` (default 0.001), the
    curve decided in R-08.

  The owner's own self-entity (``index.self_entity``) is additionally multiplied by
  ``self_factor`` (default 0.05) unless it is the only entity in the query.
* ``recency``: optional. With ``recency_half_life_days`` set and ``now`` given, the most recent
  mention date of the entity in the record gives ``floor + (1 - floor) * 0.5 ** (age / half_life)``.
  A record without a date, or any call without ``now``, gets a neutral 1.0.
* ``coverage``: ``1 + coverage_bonus * (m - 1)`` for a record matching ``m`` distinct query
  entities, so a record that mentions several of them beats one that mentions one hub often.

Scores are strictly positive (clamped to ``MIN_SCORE``) and never ``None`` or 0: a hit that
matched is always evidence. Hits are ranked by score descending, ties on record id ascending,
cut at ``k``. Output depends only on the *set* of query entities and the index contents, never on
their iteration order.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from memspine.read.fusion import Hit, Leg

__all__ = [
    "DEFAULT_ROLE_WEIGHTS",
    "ENTITY_LEG_FAMILY",
    "ENTITY_LEG_NAME",
    "MIN_SCORE",
    "DampingConfig",
    "EntityEvidence",
    "EntityIndex",
    "EntityLegResult",
    "EntityMention",
    "Role",
    "damping_factor",
    "entity_leg",
    "entity_leg_explained",
]

ENTITY_LEG_NAME = "entity"
ENTITY_LEG_FAMILY = "structured"
MIN_SCORE = 1e-9

Role = Literal["subject", "object", "mention"]
DampingMode = Literal["idf", "inv_sqrt", "mem0"]

DEFAULT_ROLE_WEIGHTS: Mapping[str, float] = {"subject": 1.0, "object": 0.7, "mention": 0.4}


@dataclass(frozen=True)
class EntityMention:
    """One mention of an entity in a record, with its role and (optional) date."""

    record_id: str
    role: Role = "mention"
    date: datetime | None = None


@dataclass(frozen=True)
class EntityIndex:
    """Entity id -> mentions. ``n_records`` is the store size ``N`` (defaults to the number of
    distinct records in the index). ``self_entity`` is the owner's own entity id, if known."""

    mentions: Mapping[str, Sequence[EntityMention]] = field(default_factory=dict)
    n_records: int | None = None
    self_entity: str | None = None

    def degree(self, entity_id: str) -> int:
        return len({m.record_id for m in self.mentions.get(entity_id, ())})

    def size(self) -> int:
        if self.n_records is not None:
            return self.n_records
        return len({m.record_id for ms in self.mentions.values() for m in ms})


@dataclass(frozen=True)
class DampingConfig:
    """Knobs of the leg. Defaults are the documented ones."""

    mode: DampingMode = "idf"
    mem0_c: float = 0.001
    self_factor: float = 0.05
    role_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_ROLE_WEIGHTS))
    coverage_bonus: float = 0.5
    recency_half_life_days: float | None = None
    recency_floor: float = 0.5
    now: datetime | None = None

    def __post_init__(self) -> None:
        if self.mem0_c < 0:
            raise ValueError("mem0_c must not be negative")
        if not 0.0 < self.self_factor <= 1.0:
            raise ValueError("self_factor must be in (0, 1]")
        if self.coverage_bonus < 0:
            raise ValueError("coverage_bonus must not be negative")
        if self.recency_half_life_days is not None and self.recency_half_life_days <= 0:
            raise ValueError("recency_half_life_days must be positive")
        if not 0.0 <= self.recency_floor <= 1.0:
            raise ValueError("recency_floor must be in [0, 1]")
        for role in ("subject", "object", "mention"):
            if self.role_weights.get(role, 0.0) <= 0:
                raise ValueError(f"role weight for {role!r} must be positive")


@dataclass(frozen=True)
class EntityEvidence:
    """Why one query entity contributed to one record."""

    entity_id: str
    role: str
    role_weight: float
    damping: float
    recency: float
    contribution: float


@dataclass(frozen=True)
class EntityLegResult:
    leg: Leg
    provenance: Mapping[str, tuple[EntityEvidence, ...]]  # record id -> evidence, by entity id
    matched_entities: tuple[str, ...]  # query entities found in the index, sorted
    unknown_entities: tuple[str, ...]  # query entities absent from the index, sorted


def damping_factor(degree: int, n_records: int, cfg: DampingConfig | None = None) -> float:
    """The anti-hub factor for an entity of ``degree`` in a store of ``n_records``."""
    cfg = cfg or DampingConfig()
    d = max(degree, 1)
    if cfg.mode == "idf":
        return math.log1p(max(n_records, d) / d)
    if cfg.mode == "inv_sqrt":
        return 1.0 / math.sqrt(d)
    return 1.0 / (1.0 + cfg.mem0_c * (d - 1) ** 2)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _recency(dates: Iterable[datetime | None], cfg: DampingConfig) -> float:
    if cfg.recency_half_life_days is None or cfg.now is None:
        return 1.0
    known = [_utc(d) for d in dates if d is not None]
    if not known:
        return 1.0
    age_days = max(0.0, (_utc(cfg.now) - max(known)).total_seconds() / 86400.0)
    decay = 0.5 ** (age_days / cfg.recency_half_life_days)
    return cfg.recency_floor + (1.0 - cfg.recency_floor) * decay


def entity_leg_explained(
    query_entities: Iterable[str],
    index: EntityIndex,
    *,
    k: int,
    damping: DampingConfig | None = None,
) -> EntityLegResult:
    """Score records for resolved ``query_entities``; return the leg and per-hit provenance."""
    if k < 1:
        raise ValueError("k must be at least 1")
    cfg = damping or DampingConfig()
    wanted = sorted(set(query_entities))
    matched = [e for e in wanted if index.degree(e) > 0]
    unknown = tuple(e for e in wanted if index.degree(e) == 0)
    n = index.size()
    only_one = len(wanted) == 1

    per_record: dict[str, list[EntityEvidence]] = {}
    for e in matched:
        d = damping_factor(index.degree(e), n, cfg)
        if index.self_entity is not None and e == index.self_entity and not only_one:
            d *= cfg.self_factor
        by_record: dict[str, list[EntityMention]] = {}
        for m in index.mentions[e]:
            by_record.setdefault(m.record_id, []).append(m)
        for rid, ms in by_record.items():
            # strongest role; ties on role name so the choice never depends on input order
            role, w = max(
                ((m.role, cfg.role_weights.get(m.role, 0.0)) for m in ms),
                key=lambda rw: (rw[1], rw[0]),
            )
            rec = _recency((m.date for m in ms), cfg)
            per_record.setdefault(rid, []).append(EntityEvidence(e, role, w, d, rec, w * d * rec))

    scored: list[tuple[str, float, tuple[EntityEvidence, ...]]] = []
    for rid, evs in per_record.items():
        evs_sorted = tuple(sorted(evs, key=lambda x: x.entity_id))
        base = math.fsum(ev.contribution for ev in evs_sorted)
        cov = 1.0 + cfg.coverage_bonus * (len(evs_sorted) - 1)
        scored.append((rid, max(base * cov, MIN_SCORE), evs_sorted))
    scored.sort(key=lambda t: (-t[1], t[0]))
    top = scored[:k]
    hits = [Hit(record_id=rid, score=s, rank=i) for i, (rid, s, _) in enumerate(top, start=1)]
    return EntityLegResult(
        leg=Leg(ENTITY_LEG_NAME, ENTITY_LEG_FAMILY, hits),
        provenance={rid: evs for rid, _, evs in top},
        matched_entities=tuple(matched),
        unknown_entities=unknown,
    )


def entity_leg(
    query_entities: Iterable[str],
    index: EntityIndex,
    *,
    k: int,
    damping: DampingConfig | None = None,
) -> Leg:
    """The entity leg (family ``structured``): ranked hits, best first, at most ``k``.

    Use :func:`entity_leg_explained` when per-hit provenance (entity, role) is needed."""
    return entity_leg_explained(query_entities, index, k=k, damping=damping).leg
