"""Assembly / ReadPolicy contract (M12 + E2): stability-sorted placement,
envelopes, theta-abstain, MMR. Logic: Phase 1.

E2 placement order (cache-aware): persona -> skills/rules -> semantic facts ->
[cache boundary] -> retrieved episodic + working + query.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["AssemblyPolicy"]


class AssemblyOptions(PolicyOptions):
    theta_abstain: float = constants.THETA_ABSTAIN
    mmr_lambda: float = 0.7
    cache_aware_placement: bool = True  # E2


class AssemblyPolicy(BindablePolicy):
    name: ClassVar[str] = "assembly"
    Options: ClassVar[type[PolicyOptions]] = AssemblyOptions

    def assemble(self, candidates: list[MemoryRecord], budget_tokens: int) -> list[MemoryRecord]:
        raise NotImplementedError("assembly logic lands in Phase 1 (plan §5, E2)")
