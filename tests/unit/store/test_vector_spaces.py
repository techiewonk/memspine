"""F-03: versioned vector spaces, shadow re-embed and switch, selective re-derivation."""

from __future__ import annotations

import math

import pytest

from memspine.config.schema import MemspineConfig
from memspine.config.schema.observability import TapConfig
from memspine.models.encoders.embedding.hash_local import HashEmbedding
from memspine.observability import tap
from memspine.store.records import MemoryRecord, RecordFingerprint
from memspine.store.vector_spaces import (
    JsonSpaceState,
    VectorSpace,
    VersionedSpaceStore,
    derivation_digest,
    rederive_plan,
    stale_derivations,
)


class _Hash(HashEmbedding):
    """A hash embedder with its own id, so two of them are two spaces."""

    def __init__(self, name: str, dim: int = 16) -> None:
        super().__init__(dim)
        self._name = name

    @property
    def embedder_id(self) -> str:
        return f"{self._name}:{self._dim}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # a different salt per model so the same text lands in a different place
        return await super().embed([f"{self._name} {t}" for t in texts])


class _Hit:
    def __init__(self, record_id: str, score: float) -> None:
        self.record_id = record_id
        self.score = score


class _MemBackend:
    """In-memory cosine backend; remembers the table name it was given."""

    def __init__(self, space: VectorSpace) -> None:
        self.space = space
        self.rows: dict[str, tuple[str, list[float]]] = {}
        self.embedder_ids: set[str] = set()
        self.query_dims: list[int] = []

    async def upsert(self, record_id, namespace, embedder_id, vector):
        self.embedder_ids.add(embedder_id)
        self.rows[record_id] = (namespace, vector)

    async def query(self, namespace, vector, embedder_id, top_k=8):
        self.query_dims.append(len(vector))
        scored = []
        for rid, (ns, vec) in self.rows.items():
            assert len(vec) == len(vector), "cross-space comparison"
            if ns == namespace:
                dot = sum(a * b for a, b in zip(vec, vector, strict=True))
                scored.append(_Hit(rid, dot / (math.hypot(*vec) * math.hypot(*vector) or 1.0)))
        return sorted(scored, key=lambda h: -h.score)[:top_k]

    async def delete(self, record_id):
        self.rows.pop(record_id, None)

    async def delete_all(self):
        self.rows.clear()

    async def exists(self, record_id):
        return record_id in self.rows


def _space(emb: _Hash, **kw) -> VectorSpace:
    return VectorSpace(model_id=emb.embedder_id, dim=emb.dim, **kw)


def _store(emb: _Hash, state=None):
    backends: dict[str, _MemBackend] = {}

    def factory(space: VectorSpace) -> _MemBackend:
        return backends.setdefault(space.id, _MemBackend(space))

    return VersionedSpaceStore(factory, _space(emb), state), backends


TEXTS = {"r1": "alpha beta", "r2": "gamma delta", "r3": "alpha gamma", "r4": "omega"}


async def _fill(store, emb, ns="u"):
    vecs = await emb.embed(list(TEXTS.values()))
    for (rid, _), vec in zip(TEXTS.items(), vecs, strict=True):
        await store.upsert(rid, ns, emb.embedder_id, vec)


def _source(items):
    def source(offset, limit):
        return items[offset : offset + limit]

    return source


# ------------------------------------------------------------------ identity


def test_space_id_covers_every_component_but_not_version():
    base = VectorSpace("m:1", 8)
    assert base.id == VectorSpace("m:1", 8, version=5).id
    for other in (
        VectorSpace("m:2", 8),
        VectorSpace("m:1", 9),
        VectorSpace("m:1", 8, dtype="float16"),
        VectorSpace("m:1", 8, query_instruction="Represent this query:"),
        VectorSpace("m:1", 8, normalisation="l2"),
    ):
        assert other.id != base.id
    assert base.cache_version != VectorSpace("m:1", 8, version=2).cache_version
    assert base.qualified_id.startswith("m:1@vs_")


# ------------------------------------------------------------------ routing


async def test_writes_are_tagged_and_reads_isolated_per_space():
    a, b = _Hash("a"), _Hash("b")
    store, backends = _store(a)
    await _fill(store, a)
    space_a = _space(a)
    assert store.active_space("u").id == space_a.id
    assert backends[space_a.id].embedder_ids == {space_a.qualified_id}

    store.set_current(_space(b))
    [vec] = await b.embed(["alpha"])
    await store.upsert("r9", "u", b.embedder_id, vec)
    space_b = _space(b)
    assert "r9" in backends[space_b.id].rows and "r9" not in backends[space_a.id].rows
    assert backends[space_b.id].embedder_ids == {space_b.qualified_id}
    # the active space is still a: reads see only a's rows
    [qa] = await a.embed(["alpha"])
    hits = await store.query("u", qa, a.embedder_id)
    assert {h.record_id for h in hits} == set(TEXTS)
    assert store.active_space("u").id == space_a.id


