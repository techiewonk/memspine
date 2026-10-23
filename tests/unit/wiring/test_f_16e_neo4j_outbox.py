"""F-16e: Neo4j graph adapter fed through the outbox (``graph.neo4j_outbox``), model-free.

The adapter contract runs against an in-memory MERGE recorder (``FakeNeo4j``) and, when the
``neo4j`` driver is importable and ``MEMSPINE_TEST_NEO4J_URL`` is set (plus optional
``MEMSPINE_TEST_NEO4J_USER`` / ``MEMSPINE_TEST_NEO4J_PASSWORD``), against a live server under a
private label that is cleared afterwards. Otherwise the live variants skip with a reason.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.clients.sqlite.sync import memory_sync_engine, sqlite_sync_engine
from memspine.config.schema import StorageConfig
from memspine.config.schema.observability import TapConfig
from memspine.exceptions import ConfigError
from memspine.memories.associative.entities import EntityPolicy, entity_node_id
from memspine.memories.associative.graph_outbox import (
    GRAPH_SCOPE,
    GRAPH_STAGE,
    Neo4jGraphHandler,
    Neo4jOutboxProjector,
    rebuild_from_log,
)
from memspine.observability import tap
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import get_registry
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.runner import InlineRunner
from memspine.services.graph.base import GraphStore
from memspine.services.graph.neo4j import CYPHER, Neo4jExecutor, Neo4jGraphStore
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

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


# --------------------------------------------------------------------------- outbox plumbing
def _rec(rid: str, ns: str = NS, trust: float = 0.9) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid, namespace=ns, memory_type="semantic", content="x", trust=trust
    )


def _write(seq: int, rid: str, ns: str = NS) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace=ns,
        seq=seq,
        payload={"record": json.loads(_rec(rid, ns).model_dump_json())},
    )


def _link(seq: int, src: str, dst: str, **extra: Any) -> MemoryEvent:
    payload = {"src": src, "dst": dst, "rel": "related", "weight": 1.0, "reason": "t", **extra}
    return MemoryEvent(kind=EventKind.LINK, namespace=NS, seq=seq, payload=payload)


def _forget(seq: int, rid: str) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.FORGET, namespace=NS, seq=seq, payload={"record_id": rid})


def _runner(engine: Any = None, clock: Any = None) -> InlineRunner:
    return InlineRunner(
        SqlJobStore(engine or memory_sync_engine()),
        clock=clock or ManualClock(T0),
        budget=RetryBudget(base_backoff=timedelta(0)),
    )


def _wire(
    fake: FakeNeo4j,
    runner: InlineRunner,
    *,
    scoped: bool = False,
    entities: EntityPolicy | None = None,
    drain: bool = False,
) -> tuple[Neo4jOutboxProjector, Neo4jGraphHandler]:
    handler = Neo4jGraphHandler(_store(fake, scoped=scoped), entities, compiled=COMPILED)
    handler.register(runner)
    return Neo4jOutboxProjector(runner, drain=drain), handler


async def test_events_become_outbox_items_and_apply_idempotently() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner)
    events = [_write(1, "r1"), _write(2, "r2"), _link(3, "r1", "r2", weight=0.7)]
    for ev in events:
        await proj.apply(ev)
    assert fake.merges == 0  # queued, nothing applied yet: the outbox row is the truth
    assert runner.pending_items(GRAPH_SCOPE, GRAPH_STAGE).__len__() == 3
    await runner.drain(GRAPH_SCOPE)
    first = fake.snapshot()
    assert [n[0] for n in first[0]] == ["r1", "r2"] and first[1] == [
        ("r1", "r2", "related", NS, 0.7)
    ]
    for ev in events:  # catch-up re-delivery of the same events
        await proj.apply(ev)
    await runner.drain(GRAPH_SCOPE)
    assert fake.snapshot() == first


async def test_retrieve_events_are_not_queued() -> None:
    runner = _runner()
    proj, _ = _wire(FakeNeo4j(), runner)
    await proj.apply(MemoryEvent(kind=EventKind.RETRIEVE, namespace=NS, seq=1, payload={}))
    assert runner.pending_items(GRAPH_SCOPE, GRAPH_STAGE) == []


async def test_scoped_handler_uses_scope_keys() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner, scoped=True)
    a = _link(1, "shared", "t")
    b = MemoryEvent(kind=EventKind.LINK, namespace=NS2, seq=2, payload=dict(a.payload))
    await proj.apply(a)
    await proj.apply(b)
    await runner.drain(GRAPH_SCOPE)
    assert len(fake.nodes) == 4 and len(fake.edges) == 2


async def test_forget_removes_node_edges_and_orphan_entities() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner, entities=EntityPolicy())
    ent = entity_node_id(NS, "acme")
    rec = _rec("r1")
    rec = rec.model_copy(update={"entity": "Acme", "content": "Acme hires"})
    ev = MemoryEvent(
        kind=EventKind.WRITE,
        namespace=NS,
        seq=1,
        payload={"record": json.loads(rec.model_dump_json())},
    )
    await proj.apply(ev)
    await proj.apply(_write(2, "r2"))
    await proj.apply(_link(3, "r1", "r2"))
    await runner.drain(GRAPH_SCOPE)
    assert any(n["id"] == "r1" for n in fake.nodes.values())
    await proj.apply(_forget(4, "r1"))
    await runner.drain(GRAPH_SCOPE)
    ids = {n["id"] for n in fake.nodes.values()}
    assert "r1" not in ids and ent not in ids  # node and its orphan entity are gone
    assert not [
        e
        for e in fake.edges.values()
        if "r1" in (fake.nodes[e["a"]]["id"], fake.nodes[e["b"]]["id"])
    ]
    assert "r2" in ids


async def test_compiled_tombstone_prunes_the_assertion_node() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner)
    node = {"labels": ["assertion"], "properties": {"namespace": NS, "compiled": COMPILED}}
    await proj.apply(_write(1, "rec"))
    await proj.apply(
        _link(
            2,
            "as1",
            "rec",
            rel="role:subject",
            reason=COMPILED,
            src_node=node,
            props={"record_id": "rec"},
        )
    )
    await runner.drain(GRAPH_SCOPE)
    assert {n["id"] for n in fake.nodes.values()} == {"as1", "rec"}
    await proj.apply(
        _link(3, "as1", "rec", rel="role:subject", weight=0.0, reason=f"{COMPILED}:erasure")
    )
    await runner.drain(GRAPH_SCOPE)
    assert {n["id"] for n in fake.nodes.values()} == {"rec"}  # edges and the assertion node went
    assert fake.edges == {}


async def test_a_hole_is_retried_and_the_graph_converges() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner)
    fake.fail_at = {2: RuntimeError("neo4j down")}  # the second mutation fails once
    for i, rid in enumerate(["a", "b", "c"], start=1):
        await proj.apply(_write(i, rid))
    first = await runner.drain(GRAPH_SCOPE)
    assert first.holes == 1
    await runner.drain(GRAPH_SCOPE)  # the hole is retried inside its budget
    assert sorted(n["id"] for n in fake.nodes.values()) == ["a", "b", "c"]
    assert runner.book.progress(GRAPH_SCOPE, GRAPH_STAGE).holes == []


async def test_crash_mid_batch_resumes_from_the_watermark(tmp_path: Path) -> None:
    path = str(tmp_path / "outbox.db")
    clock = ManualClock(T0)
    fake = FakeNeo4j()
    first = _runner(sqlite_sync_engine(path, busy_timeout_s=5.0), clock)
    proj, _ = _wire(fake, first)
    for i, rid in enumerate(["a", "b", "c", "d", "e"], start=1):
        await proj.apply(_write(i, rid))
    fake.fail_at = {3: Crash()}  # the process dies while applying the third item
    with pytest.raises(Crash):
        await first.drain(GRAPH_SCOPE)
    assert first.book.progress(GRAPH_SCOPE, GRAPH_STAGE).watermark == 1  # items 0 and 1 done
    first.close()
    done_before = fake.merges

    clock.advance(3600)  # the dead worker's lease expires
    second = _runner(sqlite_sync_engine(path, busy_timeout_s=5.0), clock)
    _wire(fake, second)
    await second.start()
    assert sorted(n["id"] for n in fake.nodes.values()) == ["a", "b", "c", "d", "e"]
    assert fake.merges - done_before == 3  # only items 2..4 ran; 0 and 1 were not redone
    assert second.depth() == 0


async def test_rebuild_from_the_log_equals_the_incremental_graph() -> None:
    events = [
        _write(1, "r1"),
        _write(2, "r2"),
        _write(3, "r3"),
        _link(4, "r1", "r2", weight=0.4),
        _link(5, "r2", "r3"),
        _forget(6, "r3"),
    ]

    class Log:
        async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
            return [e for e in events if (e.seq or 0) > after_seq][:limit]

    live, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(live, runner)
    for ev in events:
        await proj.apply(ev)
    await runner.drain(GRAPH_SCOPE)

    rebuilt = FakeNeo4j()
    stale = _store(rebuilt)
    await stale.upsert_node("garbage", [], {"namespace": NS}, namespace=NS)  # must be cleared
    count = await rebuild_from_log(Log(), Neo4jGraphHandler(stale, None, compiled=COMPILED))
    assert count == len(events)
    assert rebuilt.snapshot() == live.snapshot()


async def test_projector_reset_clears_then_replay_refeeds() -> None:
    fake, runner = FakeNeo4j(), _runner()
    proj, _ = _wire(fake, runner)
    await proj.apply(_write(1, "r1"))
    await runner.drain(GRAPH_SCOPE)
    await proj.reset()
    await proj.apply(_write(1, "r1"))  # what the engine's replay does after reset
    await runner.drain(GRAPH_SCOPE)
    assert "clear" in fake.calls and [n["id"] for n in fake.nodes.values()] == ["r1"]


# --------------------------------------------------------------------------- engine wiring
def _engine(*, mirror: bool = False, deployment_id: str | None = None) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            },
            "graph": {"provider": "sql_adjacency", "settings": {}},
            "graph_mirror": (
                {
                    "provider": "neo4j",
                    "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 2.0},
                }
                if mirror
                else None
            ),
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False},
        deployment={"id": deployment_id} if deployment_id is not None else {},
    )


def test_flag_default_off() -> None:
    storage = StorageConfig()
    assert storage.graph_mirror is None and storage.graph.provider == "auto"


async def test_mirror_without_deployment_id_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "bolt://localhost:7687")
    eng = _engine(mirror=True)
    with pytest.raises(ConfigError, match=r"deployment\.id"):
        await eng.start()


async def test_mirror_provider_without_the_mirror_capability_is_refused() -> None:
    """ADR-081: the mirror gate is the provider's ``mirror`` capability, not a profile."""
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
            "graph": {"provider": "sql_adjacency"},
            "graph_mirror": {"provider": "sql_adjacency"},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
        deployment={"id": "test-deployment"},
    )
    with pytest.raises(ConfigError, match="'mirror' capability"):
        await eng.start()


