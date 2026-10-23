"""RM-96: the pushed-down qualified walk equals the shared walk (LadybugDB).

``neighbors`` / ``subgraph`` with a ``qualifier_filter`` on LadybugDB compile the
QualifierFilter into the WHERE clause of one query per level over the qualifier columns
(:mod:`memspine.services.graph.qualifier_columns`); the RM-96 hub caps, ordering and
tie-break run in the shared ``qualified_hops`` over the admitted rows. The reference is the
shared Python walk (``qualified_hops`` / ``qualified_subgraph`` over ``frontier_edges`` and
``QualifierFilter.admits``), which the store still runs when the pushdown plan does not
apply. Per edge, the compiled clause is also checked to select exactly what ``admits``
keeps.

Proved on randomized worlds (several seeds): two namespaces, hubs whose degree and first
hop exceed both caps, tied weights, tombstones, erased and erasure-pending edges, edges
without qualifiers, malformed qualifiers, self-loops, parallel edges, non-ASCII node ids,
timezone-shifted and precision-widened valid times; random filters over every
QualifierFilter field (plus each field alone, including empty sets); depth 1 and 2; with
and without ``max_degree``, ``rel_type`` and ``namespace``.
"""

from __future__ import annotations

import importlib.util
import os
import random
import re
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from memspine.services.graph import ladybug as ladybug_mod
from memspine.services.graph.qualifier_columns import (
    COLUMNS,
    QUALIFIER_COLUMNS_VERSION,
    compile_filter,
)
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    AssertionModality,
    ClaimState,
    Completeness,
    EdgeQualifiers,
    ErasureState,
    Polarity,
    Provenance,
    QualifierFilter,
    TimePrecision,
    edge_qualifiers,
)

pytest.importorskip("ladybug")

NAMESPACES = ("ns/a", "ns/b")
RELS = ("r0", "r1", "r2")
WEIGHTS = (0.2, 0.5, 0.5, 0.7, 0.9, 1.0, 1.0, 0.0, -0.4)  # ties, tombstones (<= 0)
SCOPES = ("ns/a", "ns/b", "team")
GRANTS = ("g1", "g2", "g3")
DOMAINS = ("health", "health/condition", "healthcare", "work", "work/project/x")
LABELS = ("health", "financial", "contact", "identity", "location")
ORIGINS = ("raw_message", "document", "email", "web_page")
STATUSES = ("proposed", "open", "closed", "cancelled")
OUTCOMES = ("done", "missed")
TIERS = ("untrusted", "low", "normal", "high")
PRINCIPALS = ("alice", "bob")
POOL = ("p", "q", "r")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
IST = timezone(timedelta(hours=5, minutes=30))
WORLD_SEEDS = (11, 23, 96)


def _node(i: int) -> str:
    return ("n", "nø", "n日")[i % 3] + f"{i:03d}"


def _maybe(rng: random.Random, values: tuple[Any, ...], p: float = 0.6) -> Any:
    return rng.choice(values) if rng.random() < p else None


def _when(rng: random.Random) -> datetime:
    return (T0 + timedelta(days=rng.randrange(-200, 200), hours=rng.randrange(24))).astimezone(
        rng.choice((UTC, IST))
    )


