"""Memory Firewall policy contract (E1 / M17, OWASP ASI06).

Full firewall lands in Phase 4; the *columns* it needs (trust / quarantined /
instruction_flag) are in the Phase-0 DDL, and this contract fixes the write-time
trust interface so P1-P3 write paths call it from day one.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import SourceInfo

__all__ = ["TrustPolicy"]


class TrustOptions(PolicyOptions):
    default_trust: float = constants.TRUST_DEFAULT
    retrieved_content_cap: float = constants.TRUST_RETRIEVED_CAP
    corroborations_to_promote: int = constants.QUARANTINE_PROMOTION_CORROBORATIONS


class TrustPolicy(BindablePolicy):
    name: ClassVar[str] = "trust"
    Options: ClassVar[type[PolicyOptions]] = TrustOptions

    def trust_at_write(self, source: SourceInfo) -> float:
        raise NotImplementedError("Memory Firewall lands in Phase 4 (plan §5, E1)")
