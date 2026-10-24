"""RM-110: the graph seed lookup reads a maintained entity index, not a namespace scan.

``_graph_seeds`` used to rebuild ``AssociativeMemory.entity_index`` (every edge of the
namespace via ``edge_list``) on each graph read. It now reads the per-namespace index
kept by :mod:`memspine.memories.associative.entity_cache`, maintained by the graph
projector. Proved here: identical seed lists against the scan reference on the RM-93
world and on randomized entity worlds (writes, forgets, punctuation variants), namespace
isolation, forgotten and erased entities never seeding, a second Engine on the same
store seeing the first one's writes, and the read touching ``edge_list`` once (the build).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from test_rm93_graph_filter import NS, QUERY, _build, _engine

from memspine import Engine
from memspine.config import constants
from memspine.memories.associative import entity_cache
from memspine.memories.associative.entities import match_key


async def _scan_index(eng: Engine, ns: str) -> dict[str, list[str]]:
    """``entity_index`` (a fresh namespace scan) with each key's nodes sorted by id. A key
    holds several nodes only for punctuation variants ("o'brien", "o-brien"); the scan
    listed those in the store's row order (on SQLite the index order of the mentioning
    record's random id), so only that within-key order is normalised, nothing else."""
    assert eng._associative is not None
    return {k: sorted(v) for k, v in (await eng._associative.entity_index(ns)).items()}


async def _reference_seeds(eng: Engine, ns: str, query: str) -> list[str]:
    """The pre-RM-110 ``_graph_seeds`` (no focal entity, no NER hook, no fallback): the
    query's n-grams looked up in a fresh namespace scan (``entity_index``)."""
    index = await _scan_index(eng, ns)
    if not index:
        return []
    words = match_key(query).split()
    seeds: list[str] = []
    for size in range(min(constants.GRAPH_SEED_MAX_NGRAM, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            for node in index.get(" ".join(words[start : start + size]), []):
                if node not in seeds:
                    seeds.append(node)
    return seeds


async def _assert_same(eng: Engine, ns: str, queries: list[str]) -> None:
    assert eng._associative is not None
    lookup = await eng._associative.entity_lookup(ns)
    assert await lookup.as_dict() == await _scan_index(eng, ns)
    for query in queries:
        assert await eng._graph_seeds(ns, query) == await _reference_seeds(eng, ns, query)


def _count_edge_list(monkeypatch: pytest.MonkeyPatch, graph: Any) -> list[str]:
    calls: list[str] = []
    real = graph.edge_list

    async def edge_list(*a: Any, **k: Any) -> Any:
        calls.append("edge_list")
        return await real(*a, **k)

    monkeypatch.setattr(graph, "edge_list", edge_list)
    return calls


async def _entity_engine(path: str = ":memory:", **extra: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}},
            **extra.pop("storage", {}),
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": True, "record_access": False},
        **extra,
    )
    await eng.start()
    return eng


# ------------------------------------------------------------------ the RM-93 world


async def test_rm93_world_seeds_identical_and_no_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    try:
        await _build(eng)
        queries = [QUERY, "Did Bram meet Dana", "chess club", "nothing named here"]
        await _assert_same(eng, NS, queries)
        calls = _count_edge_list(monkeypatch, eng._graph)
        for query in queries:
            await eng._graph_seeds(NS, query)
        assert calls == []  # the index is maintained: no namespace scan per read
        seeds = await eng._graph_seeds(NS, QUERY)
        assert seeds == [f"ent:{NS}:ilse", f"ent:{NS}:tomasz"]
        # a new mention lands in the cached index without a rescan
        await eng.write(
            "Ilse visited Quentin", namespace=NS, memory_type="semantic", entity="Quentin"
        )
        assert await eng._graph_seeds(NS, "who is Quentin") == [f"ent:{NS}:quentin"]
        assert calls == []
    finally:
        await eng.stop()