def _qualifiers(rng: random.Random) -> dict[str, Any] | None:
    """Random valid qualifiers as stored (JSON mode); None = an unqualified edge."""
    if rng.random() < 0.1:
        return None
    q: dict[str, Any] = {}
    if rng.random() < 0.75:
        q["valid_from"] = _when(rng)
        q["valid_precision"] = _maybe(rng, tuple(TimePrecision), 0.5)
        q["valid_approximate"] = _maybe(rng, (True, False), 0.4)
        end = rng.random()
        if end < 0.4:
            q["valid_open_ended"] = True
        elif end < 0.8:
            q["valid_to"] = q["valid_from"] + timedelta(days=rng.randrange(1, 120))
    q["known_at"] = _maybe(rng, tuple(_when(rng) for _ in range(3)), 0.7)
    q["assertion_modality"] = _maybe(rng, tuple(AssertionModality))
    q["polarity"] = _maybe(rng, tuple(Polarity))
    q["lifecycle_status"] = _maybe(rng, STATUSES)
    if q["lifecycle_status"] in (None, "closed"):
        q["lifecycle_outcome"] = _maybe(rng, OUTCOMES, 0.4)
    q["claim_state"] = _maybe(rng, tuple(ClaimState))
    if rng.random() < 0.5:
        q["trust"] = rng.choice((0.0, 0.2, 0.5, 0.75, 0.9, 1.0))
    else:
        q["trust_tier"] = _maybe(rng, TIERS, 0.4)
    q["binding_status"] = _maybe(rng, ("bound", "partly", "unbound"))
    q["subject_binding"] = _maybe(rng, POOL)
    q["perspective"] = _maybe(rng, POOL)
    q["scope"] = _maybe(rng, SCOPES)
    if rng.random() < 0.6:
        q["grants"] = tuple(g for g in GRANTS if rng.random() < 0.5)
    q["erasure_state"] = rng.choice(
        (None, ErasureState.ACTIVE, ErasureState.ACTIVE, ErasureState.ERASED, "erasure_pending")
    )
    q["event_role"] = _maybe(rng, POOL)
    q["occurrence_id"] = _maybe(rng, POOL)
    q["vocabulary_pack"] = _maybe(rng, POOL)
    q["topical_domain"] = _maybe(rng, DOMAINS)
    checked = rng.choice((None, True, True, False))
    if checked is not None:
        q["sensitivity_checked"] = checked
        q["sensitivity"] = frozenset(label for label in LABELS if rng.random() < 0.3)
    if rng.random() < 0.6:
        q["provenance"] = Provenance(
            principal=_maybe(rng, PRINCIPALS),
            origin_type=_maybe(rng, ORIGINS),
            source_id=_maybe(rng, POOL),
            lineage_root=_maybe(rng, POOL),
        )
    q["context_location"] = _maybe(rng, POOL)
    q["participation_mode"] = _maybe(rng, POOL)
    q["applicability_condition"] = _maybe(rng, POOL)
    q["extraction_confidence"] = _maybe(rng, (0.1, 0.5, 0.8, 1.0))
    q["extraction_method"] = _maybe(rng, POOL)
    q["recurrence_series_id"] = _maybe(rng, POOL)
    q["completeness"] = _maybe(rng, tuple(Completeness))
    q["independent_support_count"] = _maybe(rng, (0, 1, 2, 5))
    model = EdgeQualifiers.model_validate({k: v for k, v in q.items() if v is not None})
    return model.to_properties()


def _world(seed: int) -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """``(src, dst, rel, namespace, properties)``; nodes 0..2 are hubs."""
    rng = random.Random(seed)
    nodes = 70
    out: dict[tuple[str, str, str], tuple[str, str, str, str, dict[str, Any]]] = {}
    while len(out) < 420:
        a = rng.randrange(3) if rng.random() < 0.35 else rng.randrange(nodes)
        b = rng.randrange(3) if rng.random() < 0.2 else rng.randrange(nodes)
        if a == b and rng.random() < 0.9:
            continue  # a few self-loops stay
        rel = rng.choice(RELS)
        props: dict[str, Any] = {"weight": rng.choice(WEIGHTS)}
        quals = _qualifiers(rng)
        if quals is not None:
            props[QUALIFIERS_PROP] = quals
        elif rng.random() < 0.2:
            props[QUALIFIERS_PROP] = {"valid_from": "not a time"}  # malformed: dropped
        ns = NAMESPACES[0] if rng.random() < 0.8 else NAMESPACES[1]
        out[(_node(a), _node(b), rel)] = (_node(a), _node(b), rel, ns, props)
    return list(out.values())


def _times(rng: random.Random) -> datetime:
    return _when(rng) + timedelta(microseconds=rng.choice((0, -1, 1)))


