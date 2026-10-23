"""F-16e: the Neo4j graph adapter as the engine's one graph client (``storage.graph``).

The outbox-fed Neo4j mirror (``storage.graph_mirror``) was removed by the ADR-081 amendment of
2026-10-23: one graph client runs at a time. The file keeps its name because other suites
import its ``FakeNeo4j`` and ``live_executor``. Model-free.

The adapter contract runs against an in-memory MERGE recorder (``FakeNeo4j``) and, when the
``neo4j`` driver is importable and ``MEMSPINE_TEST_NEO4J_URL`` is set (plus optional
``MEMSPINE_TEST_NEO4J_USER`` / ``MEMSPINE_TEST_NEO4J_PASSWORD``), against a live server under a
private label that is cleared afterwards. Otherwise the live variants skip with a reason.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import StorageConfig
from memspine.config.schema.observability import TapConfig
from memspine.exceptions import ConfigError
from memspine.observability import tap
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import get_registry
from memspine.services.graph.base import GraphStore
from memspine.services.graph.neo4j import CYPHER, Neo4jExecutor, Neo4jGraphStore

NS = "acme/alice"
NS2 = "acme/bram"
COMPILED = "edge_compiler_v2"
T0 = datetime(2026, 10, 13, tzinfo=UTC)

LIVE_URL = os.environ.get("MEMSPINE_TEST_NEO4J_URL")
HAVE_DRIVER = importlib.util.find_spec("neo4j") is not None
LIVE_SKIP = "needs the neo4j driver and MEMSPINE_TEST_NEO4J_URL (a live server)"


@pytest.fixture(autouse=True)
def _tap_reset():
    tap.shutdown()
    yield
    tap.shutdown()


class Crash(BaseException):
    """A hard stop of the process: not an Exception, so the runner does not turn it into a hole."""


class FakeNeo4j:
    """An in-memory MERGE recorder that implements every op of :data:`CYPHER` by name."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.merges = 0
        self.fail_at: dict[int, BaseException] = {}  # n-th mutating call -> raise
        self._mutations = 0
        self.closed = False

    # -- the port ------------------------------------------------------------------------
    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        assert op in CYPHER, op
        self.calls.append(op)
        if op in {"upsert_node", "upsert_edge", "delete_node", "drop_bare", "prune_dead", "clear"}:
            self._mutations += 1
            if self._mutations in self.fail_at:
                raise self.fail_at.pop(self._mutations)
        return getattr(self, f"_{op}")(params)

    async def close(self) -> None:
        self.closed = True

    # -- ops -----------------------------------------------------------------------------
    def _schema(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        return []

    def _upsert_node(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.merges += 1
        self.nodes[p["key"]] = {
            "key": p["key"],
            "id": p["id"],
            "ns": p["ns"],
            "labels": list(p["labels"]),
            "props": p["props"],
            "compiled": p["compiled"],
            "bare": False,
        }
        return []

    def _bare(self, key: str, node_id: str, ns: str) -> None:
        if key not in self.nodes:
            self.nodes[key] = {
                "key": key,
                "id": node_id,
                "ns": ns,
                "labels": [],
                "props": "{}",
                "compiled": "",
                "bare": True,
            }

    def _upsert_edge(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.merges += 1
        self._bare(p["skey"], p["src"], p["ns"])
        self._bare(p["dkey"], p["dst"], p["ns"])
        self.edges[p["ekey"]] = {
            "a": p["skey"],
            "b": p["dkey"],
            "rel": p["rel"],
            "ns": p["ns"],
            "weight": p["weight"],
            "props": p["props"],
        }
        return []

    def _drop(self, keys: set[str]) -> None:
        for key in keys:
            self.nodes.pop(key, None)
        self.edges = {
            k: e for k, e in self.edges.items() if e["a"] not in keys and e["b"] not in keys
        }

    def _touching(self, key: str) -> set[str]:
        out = set()
        for e in self.edges.values():
            if e["a"] == key:
                out.add(e["b"])
            elif e["b"] == key:
                out.add(e["a"])
        return out

    def _bare_neighbours(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        victims = {k for k, n in self.nodes.items() if n["id"] == p["id"]}
        found = {
            self.nodes[m]["id"]
            for k in victims
            for m in self._touching(k)
            if self.nodes[m]["bare"] and m not in victims
        }
        return [{"id": i} for i in sorted(found)]

    def _delete_node(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._drop({k for k, n in self.nodes.items() if n["id"] == p["id"]})
        return []

    def _drop_bare(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._drop(
            {
                k
                for k, n in self.nodes.items()
                if n["id"] in p["ids"] and n["bare"] and not self._touching(k)
            }
        )
        return []

    def _prune_dead(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        dead = {
            k
            for k, n in self.nodes.items()
            if n["id"] in p["ids"]
            and n["compiled"] == p["compiled"]
            and not any(e["weight"] > 0 for e in self.edges.values() if k in (e["a"], e["b"]))
        }
        self._drop(dead)
        return []

    def _row(self, e: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "src": self.nodes[e["a"]]["id"],
            "dst": self.nodes[e["b"]]["id"],
            "rel": e["rel"],
            "ns": e["ns"],
            "props": e["props"],
        }

    def _edges_of(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        ids = set(p["ids"])
        return [
            self._row(e)
            for e in self.edges.values()
            if self.nodes[e["a"]]["id"] in ids or self.nodes[e["b"]]["id"] in ids
        ]

    def _frontier_edges(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        ids = set(p["ids"])
        return [
            self._row(e)
            for e in self.edges.values()
            if (self.nodes[e["a"]]["id"] in ids or self.nodes[e["b"]]["id"] in ids)
            and e["weight"] > 0
            and (p["ns"] is None or e["ns"] == p["ns"])
            and (p["rel"] is None or e["rel"] == p["rel"])
        ]

    def _edge_list(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [self._row(e) for e in self.edges.values() if p["ns"] is None or e["ns"] == p["ns"]]

    def _nodes(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {"id": n["id"], "labels": n["labels"], "props": n["props"], "ns": n["ns"]}
            for n in self.nodes.values()
            if n["id"] in p["ids"]
        ]

    def _reach(self, p: Mapping[str, Any], use_rel: bool) -> dict[str, int]:
        """What ``walk_nodes`` / ``walk_edges`` compute: shortest hop count to every node id
        within ``depth`` hops over live (weight > 0) edges, undirected, optional ns / rel filter."""
        dist = {s: 0 for s in p["seeds"]}
        frontier = list(p["seeds"])
        for hop in range(1, int(p["depth"]) + 1):
            nxt: list[str] = []
            for e in self.edges.values():
                if e["weight"] <= 0 or (p["ns"] is not None and e["ns"] != p["ns"]):
                    continue
                if use_rel and p["rel"] is not None and e["rel"] != p["rel"]:
                    continue
                a, b = self.nodes[e["a"]]["id"], self.nodes[e["b"]]["id"]
                for u, v in ((a, b), (b, a)):
                    if u in frontier and v not in dist:
                        dist[v] = hop
                        nxt.append(v)
            frontier = nxt
        return dist

    def _walk_nodes(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        dist = self._reach(p, True)
        return [
            {"id": n["id"], "dist": dist[n["id"]], "labels": n["labels"], "props": n["props"]}
            for n in self.nodes.values()
            if n["id"] in dist and n["id"] not in p["seeds"]
        ]

    def _walk_edges(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        reach = self._reach(p, False)
        return [
            self._row(e)
            for e in self.edges.values()
            if e["weight"] > 0
            and (p["ns"] is None or e["ns"] == p["ns"])
            and self.nodes[e["a"]]["id"] in reach
            and self.nodes[e["b"]]["id"] in reach
        ]

    def _node_count(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"c": len(self.nodes)}]

    def _edge_count(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"c": len(self.edges)}]

    def _clear(self, p: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.nodes.clear()
        self.edges.clear()
        return []

    def snapshot(self) -> tuple[list[Any], list[Any]]:
        nodes = sorted((n["id"], n["ns"], tuple(n["labels"])) for n in self.nodes.values())
        edges = sorted(
            (self.nodes[e["a"]]["id"], self.nodes[e["b"]]["id"], e["rel"], e["ns"], e["weight"])
            for e in self.edges.values()
        )
        return nodes, edges


# --------------------------------------------------------------------------- live executor
async def live_executor(prefix: str = "MemTest") -> tuple[Any, Neo4jExecutor]:
    """RM-113: an opened Neo4j connector on the live test server and the adapter's executor
    over it (unique label and type). The caller clears the graph and closes the connector."""
    from memspine.clients.neo4j import Neo4jClient, Neo4jSpec
    from memspine.services.graph.neo4j import connector_executor

    connector = Neo4jClient(
        Neo4jSpec(
            connect_timeout_s=30.0,
            request_timeout_s=60.0,
            url_env="MEMSPINE_TEST_NEO4J_URL",
            user_env="MEMSPINE_TEST_NEO4J_USER",
            password_env="MEMSPINE_TEST_NEO4J_PASSWORD",
        )
    )
    await connector.open()
    return connector, connector_executor(
        connector,
        node_label=f"{prefix}{uuid.uuid4().hex[:10]}",
        rel_type=f"{prefix}Rel{uuid.uuid4().hex[:10]}",
    )


@pytest.fixture(params=["fake", "live"])
async def executor(request: pytest.FixtureRequest):
    if request.param == "fake":
        yield FakeNeo4j()
        return
    if not (HAVE_DRIVER and LIVE_URL):
        pytest.skip(LIVE_SKIP)
    connector, ex = await live_executor()
    try:
        yield ex
    finally:
        await ex.run("clear", {})
        await connector.close()


def _store(ex: Neo4jExecutor, *, scoped: bool = False) -> Neo4jGraphStore:
    return Neo4jGraphStore(ex, scoped=scoped)


# --------------------------------------------------------------------------- adapter contract
async def test_store_satisfies_the_graph_port(executor: Neo4jExecutor) -> None:
    assert isinstance(_store(executor), GraphStore)


async def test_reapplying_a_merge_is_idempotent(executor: Neo4jExecutor) -> None:
    g = _store(executor)
    for _ in range(3):
        await g.upsert_node("r1", ["semantic"], {"namespace": NS}, namespace=NS)
        await g.upsert_edge("r1", "r2", "related", {"weight": 0.5, "reason": "x"}, namespace=NS)
    assert await g.node_count() == 2  # r2 was created bare by the edge
    assert await g.edge_count() == 1
    [edge] = await g.edge_list(NS)
    assert (edge.src, edge.dst, edge.rel_type, edge.weight) == ("r1", "r2", "related", 0.5)
    await g.upsert_edge("r1", "r2", "related", {"weight": 0.9}, namespace=NS)  # replace
    assert (await g.edge_list(NS))[0].weight == 0.9 and await g.edge_count() == 1


async def test_neighbors_skip_tombstones_and_respect_namespace(executor: Neo4jExecutor) -> None:
    g = _store(executor)
    await g.upsert_edge("a", "b", "related", {"weight": 1.0}, namespace=NS)
    await g.upsert_edge("b", "c", "related", {"weight": 1.0}, namespace=NS)
    await g.upsert_edge("a", "x", "related", {"weight": 0.0}, namespace=NS)  # tombstone
    await g.upsert_edge("a", "y", "related", {"weight": 1.0}, namespace=NS2)
    assert [n.node_id for n in await g.neighbors("a", namespace=NS)] == ["b"]
    assert [n.node_id for n in await g.neighbors("a", depth=2, namespace=NS)] == ["b", "c"]
    assert sorted(n.node_id for n in await g.neighbors("a")) == ["b", "y"]
    sub = await g.subgraph(["a"], depth=2, namespace=NS)
    assert [(e.src, e.dst) for e in sub] == [("a", "b"), ("b", "c")]


async def test_delete_node_cascades_edges_and_drops_orphan_bare_nodes(
    executor: Neo4jExecutor,
) -> None:
    g = _store(executor)
    await g.upsert_node("rec", ["semantic"], {"namespace": NS}, namespace=NS)
    await g.upsert_node("keep", ["semantic"], {"namespace": NS}, namespace=NS)
    await g.upsert_edge("rec", "lonely", "related", {"weight": 1.0}, namespace=NS)  # bare end
    await g.upsert_edge("rec", "keep", "related", {"weight": 1.0}, namespace=NS)
    await g.delete_node("rec")
    await g.delete_node("rec")  # idempotent
    assert await g.edge_count() == 0
    assert await g.node_count() == 1  # only the explicit "keep"; the bare "lonely" went too
    assert [n.node_id for n in await g.neighbors("keep")] == []


async def test_scope_keys_keep_tenants_apart(executor: Neo4jExecutor) -> None:
    scoped = _store(executor, scoped=True)
    await scoped.upsert_edge("shared", "t", "related", {"weight": 1.0}, namespace=NS)
    await scoped.upsert_edge("shared", "t", "related", {"weight": 1.0}, namespace=NS2)
    assert await scoped.node_count() == 4  # two tenants, two copies of each node
    assert await scoped.edge_count() == 2
    assert [e.rel_type for e in await scoped.edge_list(NS)] == ["related"]
    await scoped.clear()
    unscoped = _store(executor, scoped=False)
    await unscoped.upsert_edge("shared", "t", "related", {"weight": 1.0}, namespace=NS)
    await unscoped.upsert_edge("shared", "t", "related", {"weight": 1.0}, namespace=NS2)
    assert await unscoped.node_count() == 2 and await unscoped.edge_count() == 1


async def test_prune_dead_removes_only_compiled_nodes_without_live_edges(
    executor: Neo4jExecutor,
) -> None:
    g = _store(executor)
    await g.upsert_node("as1", ["assertion"], {"namespace": NS, "compiled": COMPILED}, namespace=NS)
    await g.upsert_node("rec", ["semantic"], {"namespace": NS}, namespace=NS)
    await g.upsert_edge("as1", "rec", "role:subject", {"weight": 1.0}, namespace=NS)
    await g.prune_dead(["as1", "rec"], COMPILED)
    assert await g.node_count() == 2  # as1 still has a live edge
    await g.upsert_edge("as1", "rec", "role:subject", {"weight": 0.0}, namespace=NS)
    await g.prune_dead(["as1", "rec"], COMPILED)
    assert await g.node_count() == 1  # the compiled node went, the record node stayed
    assert [n.node_id for n in await g.neighbors("rec")] == []


def test_constructing_without_an_executor_still_names_the_extra() -> None:
    from memspine.exceptions import MissingServiceError

    with pytest.raises(MissingServiceError):
        Neo4jGraphStore()


# --------------------------------------------------------------------------- engine wiring
def _engine(*, scoped: bool = False) -> Engine:
    """Neo4j as THE graph client (``storage.graph``; ADR-081 amendment 2026-10-23)."""
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            },
            "graph": {
                "provider": "neo4j",
                "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 2.0},
            },
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False},
        deployment={"id": "test-deployment"},
        isolation={"scope_keys_v2": scoped},
    )


def test_one_graph_client_by_default() -> None:
    storage = StorageConfig()
    assert storage.graph.provider == "auto"
    assert "graph_mirror" not in StorageConfig.model_fields


def test_the_neo4j_descriptor_declares_no_mirror_capability() -> None:
    descriptor = get_registry("graph_stores").get("neo4j")
    assert "mirror" not in descriptor.declared_capabilities


async def test_neo4j_graph_without_deployment_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "bolt://localhost:7687")
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
            "graph": {
                "provider": "neo4j",
                "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 2.0},
            },
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
    )
    with pytest.raises(ConfigError, match=r"deployment\.id"):
        await eng.start()


def _install_fake_neo4j(monkeypatch: pytest.MonkeyPatch, fake: FakeNeo4j) -> None:
    """Swap the Neo4j descriptor's builder for one over the in-memory fake."""
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "bolt://localhost:7687")
    descriptor = get_registry("graph_stores").get("neo4j")

    async def build(_settings: Any, ctx: Any) -> BuildResult[GraphStore]:
        ref = ctx.shared.resources.own(ctx.resource_id, fake.close)
        store = Neo4jGraphStore(fake, scoped=True)

        async def probe() -> bool:
            await store.node_count()
            return True

        return BuildResult(
            store,
            ProviderRef("graph", "neo4j"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase"}),
            health_probe=probe,
        )

    monkeypatch.setattr(descriptor.builder, "build", build)


async def test_engine_projects_into_the_neo4j_graph_and_forget_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    fake = FakeNeo4j()
    _install_fake_neo4j(monkeypatch, fake)
    eng = _engine()
    await eng.start()
    try:
        assert isinstance(eng._graph, Neo4jGraphStore)
        assert eng._provider_results["graph.primary"].provider.name == "neo4j"
        rec = await eng.write("Ilse works at Acme", namespace=NS, memory_type="semantic")
        assert rec.record_id in {n["id"] for n in fake.nodes.values()}
        await eng.forget(rec.record_id, namespace=NS)
        assert rec.record_id not in {n["id"] for n in fake.nodes.values()}
        stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
        assert "graph.neo4j.merge_node" in stages
        await eng.rebuild()  # the graph projector clears and replays into Neo4j
        assert "clear" in fake.calls
    finally:
        await eng.stop()
    assert fake.closed


async def test_scope_keys_flag_reaches_the_neo4j_store(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNeo4j()
    _install_fake_neo4j(monkeypatch, fake)
    eng = _engine(scoped=True)
    await eng.start()
    try:
        assert isinstance(eng._graph, Neo4jGraphStore) and eng._graph.scoped is True
        await eng.write("Noor lives in Utrecht", namespace=NS, memory_type="semantic")
        assert all("\x1f" in key for key in fake.nodes)
    finally:
        await eng.stop()
