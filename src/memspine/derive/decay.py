# ruff: noqa: E501
"""Decay tiers, compression plan and multi-resolution text plan (LIB-33; O-22, M-22, P-16; K_C).

Pure and deterministic: no I/O, no clock reads (``now`` is an argument), no model calls. This module only
*plans*. The tier sweep and the summarisation model call that carry the plans out are wired later (O4-09);
nothing here is imported by the engine.

Rules encoded
-------------
* **Truth is never decayed.** The L0 event log is the truth. Decay changes a record's *access tier* and its
  retrieval weight; it never deletes anything, and nothing in this module can express a deletion (no plan
  type has a delete action). Erasure and retention (session 11) are separate machinery. Legal-held and
  quarantined records are not touched at all.
* **Activation** (ACT-R flavoured, Ebbinghaus recency), deterministic, in ``[0, 1]``::

      R = exp(-idle_days / S_kind)                       recency, Ebbinghaus e^{-t/S}
      F = min(1, ln(1 + n) / ln(1 + n_sat))              frequency from access count n
      I = clamp(importance, 0, 1)                        importance hint
      activation = w_rec * R + w_freq * F + w_imp * I

  ``idle_days`` counts from the later of ``last_accessed_at`` and ``recorded_at`` (an access restores recency
  at once, as in O-22). Weights are normalised to sum to 1 so activation stays in ``[0, 1]``.
* **Tiers** hot > warm > cold > dormant (tier migration, the intra-store movement; not "demotion", which is
  reserved for working-memory/long-term interface crossings). Three boundaries ``b_i`` separate neighbours.
  A record moves down while ``activation < b_i`` and moves up only when ``activation >= b_i + margin``. The
  dead band of width ``margin`` is the hysteresis: a record whose activation wobbles inside a band stays in
  its current tier, so it cannot flap.
* **Protected records** (pinned, standing instruction, contested, or a protected kind) are never moved to a
  colder tier. If one sits in a colder tier it is restored to hot.
* **Compression** is the sub-operation ``cmp(.; b)`` of K_C. A job describes a multi-resolution summary of a
  cold group: full (the members themselves, kept) -> paragraph -> sentence -> keyword, each with a token
  budget and lineage to every member. The model call that writes the text is elsewhere. Compression never
  deletes members: it lowers their retrieval tier at most (``member_tier_after``), and raw stays the
  authority behind the full pointer.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ColdGroup",
    "CompressBudget",
    "CompressJob",
    "DecayPolicy",
    "GroupMember",
    "LevelSpec",
    "RecordStats",
    "Resolution",
    "Tier",
    "TierMove",
    "activation",
    "compression_plan",
    "tier_plan",
]

_SECONDS_PER_DAY = 86400.0


class Tier(IntEnum):
    """Access tier. Lower value is hotter, so ``tier < other`` reads "hotter than"."""

    HOT = 0
    WARM = 1
    COLD = 2
    DORMANT = 3


class Resolution(StrEnum):
    """Multi-resolution text levels, richest first. ``FULL`` is the members themselves (a pointer)."""

    FULL = "full"
    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"
    KEYWORD = "keyword"


_LEVEL_ORDER: tuple[Resolution, ...] = (
    Resolution.PARAGRAPH,
    Resolution.SENTENCE,
    Resolution.KEYWORD,
)

_DEFAULT_STRENGTH_DAYS: dict[str, float] = {
    "working": 3.0,
    "prospective": 14.0,
    "episodic": 30.0,
    "resource": 60.0,
    "associative": 60.0,
    "shared": 60.0,
    "semantic": 90.0,
    "reflective": 120.0,
    "procedural": 180.0,
}


def _aware(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecordStats(_Frozen):
    """What decay needs to know about one record. The caller builds these from the store."""

    record_id: str
    kind: str = "episodic"
    layer: Literal["L0", "L1", "L2", "L3", "L4", "L5"] = "L1"
    recorded_at: datetime
    last_accessed_at: datetime | None = None
    access_count: int = Field(default=0, ge=0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    tier: Tier = Tier.HOT
    pinned: bool = False
    standing: bool = False
    contested: bool = False
    legal_hold: bool = False
    quarantined: bool = False
    deleted: bool = False


class DecayPolicy(_Frozen):
    """Tunables. Defaults put an untouched, mid-importance episodic record in warm at about 10 days,
    cold at about 29 days and dormant at about 62 days (close to the 7/30/90 day boundaries of O-22)."""

    strength_days: Mapping[str, float] = Field(default_factory=lambda: dict(_DEFAULT_STRENGTH_DAYS))
    default_strength_days: float = Field(default=60.0, gt=0.0)
    w_rec: float = Field(default=0.6, ge=0.0)
    w_freq: float = Field(default=0.25, ge=0.0)
    w_imp: float = Field(default=0.15, ge=0.0)
    freq_saturation: int = Field(default=20, ge=1)
    #: ``b_i`` between hot|warm, warm|cold, cold|dormant: move down below, up at or above ``b_i + margin``.
    boundaries: tuple[float, float, float] = (0.50, 0.30, 0.15)
    margin: float = Field(default=0.05, ge=0.0)
    protected_kinds: frozenset[str] = frozenset()

    @field_validator("strength_days")
    @classmethod
    def _positive_strengths(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        if any(s <= 0 for s in value.values()):
            raise ValueError("strength_days values must be positive")
        return value

    @model_validator(mode="after")
    def _check(self) -> DecayPolicy:
        if self.w_rec + self.w_freq + self.w_imp <= 0:
            raise ValueError("at least one activation weight must be positive")
        b = self.boundaries
        if not (b[0] > b[1] > b[2] >= 0.0):
            raise ValueError("boundaries must be strictly decreasing and non-negative")
        if b[0] - b[1] <= self.margin or b[1] - b[2] <= self.margin:
            raise ValueError(
                "margin must be smaller than the gap between boundaries (bands would overlap)"
            )
        return self

    def strength(self, kind: str) -> float:
        return float(self.strength_days.get(kind, self.default_strength_days))


class TierMove(_Frozen):
    """One planned tier migration. It never deletes: ``to_tier`` is always a tier, never a removal."""

    record_id: str
    from_tier: Tier
    to_tier: Tier
    activation: float
    reason: Literal["idle", "reactivated", "protected_restore"]


def activation(
    record_stats: RecordStats, now: datetime, policy: DecayPolicy | None = None
) -> float:
    """Activation in ``[0, 1]``; see the module docstring for the formula. Pure, so same inputs, same value."""
    pol = policy or DecayPolicy()
    anchor = _aware(record_stats.recorded_at)
    if record_stats.last_accessed_at is not None:
        anchor = max(anchor, _aware(record_stats.last_accessed_at))
    idle_days = max(0.0, (_aware(now) - anchor).total_seconds() / _SECONDS_PER_DAY)
    recency = math.exp(-idle_days / pol.strength(record_stats.kind))
    frequency = min(1.0, math.log1p(record_stats.access_count) / math.log1p(pol.freq_saturation))
    importance = min(1.0, max(0.0, record_stats.importance))
    total = pol.w_rec + pol.w_freq + pol.w_imp
    value = (pol.w_rec * recency + pol.w_freq * frequency + pol.w_imp * importance) / total
    return min(1.0, max(0.0, value))


def _is_protected(rec: RecordStats, pol: DecayPolicy) -> bool:
    return rec.pinned or rec.standing or rec.contested or rec.kind in pol.protected_kinds


def _target_tier(current: Tier, act: float, pol: DecayPolicy) -> Tier:
    """Hysteresis: down while below the boundary, up only at boundary + margin."""
    idx = int(current)
    b = pol.boundaries
    while idx < 3 and act < b[idx]:
        idx += 1
    if idx != int(current):
        return Tier(idx)
    while idx > 0 and act >= b[idx - 1] + pol.margin:
        idx -= 1
    return Tier(idx)


def tier_plan(
    records: Iterable[RecordStats], now: datetime, policy: DecayPolicy | None = None
) -> list[TierMove]:
    """Tier moves for ``records`` at ``now``, sorted by record id. Records that stay put emit nothing, so a
    second run over the applied result is empty (idempotent). Legal-held, quarantined and deleted records are
    skipped; protected records are never moved colder."""
    pol = policy or DecayPolicy()
    moves: list[TierMove] = []
    for rec in sorted(records, key=lambda r: r.record_id):
        if rec.legal_hold or rec.quarantined or rec.deleted:
            continue
        act = activation(rec, now, pol)
        if _is_protected(rec, pol):
            if rec.tier != Tier.HOT:
                moves.append(
                    TierMove(
                        record_id=rec.record_id,
                        from_tier=rec.tier,
                        to_tier=Tier.HOT,
                        activation=act,
                        reason="protected_restore",
                    )
                )
            continue
        target = _target_tier(rec.tier, act, pol)
        if target != rec.tier:
            reason = "idle" if target > rec.tier else "reactivated"
            moves.append(
                TierMove(
                    record_id=rec.record_id,
                    from_tier=rec.tier,
                    to_tier=target,
                    activation=act,
                    reason=reason,
                )
            )
    return moves


# ---------------------------------------------------------------------------------------------------------
# Compression plan
# ---------------------------------------------------------------------------------------------------------


class GroupMember(_Frozen):
    record_id: str
    layer: Literal["L0", "L1", "L2", "L3", "L4", "L5"] = "L1"
    tokens: int = Field(ge=0)
    tier: Tier = Tier.COLD
    pinned: bool = False
    standing: bool = False
    contested: bool = False
    legal_hold: bool = False
    quarantined: bool = False
    #: Already has multi-resolution text (idempotency: nothing to do for a group that is all compressed).
    compressed: bool = False


class ColdGroup(_Frozen):
    group_id: str
    members: tuple[GroupMember, ...]


class CompressBudget(_Frozen):
    """Token budget per level plus plan-wide caps. Level budgets are ceilings; a level is also capped by
    its source level's size so a summary is never planned larger than what it summarises."""

    paragraph_tokens: int = Field(default=200, ge=1)
    sentence_tokens: int = Field(default=40, ge=1)
    keyword_tokens: int = Field(default=12, ge=1)
    min_group_size: int = Field(default=2, ge=1)
    max_jobs: int | None = Field(default=None, ge=0)
    max_input_tokens: int | None = Field(default=None, ge=0)
    member_tier_after: Tier = Tier.DORMANT

    def level_tokens(self, level: Resolution) -> int:
        return {
            Resolution.PARAGRAPH: self.paragraph_tokens,
            Resolution.SENTENCE: self.sentence_tokens,
            Resolution.KEYWORD: self.keyword_tokens,
        }[level]


