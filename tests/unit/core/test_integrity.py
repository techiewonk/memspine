"""MTI trust arithmetic (core/integrity.py) — unit + seeded property tests.

The property test replays Paper A's Prop. 1 over random grant graphs and random
deposit trajectories using only the policy's arithmetic: every content-tainted
record must satisfy tau <= c * kbar ** d(o, ns). Stdlib ``random`` with fixed
seeds keeps it deterministic without a new dependency.
"""

from __future__ import annotations

import random
from collections import deque

import pytest

from memspine.config.schema import IntegrityConfig
from memspine.core.integrity import IntegrityPolicy
from memspine.exceptions import ConfigError


def test_defaults_are_off_and_product() -> None:
    policy = IntegrityPolicy.from_config(IntegrityConfig())
    assert not policy.enabled
    assert policy.attenuation == "product" and policy.kappa == 0.5


def test_view_trust_product_min_and_edge_override() -> None:
    policy = IntegrityPolicy(enabled=True, kappa=0.5, edge_kappa={"a->b": 0.8})
    assert policy.view_trust(0.6, "a", "a") == 0.6  # own namespace: no attenuation
    assert policy.view_trust(0.6, "a", "b") == pytest.approx(0.48)  # edge override
    assert policy.view_trust(0.6, "c", "b") == pytest.approx(0.3)
    as_min = IntegrityPolicy(enabled=True, attenuation="min", kappa=0.3)
    assert as_min.view_trust(0.9, "a", "b") == 0.3


def test_deposit_trust_is_min_over_parents_then_decayed() -> None:
    policy = IntegrityPolicy(enabled=True, derivation_decay=0.9)
    assert policy.deposit_trust(0.5, []) == 0.5  # no parents: base trust, no decay
    assert policy.deposit_trust(0.5, [0.9, 0.2]) == pytest.approx(0.18)
    assert policy.deposit_trust(0.3, [0.9]) == pytest.approx(0.27)


def test_verification_bonus_breaks_the_ceiling_and_clips() -> None:
    policy = IntegrityPolicy(enabled=True, verification_bonus=1.5)
    assert policy.deposit_trust(0.5, [0.4]) == pytest.approx(0.6)  # above the parent: g > 1
    assert policy.deposit_trust(0.9, [0.9]) == 1.0  # clipped
    assert IntegrityPolicy(enabled=True).deposit_trust(0.5, [0.4]) == 0.4


def test_admission_and_ranking() -> None:
    policy = IntegrityPolicy(enabled=True, admission_threshold=0.2)
    assert policy.admits(0.2) and not policy.admits(0.19)
    assert policy.ranked(1.2, 0.5) == pytest.approx(0.6)
    blind = IntegrityPolicy(enabled=True, trust_weighted_ranking=False)
    assert blind.ranked(1.2, 0.5) == 1.2


@pytest.mark.parametrize(
    "config",
    [
        {"attenuation": "sum"},
        {"edge_kappa": {"ab": 0.5}},
        {"edge_kappa": {"a->b": 1.5}},
    ],
)
def test_config_rejects_bad_values(config: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        IntegrityConfig(**config)  # type: ignore[arg-type]


def _distances(n: int, edges: set[tuple[int, int]], origin: int) -> dict[int, int]:
    dist = {origin: 0}
    queue = deque([origin])
    while queue:
        node = queue.popleft()
        for src, dst in edges:
            if src == node and dst not in dist:
                dist[dst] = dist[node] + 1
                queue.append(dst)
    return dist


def test_trust_ceiling_holds_on_random_trajectories() -> None:
    """Prop. 1 over 2,000 random (graph, trajectory) pairs — zero violations."""
    violations = 0
    cases = 0
    for seed in range(2_000):
        rng = random.Random(seed)
        n = rng.randint(2, 6)
        # edge (j, i): agent i may read namespace j; content flows j -> i
        edges = {(j, i) for j in range(n) for i in range(n) if i != j and rng.random() < 0.4}
        kappas = {edge: rng.uniform(0.1, 1.0) for edge in edges}
        kbar = max(kappas.values(), default=1.0)
        policy = IntegrityPolicy(
            enabled=True,
            kappa=0.5,
            edge_kappa={f"{j}->{i}": k for (j, i), k in kappas.items()},
        )
        origin = 0
        c = rng.uniform(0.05, 0.9)
        # records: (namespace, trust, tainted)
        records: list[tuple[int, float, bool]] = [(origin, c, True)]
        for _ in range(rng.randint(1, 25)):
            writer = rng.randrange(n)
            readable = [
                idx
                for idx, (ns, _, _) in enumerate(records)
                if ns == writer or (ns, writer) in edges
            ]
            parents = rng.sample(readable, k=min(len(readable), rng.randint(0, 3)))
            views = [
                policy.view_trust(records[p][1], str(records[p][0]), str(writer)) for p in parents
            ]
            base = rng.choice([0.9, 0.7, 0.5, 0.4, 0.3])
            tainted = any(records[p][2] for p in parents)
            records.append((writer, policy.deposit_trust(base, views), tainted))
        dist = _distances(n, edges, origin)
        for ns, trust, tainted in records:
            if not tainted:
                continue
            cases += 1
            if trust > c * kbar ** dist[ns] + 1e-12:
                violations += 1
    assert cases > 5_000
    assert violations == 0
