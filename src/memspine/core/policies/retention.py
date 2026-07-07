"""Retention policy (M2/M3/M7): retention classes + legal hold.

Phase-3 scope: ``may_delete`` answers whether lifecycle machinery (decay,
pruning, the P4 hard-delete cascade) is allowed to remove a record. Legal-hold
namespaces are prefix-matched, mirroring the hierarchical namespace model
(M8.5): a hold on ``org/finance`` covers ``org/finance/q3``. Referential
retention (records other records depend on) joins with the P4 cascade.
"""

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

    def _options(self) -> RetentionOptions:
        options = self.options
        assert isinstance(options, RetentionOptions)
        return options

    def on_legal_hold(self, record: MemoryRecord) -> bool:
        for held in self._options().legal_hold_namespaces:
            if record.namespace == held or record.namespace.startswith(f"{held}/"):
                return True
        return False

    def may_delete(self, record: MemoryRecord) -> bool:
        """Whether lifecycle machinery may remove this record. Regulated-PII
        records are also protected from *automatic* deletion — only an explicit
        M7 forget (user intent) removes them (the P4 cascade enforces this)."""
        if self.on_legal_hold(record):
            return False
        return record.pii_tier.value != "regulated"