class LevelSpec(_Frozen):
    level: Resolution
    source_level: Resolution
    token_budget: int


class CompressJob(_Frozen):
    """A summarisation job to run elsewhere. ``lineage`` lists every member (sorted); members are retained."""

    job_id: str
    group_id: str
    lineage: tuple[str, ...]
    input_tokens: int
    levels: tuple[LevelSpec, ...]
    #: Retrieval tier members are lowered to once the summaries exist; applied as max(current, this), so a
    #: member is never raised and never deleted.
    member_tier_after: Tier
    #: Fixed marker: the plan can only retain members. There is no delete variant.
    member_effect: Literal["retain"] = "retain"


def _eligible(m: GroupMember) -> bool:
    return (
        not (m.pinned or m.standing or m.contested or m.legal_hold or m.quarantined)
        and m.tier >= Tier.COLD
    )


def _job_id(group_id: str, lineage: Sequence[str], levels: Sequence[LevelSpec]) -> str:
    h = hashlib.sha256()
    h.update(group_id.encode())
    for item in lineage:
        h.update(b"\x00" + item.encode())
    for spec in levels:
        h.update(f"\x01{spec.level.value}:{spec.token_budget}".encode())
    return "cmp_" + h.hexdigest()[:16]


def compression_plan(
    cold_groups: Iterable[ColdGroup], budget: CompressBudget | None = None
) -> list[CompressJob]:
    """Plan multi-resolution summaries for cold groups. Largest groups first (ties by group id), so a cap on
    jobs or input tokens keeps the most valuable work. Protected, held, quarantined and non-cold members are
    left out of the job; groups left below ``min_group_size`` or already fully compressed are skipped."""
    bud = budget or CompressBudget()
    candidates: list[tuple[str, list[GroupMember]]] = []
    for group in cold_groups:
        members = sorted((m for m in group.members if _eligible(m)), key=lambda m: m.record_id)
        if len(members) < bud.min_group_size or all(m.compressed for m in members):
            continue
        candidates.append((group.group_id, members))
    candidates.sort(key=lambda c: (-sum(m.tokens for m in c[1]), c[0]))

    jobs: list[CompressJob] = []
    spent = 0
    for group_id, members in candidates:
        if bud.max_jobs is not None and len(jobs) >= bud.max_jobs:
            break
        total = sum(m.tokens for m in members)
        if bud.max_input_tokens is not None and spent + total > bud.max_input_tokens:
            continue
        levels: list[LevelSpec] = []
        source, source_tokens = Resolution.FULL, total
        for level in _LEVEL_ORDER:
            tokens = bud.level_tokens(level)
            if tokens >= source_tokens:
                continue  # a summary no smaller than its source is pointless: skip the level
            levels.append(LevelSpec(level=level, source_level=source, token_budget=tokens))
            source, source_tokens = level, tokens
        if not levels:
            continue
        lineage = tuple(m.record_id for m in members)
        jobs.append(
            CompressJob(
                job_id=_job_id(group_id, lineage, levels),
                group_id=group_id,
                lineage=lineage,
                input_tokens=total,
                levels=tuple(levels),
                member_tier_after=bud.member_tier_after,
            )
        )
        spent += total
    return jobs
