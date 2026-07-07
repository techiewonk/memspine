"""Dedup policy contract (M5/D-27): MinHash-LSH stage 1 -> cosine confirm stage 2.

Logic lands in Phase 2; Phase 0 fixes the two-stage options schema.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["DedupPolicy"]


class DedupOptions(PolicyOptions):
    minhash_num_perm: int = constants.MINHASH_NUM_PERM
    lsh_threshold: float = constants.LSH_THRESHOLD
    cosine_threshold: float = constants.DEDUP_COSINE_THRESHOLD


class DedupPolicy(BindablePolicy):
    name: ClassVar[str] = "dedup"
    Options: ClassVar[type[PolicyOptions]] = DedupOptions

    def candidates(self, record: MemoryRecord) -> list[str]:
        raise NotImplementedError("two-stage dedup lands in Phase 2 (plan §5, D-27)")
