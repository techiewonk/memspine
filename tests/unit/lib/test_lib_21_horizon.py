"""LIB-21: trust horizon on grants (G-07, N1 reach bound)."""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator

import pytest

from memspine.trust.horizon import (
    Grant,
    GrantError,
    TrustedRecord,
    chain_visible,
    deposit_trust,
    effective_trust,
    reach,
    reach_trust,
    regrant,
)


def g(a: str, b: str, h: int = 3, k: float = 0.5, scope: str = "s", purpose: str = "p") -> Grant:
    return Grant(a, b, scope, purpose, h, k)


REC = TrustedRecord("o", "s", 0.8)


def test_chain_attenuation_is_product() -> None:
    path = [g("o", "a", 2, 0.5), g("a", "b", 1, 0.5), g("b", "c", 0, 0.8)]
    assert effective_trust(REC, path) == pytest.approx(0.8 * 0.5 * 0.5 * 0.8)
    assert effective_trust(REC, []) == 0.8
    assert effective_trust(REC, path[:1]) == pytest.approx(0.4)


def test_beyond_horizon_not_visible() -> None:
    path = [g("o", "a", 1), g("a", "b", 5), g("b", "c", 5)]
    assert chain_visible(path[:2])
    assert effective_trust(REC, path) == 0.0
    assert effective_trust(REC, [g("o", "a", 0), g("a", "b", 0)]) == 0.0


def test_viewer_bound_scope_and_disconnected() -> None:
    p = [g("o", "a")]
    assert effective_trust(REC, p, viewer="a") > 0
    assert effective_trust(REC, p, viewer="z") == 0.0
    assert effective_trust(REC, [g("x", "a")]) == 0.0
    assert effective_trust(REC, [g("o", "a", scope="other")]) == 0.0
    assert effective_trust(REC, [g("o", "a"), g("zz", "b")]) == 0.0
    assert effective_trust(REC, [g("o", "a"), g("a", "b", purpose="q")]) == 0.0


def test_regrant_cannot_amplify_or_extend() -> None:
    parent = g("o", "a", 2, 0.5)
    child = regrant(parent, "b", horizon=1, kappa=1.0)
    assert (child.grantor, child.horizon) == ("a", 1)
    assert effective_trust(REC, [parent, child]) <= effective_trust(REC, [parent])
    with pytest.raises(GrantError):
        regrant(parent, "b", horizon=2, kappa=0.5)
    with pytest.raises(GrantError):
        regrant(g("o", "a", 0), "b", horizon=0, kappa=0.5)
    for bad in (0.0, 1.5, -0.1, float("nan")):
        with pytest.raises(GrantError):
            regrant(parent, "b", horizon=0, kappa=bad)
    with pytest.raises(GrantError):
        Grant("o", "a", "s", "p", -1, 0.5)


def test_overclaiming_regrant_is_clamped() -> None:
    path = [g("o", "a", 1), g("a", "b", 9), g("b", "c", 9)]
    assert not chain_visible(path)


def test_cycles_terminate() -> None:
    cyc = [g("a", "b", 50), g("b", "c", 50), g("c", "a", 50), g("a", "a", 50)]
    r = reach_trust(cyc, "a", 1000)
    assert set(r) == {"b", "c"}
    assert r["b"] == 0.5
    assert r["c"] == 0.25


def test_laundering_capped() -> None:
    poisoned = TrustedRecord("o", "s", 0.1)
    view = effective_trust(poisoned, [g("o", "a", 2, 0.9)])
    assert deposit_trust(1.0, [view]) <= view <= 0.1
    clean = effective_trust(TrustedRecord("o", "s", 0.9), [g("o", "a", 2, 0.9)])
    assert deposit_trust(0.95, [view, clean]) == min(view, 0.95)
    b_view = effective_trust(poisoned, [g("o", "a", 2, 0.9), g("a", "b", 1, 0.9)])
    assert deposit_trust(1.0, [b_view]) <= 0.1 * 0.81 + 1e-12
    assert deposit_trust(0.7, []) == 0.7
    assert deposit_trust(0.7, [0.0]) == 0.0
    with pytest.raises(GrantError):
        deposit_trust(1.2, [])


def _walks(grants: list[Grant], source: str, max_len: int) -> Iterator[list[Grant]]:
    stack = [[e] for e in grants if e.grantor == source and max_len >= 1]
    while stack:
        w = stack.pop()
        yield w
        if len(w) < max_len:
            stack.extend([*w, e] for e in grants if e.grantor == w[-1].grantee)


def _distances(grants: list[Grant], source: str) -> dict[str, int]:
    dist = {source: 0}
    frontier = [source]
    while frontier:
        nxt: list[str] = []
        for x in frontier:
            for e in grants:
                if e.grantor == x and e.grantee not in dist:
                    dist[e.grantee] = dist[x] + 1
                    nxt.append(e.grantee)
        frontier = nxt
    return dist


@pytest.mark.parametrize("seed", range(40))
def test_reach_bound_property(seed: int) -> None:
    rnd = random.Random(seed)
    agents = [f"a{i}" for i in range(rnd.randint(2, 6))]
    grants = [
        Grant(x, y, "s", "p", rnd.randint(0, 3), rnd.choice([0.2, 0.5, 0.9, 1.0]))
        for x, y in itertools.product(agents, agents)
        if rnd.random() < 0.45
    ]
    h, t0, src = rnd.randint(0, 4), 0.1, agents[0]
    got = reach_trust(grants, src, h, t0)
    brute: dict[str, float] = {}
    rec = TrustedRecord(src, "s", t0)
    for w in _walks(grants, src, h):
        v = effective_trust(rec, w)
        if v > 0 and w[-1].grantee != src:
            brute[w[-1].grantee] = max(brute.get(w[-1].grantee, 0.0), v)
    assert got == pytest.approx(brute)
    assert reach(grants, src, h) == set(brute)
    dist = _distances(grants, src)
    for agent, bound in got.items():
        assert bound <= t0 + 1e-12
        assert dist[agent] <= h


def test_determinism() -> None:
    rnd = random.Random(7)
    grants = [
        Grant(f"a{i}", f"a{j}", "s", "p", 2, 0.7)
        for i in range(5)
        for j in range(5)
        if rnd.random() < 0.5
    ]
    first = reach_trust(grants, "a0", 3, 0.4)
    for _ in range(3):
        assert reach_trust(list(reversed(grants)), "a0", 3, 0.4) == first
    assert reach(grants, "a0", 0) == set()
