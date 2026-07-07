"""Consolidation policy contract (M2): triggers + 5-stage pipeline. Logic: Phase 3."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import Field

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions

__all__ = ["ConsolidationPolicy", "ConsolidationTrigger"]


class ConsolidationTrigger(StrEnum):
    SESSION_END = "session_end"
    HEAT = "heat"
    SLEEP_CYCLE = "sleep_cycle"


class ConsolidationOptions(PolicyOptions):
    triggers: list[ConsolidationTrigger] = Field(
        default_factory=lambda: [ConsolidationTrigger.SESSION_END]
    )
    heat_threshold: int = constants.CONSOLIDATION_HEAT_THRESHOLD


class ConsolidationPolicy(BindablePolicy):
    name: ClassVar[str] = "consolidation"
    Options: ClassVar[type[PolicyOptions]] = ConsolidationOptions

    def should_trigger(self, trigger: ConsolidationTrigger, heat: int = 0) -> bool:
        raise NotImplementedError("consolidation logic lands in Phase 3 (plan §5)")
