"""Compression policy contract (M6): views-not-replacements summarization,
zstd cold-tier (D-32), E5 assembly-stage fallbacks. Logic: Phase 3."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["CompressionPolicy"]


class CompressionOptions(PolicyOptions):
    cold_tier_zstd_level: int = 3
    # E5 ordered fallbacks
    assembly_stage: list[str] = Field(default_factory=lambda: ["drop_lowest_score"])


class CompressionPolicy(BindablePolicy):
    name: ClassVar[str] = "compression"
    Options: ClassVar[type[PolicyOptions]] = CompressionOptions

    def should_compress(self, record: MemoryRecord) -> bool:
        raise NotImplementedError("compression logic lands in Phase 3 (plan §5)")
