"""Retention policy contract (M2/M3/M7): retention classes, legal hold,
referential retention. Logic: Phase 3-4."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["RetentionPolicy"]


class RetentionOptions(PolicyOptions):
    default_class: str = "standard"
    legal_hold_namespaces: list[str] = Field(default_factory=list)


class RetentionPolicy(BindablePolicy):
    name: ClassVar[str] = "retention"
    Options: ClassVar[type[PolicyOptions]] = RetentionOptions

    def may_delete(self, record: MemoryRecord) -> bool:
        raise NotImplementedError("retention logic lands in Phases 3-4 (plan §5)")
