"""Decay policy contract (M3): Ebbinghaus tiers + reinforcement. Logic: Phase 3."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["DecayPolicy", "DecayTier"]


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

    def tier_for(self, record: MemoryRecord) -> DecayTier:
        raise NotImplementedError("decay logic lands in Phase 3 (plan §5)")