async def test_no_cross_space_comparison():
    a, b = _Hash("a"), _Hash("b", dim=24)  # different dims: a comparison would blow up
    store, backends = _store(a)
    await _fill(store, a)
    store.set_current(_space(b))
    [qb] = await b.embed(["alpha"])
    # a query from the new space against the old active space is refused, not compared
    assert await store.query("u", qb, b.embedder_id) == []
    # a query vector of the wrong dim under the right id is refused too
    assert await store.query("u", qb, a.embedder_id) == []
    assert all(be.query_dims == [] for be in backends.values())
    with pytest.raises(ValueError):
        await store.upsert("x", "u", a.embedder_id, qb)


async def test_unknown_namespace_and_embedder():
    a = _Hash("a")
    store, _ = _store(a)
    [q] = await a.embed(["alpha"])
    assert await store.query("nobody", q, a.embedder_id) == []
    with pytest.raises(ValueError):
        await store.upsert("x", "u", "ghost:1", q)


# ------------------------------------------------------------------ shadow re-embed


async def test_shadow_reembed_then_atomic_switch():
    a, b = _Hash("a"), _Hash("b")
    store, backends = _store(a)
    await _fill(store, a)
    store.set_current(_space(b))
    items = list(TEXTS.items())

    first = await store.reembed("u", _source(items), b, batch=3, budget=1)
    assert (first.batches, first.records, first.done, first.switched) == (1, 3, False, False)
    assert store.active_space("u").id == _space(a).id  # not switched yet
    assert store.in_flight("u")["offset"] == 3
    [qa] = await a.embed(["alpha"])
    assert {h.record_id for h in await store.query("u", qa, a.embedder_id)} == set(TEXTS)

    second = await store.reembed("u", _source(items), b, batch=3, budget=5)
    assert (second.records, second.done, second.switched) == (1, True, True)
    assert store.in_flight("u") is None
    assert store.active_space("u").id == _space(b).id
    assert set(backends[_space(b).id].rows) == set(TEXTS)

    # after the switch: the new space answers, the old embedder's queries are refused
    [qb] = await b.embed(["alpha beta"])
    hits = await store.query("u", qb, b.embedder_id)
    assert hits[0].record_id == "r1"
    assert await store.query("u", qa, a.embedder_id) == []
    # old space kept until dropped
    assert set(backends[_space(a).id].rows) == set(TEXTS)

    again = await store.reembed("u", _source(items), b)
    assert again.done and not again.switched and again.batches == 0


async def test_drop_space_refused_while_active_then_allowed():
    a, b = _Hash("a"), _Hash("b")
    store, backends = _store(a)
    await _fill(store, a)
    store.set_current(_space(b))
    assert not await store.drop_space(_space(a).id)  # still active
    assert not await store.drop_space(_space(b).id)  # current
    await store.reembed("u", _source(list(TEXTS.items())), b, batch=10)
    assert await store.drop_space(_space(a).id)
    assert [s.id for s in store.spaces()] == [_space(b).id]
    assert backends[_space(a).id].rows == {}


async def test_state_survives_restart_incl_midway_cursor(tmp_path):
    a, b = _Hash("a"), _Hash("b")
    path = str(tmp_path / "spaces.json")
    store, _ = _store(a, JsonSpaceState(path))
    await _fill(store, a)
    store.set_current(_space(b))
    await store.reembed("u", _source(list(TEXTS.items())), b, batch=2, budget=1)

    store2, _ = _store(b, JsonSpaceState(path))  # new process: current is now b
    assert store2.active_space("u").id == _space(a).id
    assert store2.in_flight("u")["offset"] == 2
    report = await store2.reembed("u", _source(list(TEXTS.items())), b, batch=2)
    assert report.switched and report.records == 2
    store3, _ = _store(b, JsonSpaceState(path))
    assert store3.active_space("u").id == _space(b).id


async def test_erasure_reaches_every_space():
    a, b = _Hash("a"), _Hash("b")
    store, _ = _store(a)
    await _fill(store, a)
    store.set_current(_space(b))
    await store.reembed("u", _source(list(TEXTS.items())), b, batch=10)
    assert await store.exists("r1")
    await store.delete("r1")
    assert not await store.exists("r1")


async def test_search_text_uses_the_active_spaces_embedder():
    a, b = _Hash("a"), _Hash("b", dim=24)
    store, _ = _store(a)
    await _fill(store, a)
    store.set_current(_space(b))
    hits = await store.search_text("u", "alpha beta", {a.embedder_id: a, b.embedder_id: b})
    assert hits[0].record_id == "r1"


# ------------------------------------------------------------------ tap events


