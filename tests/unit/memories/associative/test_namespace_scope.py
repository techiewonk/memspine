"""KB-1: PPR and BFS in ``related`` read only the seed namespace's edges."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from memspine.core.events import MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.base import GraphEdge, GraphNode


class SpyGraph:
    def __init__(self) -> None:
        self.edge_list_calls: list[str | None] = []
        self.neighbor_calls: list[dict[str, object]] = []

    async def edge_list(self, namespace: str | None = None) -> list[GraphEdge]:
        self.edge_list_calls.append(namespace)
        return [GraphEdge("seed", "other", "related", {"weight": 1.0})]

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        self.neighbor_calls.append({"namespace": namespace, "max_degree": max_degree})
        return [GraphNode("other")]

    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None: ...


class Storage:
    def __init__(self) -> None:
        self.records = {
            rid: MemoryRecord(record_id=rid, namespace="ns/a", memory_type="episodic", content=rid)
            for rid in ("seed", "other")
        }

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)


async def _noop(_event: MemoryEvent) -> None:
    return None


async def test_ppr_and_bfs_are_scoped_to_the_seed_namespace() -> None:
    graph = SpyGraph()
    memory = AssociativeMemory(
        Storage(),
        graph,  # type: ignore[arg-type]
        _noop,
        policies={"related": {"max_degree": 7}},
    )
    assert [r.record_id for r in await memory.related("ns/a", "seed")] == ["other"]
    assert graph.edge_list_calls == ["ns/a"]
    await memory.related("ns/a", "seed", strategy="bfs")
    assert graph.neighbor_calls == [{"namespace": "ns/a", "max_degree": 7}]
