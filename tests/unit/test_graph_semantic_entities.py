"""GR-3 / GR-5 / GR-6: embedded entity nodes and hybrid entity search, on LadybugDB
(native HNSW + FTS) and on sql_adjacency (Python fallback)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine import Engine
from memspine.config.schema import GraphConfig, ReadConfig


def test_keys_are_off_by_default() -> None:
    assert GraphConfig().entity_embeddings is False
    assert ReadConfig().graph_node_search is False


async def _names(eng: Engine, nodes: list[str]) -> list[str]:
    """RM-130: entity ids are opaque; their names come from the name index."""
    assert eng._associative is not None
    names = await eng._associative.entity_names("u", nodes)
    return [names.get(n, n) for n in nodes]


def _engine(path: Path, provider: str, **read: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path / "m.db"), "busy_timeout_s": 5.0},
            },
            "graph": {"provider": provider},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        graph={"entity_embeddings": True, "edge_compiler": True},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False, "graph_node_search": True, **read},
    )


@pytest.mark.parametrize("provider", ["sql_adjacency", "ladybug"])
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
        assert eng._associative is not None  # RM-130: the opaque id from the name index
        [luna_id] = await (await eng._associative.entity_lookup("u")).get("luna")
        vector = (await eng._embedder.embed([query]))[0]
        hits = await eng._graph.search_entities("u", vector, query, 5)
        legs = await eng._graph_node_legs("u", query, vector, 10)
    finally:
        await eng.stop()
    assert hits and hits[0][0] == luna_id  # the Luna entity node ranks first
    assert all(":u:" in node_id or node_id.startswith("ent:u") for node_id, _ in hits)
    assert legs and luna.record_id in [h.record_id for h in legs[0]]
    assert getattr(legs[0], "name", None) == "graph_nodes"


@pytest.mark.parametrize("provider", ["sql_adjacency", "ladybug"])
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
        names = await _names(eng, [n for n, _ in hits])
    finally:
        await eng.stop()
    assert hits and names[0] == "luna"


@pytest.mark.parametrize("provider", ["sql_adjacency", "ladybug"])
async def test_turn_mentions_let_the_graph_reach_raw_turns(tmp_path: Path, provider: str) -> None:
    """G-7: with ``turn_mentions`` a raw turn's own names become mentions edges."""
    if provider == "ladybug":
        pytest.importorskip("ladybug")
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "m.db"), "busy_timeout_s": 5.0},
            },
            "graph": {"provider": provider},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        graph={"entity_embeddings": True, "edge_compiler": True},
        memories={
            "episodic": {"enabled": True},
            "associative": {
                "enabled": True,
                "policies": {"entity_nodes": {"turn_mentions": True}},
            },
        },
        read={"record_access": False, "graph_node_search": True},
    )
    await eng.start()
    try:
        turn = await eng.write(
            "Ana: Yes! We took Luna to the vet in Leeds.", namespace="u", memory_type="episodic"
        )
        vector = (await eng._embedder.embed(["Luna"]))[0]
        legs = await eng._graph_node_legs("u", "how is Luna?", vector, 10)
        found = [n for n, _ in await eng._graph.search_entities("u", vector, "Luna Leeds", 10)]
        nodes = await _names(eng, found)
    finally:
        await eng.stop()
    assert legs and turn.record_id in [h.record_id for h in legs[0]]
    assert "luna" in nodes and "leeds" in nodes
    assert "yes" not in nodes and "ana" not in nodes


async def test_focal_entity_seeds_the_graph_and_does_not_leak(tmp_path: Path) -> None:
    """G-11: ``focal_entity=`` adds a graph seed for one read only."""
    from memspine.read.pipeline.state import _FOCAL_ENTITY

    eng = _engine(tmp_path, "sql_adjacency", graph_leg=True)
    await eng.start()
    seen: list[list[str]] = []
    real = eng._graph_seeds

    async def spy(ns: str, query: str, fallback_ids: object = ()) -> list[str]:
        out = await real(ns, query, fallback_ids)  # type: ignore[arg-type]
        seen.append(out)
        return out

    eng._graph_seeds = spy  # type: ignore[method-assign]
    try:
        await eng.write("Luna is a grey cat", namespace="u", entity="Luna", attribute="pet")
        await eng.search("what about her?", namespace="u", focal_entity="Luna")
        after = _FOCAL_ENTITY.get()
        named = [await _names(eng, seeds) for seeds in seen]
    finally:
        await eng.stop()
    assert any("luna" in names for names in named)
    assert after is None
