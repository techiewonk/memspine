"""Trust horizon on grants: the N1 cross-agent reach bound (LIB-21, G-07).

Pure and deterministic: no I/O, no model calls, no engine imports. Not wired yet (T5-01 wires it).

Semantics (kept from ``core.integrity``: a record seen across a grant has its trust multiplied by
the grant's attenuation, ``view_trust = trust * kappa``; composed along a chain it is the product):

* A :class:`Grant` lets ``grantee`` read ``grantor``'s memory ``scope`` for ``purpose``. ``horizon``
  ``h >= 0`` is the number of *further* hops the data may travel beyond the grantee (``h = 0``: the
  grantee may read but not re-grant). ``kappa`` in ``(0, 1]`` is the attenuation.
* Re-grant (:func:`regrant`) is only by the grantee, within the same scope and purpose, with a
  horizon at most ``parent.horizon - 1``. Because every ``kappa <= 1``, effective trust along a
  chain is the product and can only fall: no amplification, no extension.
* A chain ``g1..gn`` is *visible* iff it is connected (``g(i+1).grantor == g(i).grantee``), shares
  scope and purpose, and the remaining horizon ``rem1 = h1``, ``rem(i) = min(h(i), rem(i-1) - 1)``
  never goes negative (so ``n <= h1 + 1``). A chain that over-claims is clamped, not trusted.
* :func:`effective_trust` = record trust * product of kappa, or ``0.0`` when not visible. Numeric
  trust is never access control (POL-2); callers must still apply visibility. ER-29: pass ``viewer``
  to bind the result to the principal at the end of the chain.
* :func:`deposit_trust` is the POL-2 cap: a deposit derived from read records is never more trusted
  than the least-trusted parent view, so laundering through another agent cannot raise trust.
* :func:`reach_trust` / :func:`reach` bound what a deposit from ``source`` can influence: agents
  within ``horizon`` hops along visible chains, at attenuated trust ``<= t0 * prod(kappa)``. Cycles
  terminate because depth is capped and only non-dominated states are expanded.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "Grant",
    "GrantError",
    "TrustedRecord",
    "chain_attenuation",
    "chain_visible",
    "deposit_trust",
    "effective_trust",
    "reach",
    "reach_trust",
    "regrant",
]


class GrantError(ValueError):
    """An invalid grant or an attempted amplification / horizon extension."""


def _check_unit(name: str, value: float, *, open_low: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GrantError(f"{name} must be a finite number, got {value!r}")
    if (value <= 0.0 if open_low else value < 0.0) or value > 1.0:
        bound = "(0, 1]" if open_low else "[0, 1]"
        raise GrantError(f"{name} must be in {bound}, got {value!r}")


def _check_horizon(horizon: int) -> None:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 0:
        raise GrantError(f"horizon must be an int >= 0, got {horizon!r}")


@dataclass(frozen=True)
class Grant:
    grantor: str
    grantee: str
    scope: str
    purpose: str
    horizon: int
    kappa: float

    def __post_init__(self) -> None:
        _check_horizon(self.horizon)
        _check_unit("kappa", self.kappa, open_low=True)


@dataclass(frozen=True)
class TrustedRecord:
    """The minimum a record exposes here: owning agent, scope and trust tau."""

    owner: str
    scope: str
    trust: float

    def __post_init__(self) -> None:
        _check_unit("trust", self.trust, open_low=False)


def regrant(parent: Grant, grantee: str, *, horizon: int, kappa: float) -> Grant:
    """Re-grant ``parent`` onward. Raises :class:`GrantError` on any extension of reach."""
    if parent.horizon < 1:
        raise GrantError("grant has horizon 0: it cannot be re-granted")
    if horizon > parent.horizon - 1:
        raise GrantError(
            f"re-grant horizon {horizon} exceeds parent horizon - 1 = {parent.horizon - 1}"
        )
    return Grant(parent.grantee, grantee, parent.scope, parent.purpose, horizon, kappa)


def chain_visible(path: Sequence[Grant]) -> bool:
    """True iff the chain is connected, uniform in scope and purpose, and within horizon."""
    if not path:
        return True
    first = path[0]
    rem = first.horizon
    for i, grant in enumerate(path):
        if i:
            prev = path[i - 1]
            if (
                grant.grantor != prev.grantee
                or grant.scope != first.scope
                or grant.purpose != first.purpose
            ):
                return False
            rem = min(grant.horizon, rem - 1)
        if rem < 0:
            return False
    return True


def chain_attenuation(path: Sequence[Grant]) -> float:
    """Product of kappa along the chain (1.0 for the empty chain, 0.0 if not visible)."""
    if not chain_visible(path):
        return 0.0
    out = 1.0
    for grant in path:
        out *= grant.kappa
    return out


def effective_trust(
    record: TrustedRecord, path_of_grants: Sequence[Grant], *, viewer: str | None = None
) -> float:
    """Trust of ``record`` as seen at the end of a grant chain; ``0.0`` when not visible."""
    if not path_of_grants:
        return record.trust if viewer in (None, record.owner) else 0.0
    first, last = path_of_grants[0], path_of_grants[-1]
    if first.grantor != record.owner or first.scope != record.scope:
        return 0.0
    if viewer is not None and viewer != last.grantee:
        return 0.0
    return record.trust * chain_attenuation(path_of_grants)


def deposit_trust(base: float, parent_views: Iterable[float]) -> float:
    """POL-2 cap: ``min(base, *parent effective trusts)``; no parents -> ``base``."""
    _check_unit("base", base, open_low=False)
    views = list(parent_views)
    for view in views:
        _check_unit("parent view", view, open_low=False)
    return min(base, *views) if views else base


def reach_trust(
    grants: Iterable[Grant], source: str, horizon: int, trust: float = 1.0
) -> dict[str, float]:
    """Agents a deposit of ``trust`` from ``source`` can influence, mapped to the best trust bound.

    At most ``horizon`` hops; each chain obeys :func:`chain_visible`. The source is excluded.
    """
    _check_unit("trust", trust, open_low=False)
    _check_horizon(horizon)
    groups: dict[tuple[str, str], list[Grant]] = {}
    for grant in sorted(
        set(grants),
        key=lambda x: (x.scope, x.purpose, x.grantor, x.grantee, x.horizon, x.kappa),
    ):
        groups.setdefault((grant.scope, grant.purpose), []).append(grant)
    best: dict[str, float] = {}
    for _, group in sorted(groups.items()):
        out_edges: dict[str, list[Grant]] = {}
        for grant in group:
            out_edges.setdefault(grant.grantor, []).append(grant)
        # frontier entries: (agent, remaining horizon, attenuation factor)
        frontier = [(e.grantee, e.horizon, e.kappa) for e in out_edges.get(source, [])]
        seen: dict[str, list[tuple[int, float]]] = {}
        depth = 1
        while frontier and depth <= horizon:
            nxt: list[tuple[str, int, float]] = []
            for agent, rem, fac in frontier:
                states = seen.setdefault(agent, [])
                if any(r >= rem and f >= fac for r, f in states):
                    continue
                states[:] = [(r, f) for r, f in states if not (rem >= r and fac >= f)]
                states.append((rem, fac))
                if agent != source:
                    best[agent] = max(best.get(agent, 0.0), fac)
                if rem >= 1:
                    nxt.extend(
                        (e.grantee, min(e.horizon, rem - 1), fac * e.kappa)
                        for e in out_edges.get(agent, [])
                    )
            frontier = nxt
            depth += 1
    return {agent: trust * fac for agent, fac in sorted(best.items())}


def reach(grants: Iterable[Grant], source: str, horizon: int) -> set[str]:
    """The set of agents a deposit from ``source`` can reach within ``horizon`` hops."""
    return set(reach_trust(grants, source, horizon))
