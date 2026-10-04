"""Trust-horizon invariant (THI, formerly "MTI") — pure trust arithmetic for shared memory.

No I/O: the engine supplies records and grant scopes; this module only decides
numbers. Keeping it pure is what lets the invariant be property-tested
independently of storage (Paper A, Prop. 1).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from memspine.config.schema import IntegrityConfig

__all__ = ["IntegrityPolicy"]


@dataclass(frozen=True)
class IntegrityPolicy:
    enabled: bool = False
    attenuation: str = "product"
    kappa: float = 0.5
    edge_kappa: dict[str, float] = field(default_factory=dict)
    derivation_decay: float = 1.0
    verification_bonus: float = 1.0
    admission_threshold: float = 0.0
    trust_weighted_ranking: bool = True
    principal_bound_corroboration: bool = True
    merge_reinforcement_gate: bool = True
    implicit_parents: str = "off"
    untrusted_wrap_below: float = 0.0
    claims_only_below: float = 0.0
    live_reevaluation: bool = False
    principal_reputation: bool = False

    @classmethod
    def from_config(cls, config: IntegrityConfig) -> IntegrityPolicy:
        return cls(**config.model_dump())

    def kappa_for(self, grantor: str, grantee: str) -> float:
        return self.edge_kappa.get(f"{grantor}->{grantee}", self.kappa)

    def view_trust(self, trust: float, grantor: str, grantee: str) -> float:
        """Trust of a record in ``grantor`` as seen by ``grantee`` (Def. 2)."""
        if grantor == grantee:
            return trust
        kappa = self.kappa_for(grantor, grantee)
        return trust * kappa if self.attenuation == "product" else min(trust, kappa)

    def deposit_trust(self, base: float, parent_views: Iterable[float]) -> float:
        """MTI-D: never more trusted than the least-trusted parent, then decayed.

        ``verification_bonus`` > 1 deliberately violates the invariant (baseline
        emulation); the result is clipped to [0, 1] like any trust value.
        """
        views = list(parent_views)
        if not views:
            return base
        derived = min(base, *views) * self.derivation_decay * self.verification_bonus
        return min(1.0, derived)

    def admits(self, view_trust: float) -> bool:
        return view_trust >= self.admission_threshold

    def ranked(self, composite: float, view_trust: float) -> float:
        return composite * view_trust if self.trust_weighted_ranking else composite
