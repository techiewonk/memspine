"""RM-96: hub fan-out caps of the qualifier-filtered walk (levels >= 2 only).

``qualified_hops`` expands at most ``QUALIFIED_WALK_FRONTIER_CAP`` frontier nodes per level
>= 2 (strongest admitted edge into the node first, then id), each following at most
``QUALIFIED_WALK_DEEP_MAX_DEGREE`` neighbours. Proved against the uncapped reference walk:
depth 1 is identical on every graph (hub or not, any caller ``max_degree``); lifting both
caps gives the reference at every depth; a walk whose frontiers and degrees stay under the
caps is unchanged; on a hub the level-1 set is unchanged, deeper levels are a subset and
each level >= 2 fetches the edges of at most the cap's frontier nodes.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest

from memspine.config.constants import QUALIFIED_WALK_DEEP_MAX_DEGREE, QUALIFIED_WALK_FRONTIER_CAP
from memspine.services.graph.base import GraphEdge, capped_neighbors
from memspine.services.graph.qualifiers import (
    AssertionModality,
    EdgeQualifiers,
    QualifierFilter,
    qualified_hops,
    with_qualifiers,
)

FILTER = QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED}))


def _graph(rng: random.Random, nodes: int, edges: int, hub_share: float) -> list[GraphEdge]:
    hubs = max(1, nodes // 100)
    out: dict[tuple[str, str], GraphEdge] = {}

    def pick() -> int:
        return rng.randrange(hubs) if rng.random() < hub_share else rng.randrange(nodes)

    while len(out) < edges:
        a, b = pick(), pick()
        if a == b:
            continue
        modality = (
            AssertionModality.ASSERTED if rng.random() < 0.8 else AssertionModality.HYPOTHETICAL
        )
        props = with_qualifiers(
            {"weight": round(0.1 + rng.random() * 0.9, 3)},
            EdgeQualifiers(assertion_modality=modality),
        )
        out[(f"n{a:04d}", f"n{b:04d}")] = GraphEdge(f"n{a:04d}", f"n{b:04d}", "rel", props)
    return list(out.values())


class _Fetch:
    def __init__(self, edges: list[GraphEdge]) -> None:
        self.edges = edges
        self.frontiers: list[int] = []

    async def __call__(self, frontier: Sequence[str]) -> list[GraphEdge]:
        self.frontiers.append(len(frontier))
        wanted = set(frontier)
        return [e for e in self.edges if e.weight > 0 and (e.src in wanted or e.dst in wanted)]


async def _reference(
    fetch: _Fetch, seeds: Sequence[str], depth: int, max_degree: int | None
) -> dict[str, int]:
    """The walk before RM-96 (no level caps)."""
    hops = {seed: 0 for seed in seeds}
    frontier = list(dict.fromkeys(seeds))
    for level in range(1, depth + 1):
        if not frontier:
            break
        edges = [e for e in await fetch(frontier) if FILTER.admits(e)]
        nxt: list[str] = []
        for node in frontier:
            for other in capped_neighbors(edges, node, max_degree):
                if other not in hops:
                    hops[other] = level
                    nxt.append(other)
        frontier = nxt
    return hops


def _level(hops: dict[str, int], level: int) -> set[str]:
    return {n for n, h in hops.items() if h == level}


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("max_degree", [None, 3])
async def test_depth_one_unchanged_and_uncapped_is_reference(
    seed: int, max_degree: int | None
) -> None:
    rng = random.Random(seed)
    edges = _graph(rng, 400, 3000, 0.3)
    hub, plain = "n0000", f"n{rng.randrange(10, 400):04d}"
    for start in ([hub], [plain], [hub, plain]):
        ref1 = await _reference(_Fetch(edges), start, 1, max_degree)
        got1 = await qualified_hops(_Fetch(edges), start, 1, FILTER, max_degree)
        assert got1 == ref1  # one-hop: never capped beyond the caller's max_degree
        for depth in (2, 3):
            ref = await _reference(_Fetch(edges), start, depth, max_degree)
            lifted = await qualified_hops(
                _Fetch(edges),
                start,
                depth,
                FILTER,
                max_degree,
                frontier_cap=None,
                deep_max_degree=None,
            )
            assert lifted == ref
            fetch = _Fetch(edges)
            capped = await qualified_hops(fetch, start, depth, FILTER, max_degree)
            assert _level(capped, 1) == _level(ref, 1)
            assert set(capped) <= set(ref)
            assert all(n <= QUALIFIED_WALK_FRONTIER_CAP for n in fetch.frontiers[1:])


async def test_small_walks_are_unchanged_by_the_caps() -> None:
    """Frontiers and degrees under the caps: the capped walk is the reference walk."""
    rng = random.Random(7)
    edges = _graph(rng, 2000, 2500, 0.0)  # sparse, no hubs: degrees ~2.5
    for _ in range(20):
        start = [f"n{rng.randrange(2000):04d}"]
        ref = await _reference(_Fetch(edges), start, 2, None)
        fetch = _Fetch(edges)
        got = await qualified_hops(fetch, start, 2, FILTER, None)
        if max(fetch.frontiers) <= QUALIFIED_WALK_FRONTIER_CAP:
            assert got == ref


async def test_hub_depth_two_is_bounded_strongest_first() -> None:
    rng = random.Random(11)
    edges = _graph(rng, 1000, 12000, 0.3)
    hub = "n0000"
    ref = await _reference(_Fetch(edges), [hub], 2, None)
    fetch = _Fetch(edges)
    got = await qualified_hops(fetch, [hub], 2, FILTER, None)
    assert len(_level(ref, 1)) > QUALIFIED_WALK_FRONTIER_CAP  # the hub overflows the cap
    assert fetch.frontiers == [1, QUALIFIED_WALK_FRONTIER_CAP]
    assert _level(got, 1) == _level(ref, 1)
    assert len(_level(got, 2)) <= QUALIFIED_WALK_FRONTIER_CAP * QUALIFIED_WALK_DEEP_MAX_DEGREE
    assert _level(got, 2) < _level(ref, 2)
    # the expanded frontier is the hub's strongest admitted neighbours
    admitted = [e for e in edges if FILTER.admits(e) and hub in (e.src, e.dst)]
    strongest = capped_neighbors(admitted, hub, QUALIFIED_WALK_FRONTIER_CAP)
    reach: set[str] = set()
    for node in strongest:
        reach |= {e.dst if e.src == node else e.src for e in edges if node in (e.src, e.dst)}
    assert _level(got, 2) <= reach
