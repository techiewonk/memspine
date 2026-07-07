"""Conflict policy contract (M4): bi-temporal ADD/UPDATE/INVALIDATE/NOOP + R1-R5.

Logic lands in Phase 2; Phase 0 fixes the verdict vocabulary and signature.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ConflictPolicy", "ConflictVerdict"]


class ConflictVerdict(StrEnum):
    ADD = "add"
    UPDATE = "update"
    INVALIDATE = "invalidate"
    NOOP = "noop"


class ConflictOptions(PolicyOptions):
    bias: str = "newest"  # R1-R5 ladder default; templates may override


class ConflictPolicy(BindablePolicy):
    name: ClassVar[str] = "conflict"
    Options: ClassVar[type[PolicyOptions]] = ConflictOptions

    def resolve(self, incoming: MemoryRecord, existing: MemoryRecord) -> ConflictVerdict:
        raise NotImplementedError("conflict ladder lands in Phase 2 (plan §5)")