async def test_engine_search_identical_with_cache_and_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine.search end to end: the cached seed lookup and the scan give the same hits."""
    eng = await _engine()
    try:
        await _build(eng)
        cached = [(r.record_id, round(s, 6)) for r, s in await eng.search(QUERY, namespace=NS)]
        assert eng._associative is not None
        assoc = eng._associative

        async def scan_lookup(ns: str) -> Any:  # every read rebuilds from the scan
            entity_cache.invalidate(assoc._graph)
            return await entity_cache.entity_lookup(
                assoc._graph, None, ns, lambda: assoc.entity_index(ns)
            )

        monkeypatch.setattr(assoc, "entity_lookup", scan_lookup)
        scanned = [(r.record_id, round(s, 6)) for r, s in await eng.search(QUERY, namespace=NS)]
    finally:
        await eng.stop()
    assert cached and cached == scanned


# ------------------------------------------------------------------ randomized worlds

_NAMES = [
    "Alice",
    "Bob",
    "Charlotte's Web",
    "Charlotte s Web",
    "O'Brien",
    "O Brien",
    "o-brien",
    "@john",
    "John",
    "New York",
    "York",
    "Dr. Who",
    "Melanie",
]


@pytest.mark.parametrize("provider", ["sql_adjacency", "ladybug"])
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_randomized_world_seeds_identical(seed: int, provider: str, tmp_path: Path) -> None:
    if provider == "ladybug":
        pytest.importorskip("ladybug")
    rng = random.Random(seed)
    eng = await _entity_engine(str(tmp_path / "m.db"), storage={"graph": {"provider": provider}})
    other = "b"
    written: dict[str, list[str]] = {NS: [], other: []}
    queries = [
        "what about charlotte s web and john",
        "o brien moved to new york",
        "did alice see dr who",
        "melanie and bob",
        "york",
    ]
    try:
        for step in range(60):
            ns = NS if rng.random() < 0.75 else other
            if written[ns] and rng.random() < 0.3:
                victim = written[ns].pop(rng.randrange(len(written[ns])))
                await eng.forget(victim, namespace=ns)
            else:
                name = rng.choice(_NAMES)
                tags = [f"dst:{rng.choice(_NAMES)}"] if rng.random() < 0.5 else []
                rec = await eng.write(
                    f"fact {step} about {name}",
                    namespace=ns,
                    memory_type="semantic",
                    entity=name,
                    tags=tags,
                )
                written[ns].append(rec.record_id)
            if step % 7 == 0:
                await _assert_same(eng, NS, queries)
        await _assert_same(eng, NS, queries)
        await _assert_same(eng, other, queries)
        assert any(len(v) > 1 for v in (await _scan_index(eng, NS)).values())  # variants met
        for query in queries:  # namespace isolation: no seed of the other namespace
            assert all(s.startswith(f"ent:{NS}:") for s in await eng._graph_seeds(NS, query))
    finally:
        await eng.stop()


async def test_forgotten_entity_never_seeds() -> None:
    eng = await _entity_engine()
    try:
        rec = await eng.write("Zelda plays", namespace=NS, memory_type="semantic", entity="Zelda")
        assert await eng._graph_seeds(NS, "zelda") == [f"ent:{NS}:zelda"]
        await eng.forget(rec.record_id, namespace=NS)
        assert await eng._graph_seeds(NS, "zelda") == []
        await _assert_same(eng, NS, ["zelda"])
    finally:
        await eng.stop()


async def test_erased_entity_never_seeds() -> None:
    """T5-04 erasure purges graph nodes directly (not through the projector): the purge
    drops the cached index, so the erased entity is gone from the next lookup."""
    eng = await _entity_engine(privacy={})
    try:
        keep = await eng.write("Yusuf cooks", namespace=NS, memory_type="semantic", entity="Yusuf")
        gone = await eng.write("Zelda plays", namespace=NS, memory_type="semantic", entity="Zelda")
        assert await eng._graph_seeds(NS, "zelda and yusuf") == [
            f"ent:{NS}:zelda",
            f"ent:{NS}:yusuf",
        ]
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        assert await eng._graph_seeds(NS, "zelda and yusuf") == [f"ent:{NS}:yusuf"]
        await _assert_same(eng, NS, ["zelda and yusuf"])
        assert keep.record_id
    finally:
        await eng.stop()


async def test_removed_mention_is_rechecked_not_rescanned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FORGET marks the entity suspect; the next lookup re-checks that entity alone."""
    eng = await _entity_engine()
    try:
        one = await eng.write("Ada codes", namespace=NS, memory_type="semantic", entity="Ada")
        await eng.write("Ada sings", namespace=NS, memory_type="semantic", entity="Ada")
        assert await eng._graph_seeds(NS, "ada") == [f"ent:{NS}:ada"]
        calls = _count_edge_list(monkeypatch, eng._graph)
        await eng.forget(one.record_id, namespace=NS)
        assert await eng._graph_seeds(NS, "ada") == [f"ent:{NS}:ada"]  # one live mention left
        assert calls == []
    finally:
        await eng.stop()


async def test_second_engine_on_the_store_is_seen(tmp_path: Path) -> None:
    """Another Engine (standing in for another process) writes and forgets on the same
    SQLite file: the durable projector mark tells the reader its cache is stale."""
    path = str(tmp_path / "rm110.db")
    reader = await _entity_engine(path)
    writer = await _entity_engine(path)
    try:
        rec = await writer.write("Pia rows", namespace=NS, memory_type="semantic", entity="Pia")
        assert await reader._graph_seeds(NS, "pia and quinn") == [f"ent:{NS}:pia"]
        await writer.write("Quinn rows", namespace=NS, memory_type="semantic", entity="Quinn")
        assert await reader._graph_seeds(NS, "pia and quinn") == [
            f"ent:{NS}:pia",
            f"ent:{NS}:quinn",
        ]
        await writer.forget(rec.record_id, namespace=NS)
        assert await reader._graph_seeds(NS, "pia and quinn") == [f"ent:{NS}:quinn"]
        await _assert_same(reader, NS, ["pia and quinn"])
    finally:
        await writer.stop()
        await reader.stop()


async def test_entity_leg_mentions_match_namespace_scan() -> None:
    """The entity leg's per-seed mention fetch equals the namespace-wide map on the seeds."""
    eng = await _entity_engine()
    try:
        for i, name in enumerate(["Ada", "Ada", "Bo", "Cy", "Ada"]):
            await eng.write(f"note {i}", namespace=NS, memory_type="semantic", entity=name)
        assert eng._associative is not None
        seeds = [f"ent:{NS}:ada", f"ent:{NS}:bo", f"ent:{NS}:nobody", "ent:b:ada"]
        full = await eng._associative.entity_mentions(NS)
        got = await eng._associative.mentions_of(NS, seeds)
    finally:
        await eng.stop()
    want = {s: sorted(full[s]) for s in seeds if s in full}
    assert {k: sorted(v) for k, v in got.items()} == want
    assert len(want[f"ent:{NS}:ada"]) == 3