async def test_flag_off_builds_no_neo4j() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write("Ilse works at Acme", namespace=NS, memory_type="semantic")
        assert eng._neo4j is None
        assert "graph.mirror" not in eng._provider_results
    finally:
        await eng.stop()


def _install_fake_mirror(monkeypatch: pytest.MonkeyPatch, fake: FakeNeo4j) -> None:
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
            frozenset({"graph", "namespace_filter", "erase", "mirror"}),
            health_probe=probe,
        )

    monkeypatch.setattr(descriptor.builder, "build", build)


async def test_engine_feeds_neo4j_through_the_outbox_and_forget_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    fake = FakeNeo4j()
    _install_fake_mirror(monkeypatch, fake)
    eng = _engine(mirror=True, deployment_id="test-deployment")
    await eng.start()
    try:
        assert eng._neo4j is not None
        assert "graph.mirror" in eng._provider_results
        rec = await eng.write("Ilse works at Acme", namespace=NS, memory_type="semantic")
        embedded = {n for n in [rec.record_id]}
        assert embedded <= {n["id"] for n in fake.nodes.values()}  # mirrored, drained inline
        # the embedded graph is the read path and still has it
        assert await eng._graph.node_count() >= 1
        await eng.forget(rec.record_id, namespace=NS)
        ids = {n["id"] for n in fake.nodes.values()}
        assert rec.record_id not in ids
        stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
        assert {"graph.neo4j.enqueue", "graph.neo4j.apply", "graph.neo4j.merge_node"} <= stages
        # a rebuild of every projector re-feeds Neo4j from the log
        await eng.rebuild()
        assert "clear" in fake.calls
    finally:
        await eng.stop()
    assert fake.closed


async def test_scope_keys_flag_reaches_the_neo4j_store(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeNeo4j()
    _install_fake_mirror(monkeypatch, fake)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            },
            "graph_mirror": {
                "provider": "neo4j",
                "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 2.0},
            },
            "graph": {"provider": "sql_adjacency", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
        read={"record_access": False},
        deployment={"id": "test-deployment"},
        isolation={"scope_keys_v2": True},
    )
    await eng.start()
    try:
        assert eng._neo4j is not None and eng._neo4j.store.scoped is True
        await eng.write("Noor lives in Utrecht", namespace=NS, memory_type="semantic")
        assert all("\x1f" in key for key in fake.nodes)
    finally:
        await eng.stop()
