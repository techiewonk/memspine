"""RM-130 (ADR-089) end to end: opaque entity ids minted into the log, the name index as a
rebuildable projection, evidence-cited merges undone on erasure, seeds and search intact."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.exceptions import ConflictError
from memspine.memories.associative.entities import MENTIONS_REL, parse_entity_node
from memspine.memories.associative.entity_index import LINKS_KEY, entity_status
from memspine.services.storage.entity_index_tables import entity_index_store_of
from memspine.store.events import EventKind

NS = "a"


async def _engine(path: str = ":memory:") -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": path}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": True, "record_access": False},
    )
    await eng.start()
    return eng


async def _links(eng: Engine) -> list[dict[str, Any]]:
    assert eng._storage is not None
    return [
        link
        for e in await eng._storage.read_events(0, 100_000)
        if e.kind is EventKind.WRITE
        for link in e.payload.get(LINKS_KEY, [])
    ]


async def _index_rows(eng: Engine) -> list[tuple[object, ...]]:
    store = entity_index_store_of(eng._storage)
    out: list[tuple[object, ...]] = []
    for kind in ("entity", "name", "link", "merge", "cite"):
        out.extend(
            (r.kind, r.key, r.entity_id, r.ref, r.match, sorted(r.data.items()))
            for r in await store.select(NS, kind)
        )
    return out


async def _mention_edges(eng: Engine) -> list[tuple[str, str, float]]:
    assert eng._graph is not None
    return sorted(
        (e.src, e.dst, e.weight)
        for e in await eng._graph.edge_list(NS)
        if e.rel_type == MENTIONS_REL
    )


async def test_ids_are_opaque_minted_once_and_rebuild_reproduces_ids_and_index(
    tmp_path: Any,
) -> None:
    eng = await _engine(str(tmp_path / "m.db"))
    try:
        await eng.write("Alex lives in Rome", namespace=NS, entity="Alex", tags=["dst:Rome"])
        await eng.write("Alex plays chess", namespace=NS, entity="Alex")
        gone = await eng.write("Dana bakes", namespace=NS, entity="Dana")
        await eng.forget(gone.record_id, namespace=NS)
        links = await _links(eng)
        prefix = f"ent:{NS}:"
        assert all(
            parse_entity_node(lk["entity_id"]) == (NS, lk["entity_id"][len(prefix) :])
            for lk in links
        )
        assert all("alex" not in lk["entity_id"] for lk in links)
        assert links[0]["entity_id"] == links[2]["entity_id"]  # unique-name link
        rows, edges = await _index_rows(eng), await _mention_edges(eng)
        assert rows and edges
        await eng.rebuild()
        assert await _index_rows(eng) == rows
        assert await _mention_edges(eng) == edges
        assert await _links(eng) == links  # replay never minted a new id
    finally:
        await eng.stop()


async def test_same_named_people_stay_distinct_and_search_still_works() -> None:
    eng = await _engine()
    try:
        await eng.write("Alex fixed the bike", namespace=NS, entity="Alex")
        await eng.write(
            "Alex taught the piano lesson", namespace=NS, entity="Alex", tags=["entity_ref:new"]
        )
        assert eng._associative is not None
        lookup = await eng._associative.entity_lookup(NS)
        two = await lookup.get("alex")
        assert len(two) == 2  # two people, two ids
        # a bare third mention is ambiguous: a provisional placeholder, never one of them
        third = await eng.write("Alex was late", namespace=NS, entity="Alex")
        [link] = [lk for lk in await _links(eng)][-1:]
        assert link["basis"] == "ambiguous" and sorted(link["candidates"]) == sorted(two)
        seeds = await eng._graph_seeds(NS, "what did alex do")
        assert set(two) <= set(seeds) and link["entity_id"] in seeds
        hits = await eng.search("Alex", namespace=NS, top_k=5)
        assert third.record_id in {r.record_id for r, _ in hits}
    finally:
        await eng.stop()


async def test_merge_needs_evidence_and_erasure_of_the_evidence_undoes_it() -> None:
    eng = await _engine()
    try:
        await eng.write("Mel paints", namespace=NS, entity="Mel")
        await eng.write("Melanie sings", namespace=NS, entity="Melanie")
        evidence = await eng.write("Mel is what everyone calls Melanie", namespace=NS)
        assert eng._associative is not None
        lookup = await eng._associative.entity_lookup(NS)
        [mel], [melanie] = await lookup.get("mel"), await lookup.get("melanie")
        with pytest.raises(ConflictError, match="evidence"):
            await eng._associative.merge_entities(NS, melanie, mel, [])
        await eng._associative.merge_entities(NS, melanie, mel, [evidence.record_id])
        assert await entity_status(eng._storage, NS, mel) == "confirmed"
        assert set(await eng._graph_seeds(NS, "melanie")) == {melanie, mel}
        await eng.forget(evidence.record_id, namespace=NS, hard=True)
        assert await eng._graph_seeds(NS, "melanie") == [melanie]
        assert await entity_status(eng._storage, NS, mel) == "contested"
        await eng.rebuild()  # the undo is in the log: a rebuild keeps it undone
        assert await eng._graph_seeds(NS, "melanie") == [melanie]
    finally:
        await eng.stop()
