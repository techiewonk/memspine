"""GR-3 / GR-5 / GR-6: embedded entity nodes and hybrid entity search, on LadybugDB
(native HNSW + FTS) and on sqlite_adjacency (Python fallback)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine import Engine
from memspine.config.schema import GraphConfig, ReadConfig


def test_keys_are_off_by_default() -> None:
    assert GraphConfig().entity_embeddings is False
    assert ReadConfig().graph_node_search is False


def _engine(path: Path, provider: str) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        graph={"provider": provider, "entity_embeddings": True},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False, "graph_node_search": True},
    )


@pytest.mark.parametrize("provider", ["sqlite_adjacency", "ladybug"])
async def test_entity_search_and_node_leg(tmp_path: Path, provider: str) -> None:
    if provider == "ladybug":
        pytest.importorskip("ladybug")
    eng = _engine(tmp_path, provider)
    await eng.start()
    try:
        luna = await eng.write("Luna is a grey cat", namespace="u", entity="Luna", attribute="pet")
        await eng.write(
            "Paris is the capital of France", namespace="u", entity="Paris", attribute="city"
        )
        await eng.write("Luna the dog of Bob", namespace="other", entity="Luna", attribute="pet")
        query = "Luna"
        vector = (await eng._embedder.embed([query]))[0]
        hits = await eng._graph.search_entities("u", vector, query, 5)
        legs = await eng._graph_node_legs("u", query, vector, 10)
    finally:
        await eng.stop()
    assert hits and "luna" in hits[0][0]  # the Luna entity node ranks first
    assert all(":u:" in node_id or node_id.startswith("ent:u") for node_id, _ in hits)
    assert legs and luna.record_id in [h.record_id for h in legs[0]]
    assert getattr(legs[0], "name", None) == "graph_nodes"


@pytest.mark.parametrize("provider", ["sqlite_adjacency", "ladybug"])
async def test_entity_index_survives_reopen(tmp_path: Path, provider: str) -> None:
    if provider == "ladybug":
        pytest.importorskip("ladybug")
    eng = _engine(tmp_path, provider)
    await eng.start()
    try:
        await eng.write("Luna is a grey cat", namespace="u", entity="Luna", attribute="pet")
    finally:
        await eng.stop()
    eng = _engine(tmp_path, provider)
    await eng.start()
    try:
        vector = (await eng._embedder.embed(["Luna"]))[0]
        hits = await eng._graph.search_entities("u", vector, "Luna", 5)
    finally:
        await eng.stop()
    assert hits and "luna" in hits[0][0]
