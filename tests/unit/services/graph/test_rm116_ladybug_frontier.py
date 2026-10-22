"""RM-116: LadybugDB ``frontier_edges`` (per-node primary-key queries for small frontiers,
an anchored UNION ALL above) returns exactly the edges of the old single ``s IN .. OR
d IN ..`` query: same set, each edge once, under namespace and relation filters."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("ladybug")

from memspine.clients.ladybug import LadybugClient, LadybugSpec
from memspine.services.graph import ladybug as lb
from memspine.services.graph.ladybug import LadybugGraphStore

NAMESPACES = ("ns/a", "ns/b")
RELS = ("knows", "employs", "mentions")


@pytest.fixture
async def store() -> AsyncIterator[LadybugGraphStore]:
    client = LadybugClient(LadybugSpec(":memory:"))
    await client.open()
    graph = LadybugGraphStore(client)
    rng = random.Random(116)
    for _ in range(300):
        a = rng.randrange(3) if rng.random() < 0.3 else rng.randrange(40)  # a few hubs
        b = a if rng.random() < 0.03 else rng.randrange(40)  # some self-loops
        props = {"weight": rng.choice((0.0, 0.5, 1.0))}  # weight 0 = tombstone
        await graph.upsert_edge(
            f"n{a}", f"n{b}", rng.choice(RELS), props, namespace=rng.choice(NAMESPACES)
        )
    yield graph
    await client.close()


async def _legacy(
    graph: LadybugGraphStore, frontier: list[str], ns: str | None, rel: str | None
) -> list[tuple[str, str, str]]:
    params: dict[str, Any] = {"frontier": frontier}
    where = " AND ".join(lb._filters("e", ns, rel, params))
    rows = await graph._execute(
        f"{lb._EDGE_MATCH} WHERE (s.node_id IN $frontier OR d.node_id IN $frontier) "
        f"AND {where} {lb._EDGE_RETURN}",
        params,
    )
    return sorted((str(r[0]), str(r[1]), str(r[2])) for r in rows)


@pytest.mark.parametrize("per_node_max", [0, 3, 100], ids=["union", "default", "per_node"])
async def test_frontier_edges_equal_the_or_query(
    store: LadybugGraphStore, per_node_max: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lb, "LADYBUG_FRONTIER_PER_NODE_MAX", per_node_max)
    rng = random.Random(per_node_max)
    checked = 0
    for size in (1, 2, 3, 4, 6, 9):
        for ns in (None, *NAMESPACES):
            for rel in (None, "knows"):
                frontier = sorted({f"n{rng.randrange(40)}" for _ in range(size)} | {"n0"})
                got = await store.frontier_edges(frontier, namespace=ns, rel_type=rel)
                keys = sorted((e.src, e.dst, e.rel_type) for e in got)
                assert keys == await _legacy(store, frontier, ns, rel)
                assert len(keys) == len(set(keys))  # each edge once
                assert all(e.weight > 0 for e in got)
                checked += bool(keys)
    assert checked > 0
