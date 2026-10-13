"""Trust-horizon invariant (THI, formerly "MTI") — pure trust arithmetic for shared memory.

No I/O: the engine supplies records and grant scopes; this module only decides
numbers. Keeping it pure is what lets the invariant be property-tested
independently of storage (Paper A, Prop. 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from memspine.config.schema import IntegrityConfig
from memspine.trust import horizon as _horizon

__all__ = ["IntegrityPolicy"]


#: ``integrity.grant_horizon`` None: no explicit hop cap (the library needs an int).
_UNBOUNDED_HORIZON = 1_000_000


def _unit(value: float) -> float:
    """Clamp to [0, 1]: the library rejects anything else, the legacy arithmetic never did."""
    return min(1.0, max(0.0, value))


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
    corroboration_roots: bool = False
    merge_reinforcement_gate: bool = True
    implicit_parents: str = "off"
    untrusted_wrap_below: float = 0.0
    claims_only_below: float = 0.0
    live_reevaluation: bool = False
    principal_reputation: bool = False
    horizon_v2: bool = False
    grant_horizon: int | None = None

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
        if self.horizon_v2:  # T5-01: one grant edge through the LIB-21 library
            return _horizon.effective_trust(
                _horizon.TrustedRecord(owner=grantor, scope="*", trust=_unit(trust)),
                [self.grant(grantor, grantee)],
                viewer=grantee,
            )
        return trust * kappa if self.attenuation == "product" else min(trust, kappa)

    def grant(self, grantor: str, grantee: str) -> _horizon.Grant:
        """The LIB-21 grant for the edge ``grantor -> grantee`` (T5-01)."""
        horizon = self.grant_horizon if self.grant_horizon is not None else _UNBOUNDED_HORIZON
        return _horizon.Grant(grantor, grantee, "*", "*", horizon, self.kappa_for(grantor, grantee))

    def chain_view(self, trust: float, path: Sequence[_horizon.Grant]) -> float:
        """Trust of a record seen across a whole grant chain: ``trust * prod(kappa)``, or 0.0
        when the chain is not visible (disconnected or past its horizon). T5-01."""
        if not path:
            return trust
        return trust * _horizon.chain_attenuation(path)

    def deposit_trust(self, base: float, parent_views: Iterable[float]) -> float:
        """MTI-D: never more trusted than the least-trusted parent, then decayed.

        ``verification_bonus`` > 1 deliberately violates the invariant (baseline
        emulation); the result is clipped to [0, 1] like any trust value.
        """
        views = list(parent_views)
        if not views:
            return base
        capped = (
            _horizon.deposit_trust(_unit(base), [_unit(v) for v in views])
            if self.horizon_v2
            else min(base, *views)
        )
        derived = capped * self.derivation_decay * self.verification_bonus
        return min(1.0, derived)

    def admits(self, view_trust: float) -> bool:
        return view_trust >= self.admission_threshold

    def ranked(self, composite: float, view_trust: float) -> float:
        return composite * view_trust if self.trust_weighted_ranking else composite