def _field_values(rng: random.Random) -> dict[str, Callable[[], Any]]:
    """Every QualifierFilter field -> a random value (sets may be empty)."""

    def subset(values: tuple[Any, ...]) -> Callable[[], frozenset[Any]]:
        return lambda: frozenset(v for v in values if rng.random() < 0.45)

    def window() -> tuple[datetime, datetime]:
        start = _times(rng)
        return start, start + timedelta(days=rng.choice((1, 10, 90)))

    return {
        "valid_at": lambda: _times(rng),
        "valid_window": window,
        "known_as_of": lambda: _times(rng),
        "assertion_modalities": subset(tuple(AssertionModality)),
        "polarities": subset(tuple(Polarity)),
        "lifecycle_statuses": subset(STATUSES),
        "lifecycle_outcomes": subset(OUTCOMES),
        "claim_states": subset(tuple(ClaimState)),
        "principals": subset(PRINCIPALS),
        "origin_types": subset(ORIGINS),
        "source_ids": subset(POOL),
        "lineage_roots": subset(POOL),
        "min_trust": lambda: rng.choice((0.0, 0.3, 0.5, 0.75, 1.0)),
        "trust_tiers": subset(TIERS),
        "binding_statuses": subset(("bound", "partly", "unbound")),
        "subject_bindings": subset(POOL),
        "perspectives": subset(POOL),
        "scopes": subset(SCOPES),
        "required_grants": subset(GRANTS),
        "erasure_states": subset(tuple(ErasureState)),
        "event_roles": subset(POOL),
        "occurrence_ids": subset(POOL),
        "vocabulary_packs": subset(POOL),
        "topical_domains": subset(("health", "work", "healthcare", "work/project")),
        "sensitivities": subset(LABELS),
        "context_locations": subset(POOL),
        "participation_modes": subset(POOL),
        "applicability_conditions": subset(POOL),
        "min_extraction_confidence": lambda: rng.choice((0.0, 0.5, 0.9)),
        "extraction_methods": subset(POOL),
        "recurrence_series_ids": subset(POOL),
        "completeness": subset(tuple(Completeness)),
        "min_independent_support": lambda: rng.choice((0, 1, 3)),
        "exclude_sensitivities": subset(LABELS),
        "exclude_claim_states": subset(tuple(ClaimState)),
        "exclude_lifecycle_statuses": subset(STATUSES),
        "exclude_assertion_modalities": subset(tuple(AssertionModality)),
        "exclude_topical_domains": subset(("health", "work/project")),
        "exclude_origin_types": subset(ORIGINS),
        "unchecked_sensitivity": lambda: rng.choice(("pass", "fail")),
    }


def test_every_filter_field_is_generated() -> None:
    assert set(_field_values(random.Random(0))) == set(QualifierFilter.model_fields)


def _filters(seed: int, count: int) -> list[QualifierFilter]:
    """The empty filter, each field alone, then random 1-4 field combinations."""
    rng = random.Random(seed)
    values = _field_values(rng)
    out = [QualifierFilter()]
    for field, make in values.items():
        out.append(QualifierFilter(**{field: make()}))
    while len(out) < count:
        fields = rng.sample(sorted(values), rng.randint(1, 4))
        out.append(QualifierFilter(**{f: values[f]() for f in fields}))
    return out


LIVE_NEO4J = bool(os.environ.get("MEMSPINE_TEST_NEO4J_URL")) and (
    importlib.util.find_spec("neo4j") is not None
)


class _Backend:
    """One adapter under test: its store, how to force its shared-walk fallback (the
    reference) and how to forbid it, and how to select edges with a compiled clause."""

    name: str
    store: Any

    def reference(self, mp: pytest.MonkeyPatch) -> None:
        raise NotImplementedError

    def forbid(self, mp: pytest.MonkeyPatch) -> None:
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the pushed-down walk fell back to the shared walk")

        # the shared walk reads its levels through frontier_edges; the pushdown never does
        mp.setattr(type(self.store), "frontier_edges", boom)

    async def select(self, qf: QualifierFilter) -> list[tuple[str, str, str]]:
        raise NotImplementedError


