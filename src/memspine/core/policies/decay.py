"""Decay policy (M3): Ebbinghaus-informed tiers + reinforcement.

Pure decision logic: ``tier_for`` maps a record's idle time (days since the
last access, or since recording when never accessed) onto the four-tier
lifecycle. Reinforcement is already event-sourced — RETRIEVE events advance
``scoring.last_accessed_at`` through the projector — so an accessed record
naturally climbs back toward HOT without any special-case code here.

The decay *sweep* (workers/pipelines.py) turns tier changes into
DECAY_TRANSITION events through the write door; this module never does I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["DecayPolicy", "DecayTier"]

_SECONDS_PER_DAY = 86400.0


class DecayTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    DORMANT = "dormant"


class DecayOptions(PolicyOptions):
    hot_to_warm_days: int = constants.DECAY_HOT_TO_WARM_DAYS
    warm_to_cold_days: int = constants.DECAY_WARM_TO_COLD_DAYS
    cold_to_dormant_days: int = constants.DECAY_COLD_TO_DORMANT_DAYS


class DecayPolicy(BindablePolicy):
    name: ClassVar[str] = "decay"
    Options: ClassVar[type[PolicyOptions]] = DecayOptions

    def idle_days(self, record: MemoryRecord, now: datetime | None = None) -> float:
        """Days since the record was last touched (accessed or recorded)."""
        now = now or datetime.now(UTC)
        anchor = record.scoring.last_accessed_at or record.recorded_at
        # A record edited after its last retrieval is "touched" at the edit.
        anchor = max(anchor, record.recorded_at)
        return max(0.0, (now - anchor).total_seconds() / _SECONDS_PER_DAY)

    def tier_for(self, record: MemoryRecord, now: datetime | None = None) -> DecayTier:
        """The tier the record's idle time puts it in — absolute boundaries,
        so reinforcement (a fresh access) restores HOT in one step."""
        options = self.options
        assert isinstance(options, DecayOptions)
        idle = self.idle_days(record, now)
        if idle < options.hot_to_warm_days:
            return DecayTier.HOT
        if idle < options.warm_to_cold_days:
            return DecayTier.WARM
        if idle < options.cold_to_dormant_days:
            return DecayTier.COLD
        return DecayTier.DORMANT