async def test_tap_events():
    a, b = _Hash("a"), _Hash("b")
    tap.configure(TapConfig(enabled=True, sink="memory"))
    try:
        store, _ = _store(a)
        with tap.request("read"):
            await _fill(store, a)
            [q] = await a.embed(["alpha"])
            await store.query("u", q, a.embedder_id)
            store.set_current(_space(b))
            await store.reembed("u", _source(list(TEXTS.items())), b, batch=2)
        events = [e for t in tap.memory_traces() for e in t["events"]]
    finally:
        tap.shutdown()
    stages = [e["stage"] for e in events]
    assert stages.count("store.vector.space") == len(TEXTS) + 1
    assert stages.count("job.reembed") == 3 and stages.count("job.reembed") >= 3
    ends = [e for e in events if e["stage"] == "job.reembed" and e["fields"].get("phase") == "end"]
    assert ends[0]["fields"]["switched"] is True and ends[0]["fields"]["records"] == 4
    read = [e for e in events if e["fields"].get("op") == "read"]
    assert read[0]["fields"]["mismatch"] is False


# ------------------------------------------------------------------ re-derivation


def _rec(content, ns="u", memory_type="semantic", channel=None, **fp):
    rec = MemoryRecord(namespace=ns, memory_type=memory_type, content=content)
    if fp:
        rec = rec.model_copy(update={"fingerprint": RecordFingerprint(content="x", **fp)})
    return rec


def test_stale_derivations_listing_and_plan():
    current = ("qwen3.5-9b", "ph2")
    fresh = _rec("fresh", model_id="qwen3.5-9b", prompt_hash="ph2")
    old_model = _rec("old model", model_id="qwen2", prompt_hash="ph2")
    old_prompt = _rec("old prompt", model_id="qwen3.5-9b", prompt_hash="ph1")
    explicit = _rec("explicit", derivation=derivation_digest("qwen3.5-9b", "ph2"))
    legacy = _rec("legacy")
    legacy = legacy.model_copy(update={"fingerprint": RecordFingerprint()})
    raw = _rec("raw turn", memory_type="episodic")
    other_ns = _rec("elsewhere", ns="v", model_id="qwen2", prompt_hash="ph1")
    records = [fresh, old_model, old_prompt, explicit, legacy, raw, other_ns]

    report = stale_derivations(records, *current, namespace="u")
    assert set(report.stale_ids) == {old_model.record_id, old_prompt.record_id, legacy.record_id}
    assert report.total_derived == 5  # the raw turn and the other namespace are not counted
    assert report.by_model == {"qwen2": 1, "qwen3.5-9b": 1, "legacy": 1}
    assert report.by_prompt == {"ph2": 1, "ph1": 1, "legacy": 1}

    plan = rederive_plan(report, batch=2)
    assert [len(b) for b in plan.batches] == [2, 1] and plan.size == 3


async def test_rederive_plan_hook_and_budget():
    recs = [_rec(f"r{i}", model_id="old", prompt_hash="p") for i in range(5)]
    plan = rederive_plan(stale_derivations(recs, "new", "p"), batch=2)
    seen: list[list[str]] = []

    async def hook(ids):
        seen.append(ids)

    assert await plan.run(hook, budget=2) == 2
    assert sum(len(b) for b in seen) == 4
    assert await plan.run(lambda ids: seen.append(ids)) == 3


def test_rederive_plan_tap_event():
    tap.configure(TapConfig(enabled=True, sink="memory"))
    try:
        with tap.request("read"):
            stale_derivations([_rec("x", model_id="old", prompt_hash="p")], "new", "p")
        events = [e for t in tap.memory_traces() for e in t["events"]]
    finally:
        tap.shutdown()
    [ev] = [e for e in events if e["stage"] == "job.rederive.plan"]
    assert ev["fields"]["stale"] == 1 and ev["fields"]["total_derived"] == 1


# ------------------------------------------------------------------ flag off


def test_flag_off_by_default():
    assert MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).embedding.versioned_spaces is False


# ------------------------------------------------------------------ engine wiring (LanceDB)


async def test_engine_flag_on_matches_flag_off_and_names_table_by_space(tmp_path):
    pytest.importorskip("lancedb")
    from memspine import Engine

    def make(sub: str, on: bool) -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={'provider': "sqlite", 'settings': {'path': str(tmp_path / sub / "m.db"), 'busy_timeout_s': 5.0}},
            embedding={'provider': "hash", 'versioned_spaces': on, 'settings': {}},
            read={"record_access": False},
        )

    off, on = make("off", False), make("on", True)
    for eng in (off, on):
        await eng.start()
    try:
        for eng in (off, on):
            for text in ("camping by the lake", "skiing in the alps"):
                await eng.write(text, namespace="alice")
        on_vector = on._provider_results["vector"].service
        off_vector = off._provider_results["vector"].service
        assert isinstance(on_vector, VersionedSpaceStore)
        assert not isinstance(off_vector, VersionedSpaceStore)
        space = on_vector.active_space("alice")
        assert space is not None
        import lancedb

        db = lancedb.connect(str(tmp_path / "on" / "m.db.lance"))
        assert any(space.id in name for name in db.table_names())
        a = await off.search("camping lake", namespace="alice", top_k=5)
        b = await on.search("camping lake", namespace="alice", top_k=5)
        assert [r.content for r, _ in a] == [r.content for r, _ in b]
    finally:
        for eng in (off, on):
            await eng.stop()
