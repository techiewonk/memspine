"""KB-1: migration 0003 adds namespace/weight/kind to the graph tables and
backfills them on a database stamped before it."""

from __future__ import annotations

from pathlib import Path

import orjson
from alembic import command
from sqlalchemy import create_engine, inspect, text

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head


def _columns(db: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db}")
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


async def test_fresh_head_has_graph_columns_and_indexes(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    upgrade_to_head(db)
    assert {"namespace", "weight", "kind"} <= _columns(db, "graph_edges")
    assert {"namespace", "kind"} <= _columns(db, "graph_nodes")
    engine = create_engine(f"sqlite:///{db}")
    try:
        names = {ix["name"] for ix in inspect(engine).get_indexes("graph_edges")}
    finally:
        engine.dispose()
    assert {"ix_graph_edges_ns_src_weight", "ix_graph_edges_ns_dst_weight"} <= names


async def test_upgrade_backfills_a_pre_kb1_graph(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    upgrade_to_head(db)
    cfg = alembic_config(db)
    command.downgrade(cfg, "0002")
    assert not ({"namespace", "weight", "kind"} & _columns(db, "graph_edges"))

    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        insert_node = text(
            "INSERT INTO graph_nodes (node_id, labels, properties) VALUES (:n, :l, :p)"
        )
        conn.execute(
            insert_node,
            {"n": "r1", "l": orjson.dumps(["episodic"]), "p": orjson.dumps({"namespace": "ns/a"})},
        )
        conn.execute(insert_node, {"n": "r2", "l": orjson.dumps([]), "p": orjson.dumps({})})
        insert_edge = text(
            "INSERT INTO graph_edges (src, dst, rel_type, properties) VALUES (:s, :d, :r, :p)"
        )
        conn.execute(insert_edge, {"s": "r1", "d": "r2", "r": "related", "p": b'{"weight":0.0}'})
        conn.execute(
            insert_edge,
            {"s": "r2", "d": "r1", "r": "asserted", "p": b'{"weight":0.4,"kind":"event"}'},
        )
        conn.execute(insert_edge, {"s": "r1", "d": "r2", "r": "odd", "p": b'{"weight":"x"}'})
    engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        nodes = dict(conn.execute(text("SELECT node_id, namespace FROM graph_nodes")).all())
        kinds = dict(conn.execute(text("SELECT node_id, kind FROM graph_nodes")).all())
        edges = {
            row[0]: row[1:]
            for row in conn.execute(
                text("SELECT rel_type, namespace, weight, kind FROM graph_edges")
            ).all()
        }
    engine.dispose()
    assert nodes == {"r1": "ns/a", "r2": ""}
    assert kinds == {"r1": "episodic", "r2": None}
    assert edges["related"] == ("ns/a", 0.0, None)
    assert edges["asserted"] == ("ns/a", 0.4, "event")  # src r2 has no ns -> dst's
    assert edges["odd"] == ("ns/a", 1.0, None)  # non-numeric weight -> 1.0

    # The backfilled tombstone is still gone for the CTE walk.
    client = SQLiteClient(db)
    await client.connect()
    try:
        graph = SQLiteAdjacencyGraph(client)
        assert [n.node_id for n in await graph.neighbors("r1", namespace="ns/a")] == ["r2"]
        assert [n.node_id for n in await graph.neighbors("r1", rel_type="related")] == []
    finally:
        await client.close()