class _Ladybug(_Backend):
    name = "ladybug"

    def __init__(self, store: Any) -> None:
        self.store = store

    def reference(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr(ladybug_mod, "_walk_plan", lambda *a, **k: None)

    async def select(self, qf: QualifierFilter) -> list[tuple[str, str, str]]:
        params: dict[str, Any] = {}
        where = compile_filter(qf, "e", params)
        assert where is not None
        rows = await self.store._execute(
            f"{ladybug_mod._EDGE_MATCH} WHERE {where} {ladybug_mod._EDGE_RETURN}", params
        )
        return sorted((str(r[0]), str(r[1]), str(r[2])) for r in rows)


class _Neo4j(_Backend):
    name = "neo4j"

    def __init__(self, store: Any, executor: Any) -> None:
        self.store = store
        self.executor = executor

    def reference(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr(self.executor, "compiles_qualifier_filters", False, raising=False)

    async def select(self, qf: QualifierFilter) -> list[tuple[str, str, str]]:
        params: dict[str, Any] = {}
        where = compile_filter(qf, "r", params)
        assert where is not None
        label, rel = self.executor._fmt["N"], self.executor._fmt["R"]
        query = (
            f"MATCH (a:{label})-[r:{rel}]->(b:{label}) WHERE {where} "
            "RETURN a.id AS s, b.id AS d, r.rel_type AS t"
        )
        async with self.executor._driver.session(database=self.executor._database) as session:
            result = await session.run(query, params)
            rows = [record async for record in result]
        return sorted((str(r["s"]), str(r["d"]), str(r["t"])) for r in rows)


async def _ladybug_backend() -> AsyncIterator[_Backend]:
    from memspine.clients.ladybug import LadybugClient, LadybugSpec

    client = LadybugClient(LadybugSpec(":memory:"))
    await client.open()
    yield _Ladybug(ladybug_mod.LadybugGraphStore(client))
    await client.close()


async def _neo4j_backend() -> AsyncIterator[_Backend]:
    if not LIVE_NEO4J:
        pytest.skip("needs the neo4j driver and MEMSPINE_TEST_NEO4J_URL (a live server)")
    from memspine.services.graph.neo4j import Neo4jGraphStore

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wiring"))
    from test_f_16e_neo4j_outbox import live_executor  # type: ignore[import-not-found]

    connector, executor = await live_executor("Rm96")
    try:
        yield _Neo4j(Neo4jGraphStore(executor, scoped=True), executor)
    finally:
        await executor.run("clear", {})
        await connector.close()


@pytest.fixture(params=["ladybug", "neo4j"])
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[_Backend]:
    factory = _ladybug_backend if request.param == "ladybug" else _neo4j_backend
    async for item in factory():
        yield item


@pytest.fixture
async def store() -> AsyncIterator[Any]:
    async for item in _ladybug_backend():
        yield item.store


async def _load(store: Any, world: list[tuple[str, str, str, str, dict[str, Any]]]) -> None:
    for i in range(80):
        await store.upsert_node(_node(i), ["entity"], {"i": i}, namespace=NAMESPACES[0])
    for src, dst, rel, ns, props in world:
        await store.upsert_edge(src, dst, rel, props, namespace=ns)


class _Reference:
    """Runs the store's shared-walk fallback (the pre-RM-96 path) as the reference."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, backend: _Backend | None = None) -> None:
        self._mp = monkeypatch
        self._backend = backend

    async def __call__(self, coro_fn: Callable[[], Any]) -> Any:
        with self._mp.context() as mp:
            if self._backend is None:
                mp.setattr(ladybug_mod, "_walk_plan", lambda *a, **k: None)
            else:
                self._backend.reference(mp)
            return await coro_fn()


def _node_key(nodes: list[Any]) -> list[tuple[str, tuple[str, ...], dict[str, object]]]:
    return [(n.node_id, n.labels, n.properties) for n in nodes]


def _edge_key(edges: list[Any]) -> list[tuple[str, str, str, dict[str, object]]]:
    return [(e.src, e.dst, e.rel_type, e.properties) for e in edges]


@pytest.mark.parametrize("world_seed", WORLD_SEEDS)
async def test_compiled_filter_admits_exactly_what_matches_admits(
    backend: _Backend, world_seed: int
) -> None:
    """Per edge: the compiled WHERE clause selects exactly the edges ``admits`` keeps."""
    world = _world(world_seed)
    await _load(backend.store, world)
    edges = await backend.store.edge_list()
    for qf in _filters(world_seed, 140):
        want = sorted((e.src, e.dst, e.rel_type) for e in edges if qf.admits(e))
        assert await backend.select(qf) == want, qf


@pytest.mark.parametrize("world_seed", WORLD_SEEDS)
async def test_pushed_down_walk_equals_the_shared_walk(
    backend: _Backend, world_seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = backend.store
    world = _world(world_seed)
    await _load(store, world)
    rng = random.Random(world_seed + 1)
    reference = _Reference(monkeypatch, backend)
    starts = [
        _node(0),
        _node(1),
        _node(2),
        "missing",
        *(_node(rng.randrange(70)) for _ in range(5)),
    ]
    hub_level_one = 0

    async def check(
        seed: str,
        depth: int,
        max_degree: int | None,
        rel: str | None,
        ns: str | None,
        qf: QualifierFilter,
        seeds: list[str],
    ) -> int:
        def walk() -> Any:
            return store.neighbors(
                seed,
                rel_type=rel,
                depth=depth,
                namespace=ns,
                max_degree=max_degree,
                qualifier_filter=qf,
            )

        def sub() -> Any:
            return store.subgraph(
                seeds, depth=depth, namespace=ns, max_degree=max_degree, qualifier_filter=qf
            )

        want = await reference(walk)
        want_edges = await reference(sub)
        with monkeypatch.context() as mp:
            backend.forbid(mp)
            got = await walk()
            got_edges = await sub()
        assert _node_key(got) == _node_key(want), (seed, depth, max_degree, rel, ns, qf)
        assert _edge_key(got_edges) == _edge_key(want_edges), (seeds, depth, max_degree, qf)
        return len(want)

    # every hub at depth 1 and 2 under the empty filter and an erasure-only filter
    erasure = QualifierFilter(erasure_states=frozenset({ErasureState.ACTIVE}))
    for hub in starts[:3]:
        for depth in (1, 2):
            for qf in (QualifierFilter(), erasure):
                reached = await check(hub, depth, None, None, NAMESPACES[0], qf, [hub, _node(9)])
                hub_level_one += depth == 1 and reached > 16
    assert hub_level_one > 0, "no walk exercised a hub above the caps"
    for qf in _filters(world_seed + 2, 70):
        for _ in range(2):
            await check(
                rng.choice(starts),
                rng.choice((1, 2)),
                rng.choice((None, None, 2, 5, 40)),
                rng.choice((None, None, "r0")),
                rng.choice((NAMESPACES[0], NAMESPACES[0], None)),
                qf,
                rng.sample(starts, rng.randint(1, 3)),
            )


async def test_hub_depth_two_applies_both_caps(
    backend: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hub whose first hop (20 nodes) and every first-hop degree (21) exceed the caps:
    the pushed-down walk and the shared walk reach the same capped set, strongest first."""
    store = backend.store
    hub = "hub"
    for i in range(20):
        mid = f"m{i:02d}"
        await store.upsert_edge(
            hub, mid, "r", {"weight": round(0.2 + (i % 7) * 0.1, 2)}, namespace="h"
        )
        for j in range(20):
            await store.upsert_edge(
                mid,
                f"x{i:02d}_{j:02d}",
                "r",
                {"weight": round(0.1 + (j % 5) * 0.2, 2)},
                namespace="h",
            )
    qf = QualifierFilter()
    reference = _Reference(monkeypatch, backend)
    for depth in (1, 2):

        def walk(depth: int = depth) -> Any:
            return store.neighbors(hub, depth=depth, namespace="h", qualifier_filter=qf)

        want = await reference(walk)
        with monkeypatch.context() as mp:
            backend.forbid(mp)
            got = await walk()
        assert _node_key(got) == _node_key(want)
    assert 20 < len(want) <= 20 + 16 * 16  # level 1 whole; level 2 capped


async def test_one_filtered_query_per_level(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``neighbors`` runs one query per level, node rows included (depth 1: one query, was
    a level query plus a node query; depth 2: two, was three); ``subgraph`` adds one
    filtered query for the induced edges. Every query carries the compiled filter."""
    await _load(store, _world(7))
    calls: list[str] = []
    real = store._execute

    async def counting(query: str, parameters: Any = None) -> Any:
        calls.append(query)
        return await real(query, parameters)

    monkeypatch.setattr(store, "_execute", counting)
    qf = QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED}))
    for depth in (1, 2):
        calls.clear()
        await store.neighbors(_node(0), depth=depth, namespace=NAMESPACES[0], qualifier_filter=qf)
        assert len(calls) == depth
        calls.clear()
        await store.subgraph([_node(0), _node(5)], depth=depth, qualifier_filter=qf)
        assert len(calls) == depth + 1
        assert all("q_erasure_state" in query for query in calls)


async def test_fallbacks_keep_the_shared_walk(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Depth 3 and a zero cap run the shared walk (the plan does not apply)."""
    await _load(store, _world(5))
    qf = QualifierFilter()
    assert ladybug_mod._walk_plan(qf, ["a"], 3, None, None, None) is None
    assert ladybug_mod._walk_plan(qf, ["a"], 2, None, None, 0) is None
    assert ladybug_mod._walk_plan(qf, [], 1, None, None, None) is None
    reference = _Reference(monkeypatch)
    want = await reference(lambda: store.neighbors(_node(0), depth=3, qualifier_filter=qf))
    assert _node_key(await store.neighbors(_node(0), depth=3, qualifier_filter=qf)) == _node_key(
        want
    )


async def test_stale_columns_are_backfilled_and_never_walked_meanwhile(tmp_path: Any) -> None:
    """Edges whose qualifier columns are missing (a database from before RM-96, or written
    by an older process) are re-derived on open; until then the single query skips them."""
    from memspine.clients.ladybug import LadybugClient, LadybugSpec

    path = str(tmp_path / "g.lbug")
    client = LadybugClient(LadybugSpec(path))
    await client.open()
    store = ladybug_mod.LadybugGraphStore(client)
    erased = {QUALIFIERS_PROP: {"erasure_state": "erased"}}
    await store.upsert_edge("a", "b", "r", {"weight": 1.0}, namespace="n")
    await store.upsert_edge("a", "c", "r", {"weight": 1.0, **erased}, namespace="n")
    await store._execute("MATCH ()-[e:MemoryLink]->() SET e.q_v = NULL, e.q_erasure_state = NULL")
    qf = QualifierFilter()
    assert await store.neighbors("a", namespace="n", qualifier_filter=qf) == []  # fail closed
    await client.close()

    client = LadybugClient(LadybugSpec(path))
    await client.open()
    reopened = ladybug_mod.LadybugGraphStore(client)
    got = await reopened.neighbors("a", namespace="n", qualifier_filter=qf)
    assert [n.node_id for n in got] == ["b"]  # backfilled; the erased edge stays untraversed
    rows = await reopened._execute("MATCH ()-[e:MemoryLink]->() RETURN e.q_v")
    assert {r[0] for r in rows} == {QUALIFIER_COLUMNS_VERSION}
    await client.close()


def test_columns_cover_every_compiled_qualifier() -> None:
    """Each column the compiler reads is declared (so the DDL and upsert carry it)."""
    params: dict[str, Any] = {}
    full = {name: make() for name, make in _field_values(random.Random(3)).items()}
    where = compile_filter(QualifierFilter(**full), "e", params)
    assert where is not None
    used = set(re.findall(r"e\.(q_[a-z_]+)", where))
    assert used <= set(COLUMNS)
    assert edge_qualifiers({}) is None


# ------------------------------------------------------------------------------- Neo4j
@pytest.fixture
async def neo4j() -> AsyncIterator[_Neo4j]:
    async for item in _neo4j_backend():
        assert isinstance(item, _Neo4j)
        yield item


async def test_neo4j_one_query_per_walk(neo4j: _Neo4j, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neo4j runs the whole qualified walk as ONE ``qualified_walk`` query (depth 1 and 2,
    node rows included; ``subgraph`` also takes its induced edges from that query)."""
    store = neo4j.store
    await _load(store, _world(7))
    qf = QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED}))
    await store.neighbors(_node(0), depth=1, namespace=NAMESPACES[0], qualifier_filter=qf)
    ops: list[str] = []
    real = store._ex.run

    async def counting(op: str, params: Any) -> Any:
        ops.append(op)
        return await real(op, params)

    monkeypatch.setattr(store._ex, "run", counting)
    for depth in (1, 2):
        ops.clear()
        await store.neighbors(_node(0), depth=depth, namespace=NAMESPACES[0], qualifier_filter=qf)
        assert ops == ["qualified_walk"]
        ops.clear()
        await store.subgraph([_node(0), _node(5)], depth=depth, qualifier_filter=qf)
        assert ops == ["qualified_walk"]


async def test_neo4j_stale_properties_backfilled_and_never_walked_meanwhile(
    neo4j: _Neo4j,
) -> None:
    """Edges without qualifier properties (written before RM-96) are re-derived before the
    first pushed-down walk of a store; a stale edge is never traversed (fail closed)."""
    from memspine.services.graph.neo4j import Neo4jGraphStore

    store, executor = neo4j.store, neo4j.executor
    erased = {QUALIFIERS_PROP: {"erasure_state": "erased"}}
    await store.upsert_edge("a", "b", "r", {"weight": 1.0}, namespace="n")
    await store.upsert_edge("a", "c", "r", {"weight": 1.0, **erased}, namespace="n")
    label, rel = executor._fmt["N"], executor._fmt["R"]
    async with executor._driver.session(database=executor._database) as session:
        await session.run(
            f"MATCH (:{label})-[r:{rel}]->(:{label}) SET r.q_v = null, r.q_erasure_state = null"
        )
    qf = QualifierFilter()
    skipped = Neo4jGraphStore(executor, scoped=True)
    skipped._qualifier_columns_done = True  # as if a concurrent writer left stale edges
    assert await skipped.neighbors("a", namespace="n", qualifier_filter=qf) == []
    fresh = Neo4jGraphStore(executor, scoped=True)
    got = await fresh.neighbors("a", namespace="n", qualifier_filter=qf)
    assert [n.node_id for n in got] == ["b"]  # backfilled; the erased edge stays untraversed
    stale = await executor.run("qualifier_stale", {"v": QUALIFIER_COLUMNS_VERSION, "limit": 5})
    assert stale == []


async def test_neo4j_recorder_keeps_the_shared_walk() -> None:
    """An executor that does not run Cypher (the in-memory recorder) gets the shared walk."""
    from memspine.services.graph.neo4j import Neo4jGraphStore

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wiring"))
    from test_f_16e_neo4j_outbox import FakeNeo4j

    fake = FakeNeo4j()
    store = Neo4jGraphStore(fake, scoped=True)
    await store.upsert_edge("a", "b", "r", {"weight": 1.0}, namespace="n")
    got = await store.neighbors("a", depth=2, namespace="n", qualifier_filter=QualifierFilter())
    assert [n.node_id for n in got] == ["b"]
    assert "frontier_edges" in fake.calls
    assert "qualified_walk" not in fake.calls
