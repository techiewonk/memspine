"""RM-77 (ADR-079 consequences): multi-Engine acceptance for shared, keyed resources.

Real Engines (``template="core"``, hash embedder, temp SQLite files) prove: stopping one Engine
keeps another that shares its resources working; different settings get their own resources;
single-writer paths (SQLite, Tantivy, LanceDB) share one handle per path and both Engines see
each other's data; loop-bound clients are never shared across event loops; the registry resets
in a forked child; ``:memory:`` stores are never shared; results and caches never cross Engines,
namespaces or tenants.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.observability import bindings
from memspine.providers import shared
from memspine.providers.shared import ResourceKey, ResourceRegistry, shared_registry

#: the process registry the at-fork hook is registered on (before the fixture swaps it)
_PROCESS_REGISTRY = shared._REGISTRY

_SINGLE_WRITER = ("sql.sqlite", "sql.sqlite.sync", "lexical.tantivy", "vector.lancedb")


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Each test asserts on its own registry entries (see test_shared_connections)."""
    registry = ResourceRegistry()
    monkeypatch.setattr(shared, "_REGISTRY", registry)
    yield registry
    assert not bindings.live()  # every Engine below is stopped


def _engine(path: Path | str, **overrides: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        **overrides,
    )


def _refs() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for stats in shared_registry().stats():
        out.setdefault(stats.key.category, []).append(stats.refs)
    return {k: sorted(v) for k, v in out.items()}


async def _ids(eng: Engine, query: str, namespace: str) -> set[str]:
    return {r.record_id for r, _score in await eng.search(query, namespace=namespace)}


async def _vector_ids(eng: Engine, text: str, namespace: str) -> set[str]:
    assert eng._embedder is not None and eng._vector is not None
    vec = (await eng._embedder.embed([text]))[0]
    hits = await eng._vector.query(namespace, vec, eng._embedder.embedder_id)
    return {h.record_id for h in hits}


# --------------------------------------------------------------------------- stop one, keep other
async def test_stopping_one_engine_keeps_the_other_working(tmp_path: Path) -> None:
    a, b = _engine(tmp_path / "s.db"), _engine(tmp_path / "s.db")
    await a.start()
    await b.start()
    try:
        assert {c: _refs()[c] for c in _SINGLE_WRITER} == {c: [2] for c in _SINGLE_WRITER}
        first = await a.write("Ana likes green tea", namespace="n")
        await a.stop()
        assert {c: _refs()[c] for c in _SINGLE_WRITER} == {c: [1] for c in _SINGLE_WRITER}
        # b still reads (SQL, lexical and vector legs) and writes through the shared handles
        assert first.record_id in await _ids(b, "green tea", "n")
        assert first.record_id in await _vector_ids(b, "Ana likes green tea", "n")
        second = await b.write("Ben drinks black coffee", namespace="n")
        assert second.record_id in await _ids(b, "black coffee", "n")
    finally:
        await a.stop()
        await b.stop()
    assert _refs() == {}  # the last holder closed every shared resource


# --------------------------------------------------------------------------- isolation
async def test_different_settings_get_their_own_resources(tmp_path: Path) -> None:
    base = _engine(tmp_path / "a.db")
    other_path = _engine(tmp_path / "b.db")
    other_knobs = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "a.db"), "busy_timeout_s": 9.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": False},  # a's Tantivy writer stays the only one on its path
    )
    engines = (base, other_path, other_knobs)
    for eng in engines:
        await eng.start()
    try:
        assert base._sql is not None and other_path._sql is not None
        assert other_knobs._sql is not None
        assert base._sql.engine is not other_path._sql.engine  # another file
        assert base._sql.engine is not other_knobs._sql.engine  # same file, other pool knobs
        assert base._lexical is not other_path._lexical
        assert other_knobs._lexical is None
        assert base._vector._client is not other_path._vector._client  # type: ignore[union-attr]
        assert _refs()["sql.sqlite"] == [1, 1, 1]
        rid = (await base.write("only in a", namespace="n")).record_id
        assert rid not in await _ids(other_path, "only in a", "n")
    finally:
        for eng in engines:
            await eng.stop()


# --------------------------------------------------------------------------- single-writer paths
async def test_single_writer_paths_share_one_handle_and_see_each_others_data(
    tmp_path: Path,
) -> None:
    a = _engine(tmp_path / "w.db")
    b = _engine(tmp_path / "." / "w.db")  # another spelling of the same path
    await a.start()
    await b.start()
    try:
        assert a._sql is not None and b._sql is not None
        assert a._sql.engine is b._sql.engine
        # One Tantivy connector (writer) per path; an adapter per Engine.
        assert a._lexical.connector is b._lexical.connector  # type: ignore[union-attr]
        assert a._vector is not b._vector  # each Engine keeps its own store ...
        assert a._vector._client is b._vector._client  # type: ignore[union-attr]  # ... on one connection
        # b opens the vector table first, so its handle is on the empty version
        assert await _vector_ids(b, "warm up", "n") == set()
        from_a = (await a.write("Ana likes green tea", namespace="n")).record_id
        assert from_a in await _vector_ids(b, "Ana likes green tea", "n")  # RM-77 LanceDB fix
        assert from_a in await _ids(b, "green tea", "n")
        from_b = (await b.write("Ben drinks black coffee", namespace="n")).record_id
        assert from_b in await _vector_ids(a, "Ben drinks black coffee", "n")
        assert from_b in await _ids(a, "black coffee", "n")
        await b.forget(from_b, namespace="n", hard=True)
        assert from_b not in await _vector_ids(a, "Ben drinks black coffee", "n")
    finally:
        await a.stop()
        await b.stop()


# --------------------------------------------------------------------------- loop affinity
def test_engines_on_two_event_loops_share_only_loop_free_resources(tmp_path: Path) -> None:
    path = tmp_path / "loops.db"
    started = threading.Barrier(2, timeout=60)
    written = threading.Barrier(2, timeout=60)
    found: dict[int, Any] = {}
    errors: list[BaseException] = []

    def run(index: int) -> None:
        async def main() -> None:
            eng = _engine(path, runtime={"scheduler": True, "model_concurrency": 2})
            await eng.start()
            try:
                assert eng._sql is not None
                found[index] = {
                    "sql": eng._sql.engine,
                    "lexical": eng._lexical.connector,  # type: ignore[union-attr]
                    "client": eng._vector._client,  # type: ignore[union-attr]
                    "scheduler": eng._sched,
                }
                await asyncio.to_thread(started.wait)
                if index == 0:
                    found["rid"] = (await eng.write("Ana likes green tea", namespace="n")).record_id
                await asyncio.to_thread(written.wait)
                if index == 1:
                    found["seen"] = await _ids(eng, "green tea", "n")
                    found["seen_vector"] = await _vector_ids(eng, "Ana likes green tea", "n")
                await asyncio.to_thread(started.wait)  # both still started for the reads
            finally:
                await eng.stop()

        try:
            asyncio.run(main())
        except BaseException as exc:  # surfaced below
            errors.append(exc)
            started.abort()
            written.abort()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors, errors
    zero, one = found[0], found[1]
    assert zero["sql"] is not one["sql"]  # an async engine is bound to its loop
    assert zero["scheduler"] is not one["scheduler"]  # the model scheduler too
    assert zero["lexical"] is one["lexical"]  # thread-safe connectors are shared
    assert zero["client"] is one["client"]
    assert found["rid"] in found["seen"] and found["rid"] in found["seen_vector"]
    assert _refs() == {}


# --------------------------------------------------------------------------- fork reset
async def test_registry_reset_after_fork_forgets_and_rebuilds(tmp_path: Path) -> None:
    """Simulates the child side of a fork (the at-fork hook runs ``reset_after_fork``): the
    inherited entries are forgotten without being closed, and the next Engine builds its own."""
    parent = _engine(tmp_path / "f.db", read={"hybrid": False})
    await parent.start()
    child: Engine | None = None
    try:
        registry = shared_registry()
        assert registry.stats()
        registry.reset_after_fork()
        assert registry.stats() == ()
        assert parent._sql is not None
        async with parent._sql.engine.connect():  # nothing was closed by the reset
            pass
        child = _engine(tmp_path / "f.db", read={"hybrid": False})
        await child.start()
        assert child._sql is not None and child._sql.engine is not parent._sql.engine
        assert child._vector._client is not parent._vector._client  # type: ignore[union-attr]
        await parent.stop()  # releasing a forgotten entry closes only the parent's own handle
        rid = (await child.write("written after the reset", namespace="n")).record_id
        assert rid in await _ids(child, "after the reset", "n")
    finally:
        await parent.stop()
        if child is not None:
            await child.stop()
    assert _refs() == {}


async def test_a_key_from_another_process_is_refused() -> None:
    key = ResourceKey("probe", "x", "thread", os.getpid() + 1)

    async def build() -> object:
        return object()

    with pytest.raises(RuntimeError, match="another process"):
        await shared_registry().acquire(key, build)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX only")
def test_forked_child_starts_with_an_empty_registry() -> None:
    from memspine.providers.shared import resource_key

    async def build() -> object:
        return object()

    key = resource_key("probe.fork", {}, scope="thread")
    lease = asyncio.run(_PROCESS_REGISTRY.acquire(key, build))
    try:
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            os.close(read_fd)
            os.write(write_fd, str(len(_PROCESS_REGISTRY.stats())).encode())
            os._exit(0)
        os.close(write_fd)
        child_entries = os.read(read_fd, 16).decode()
        os.close(read_fd)
        os.waitpid(pid, 0)
        assert child_entries == "0"
        assert any(s.key.category == "probe.fork" for s in _PROCESS_REGISTRY.stats())
    finally:
        asyncio.run(lease.aclose())


def test_bindings_reset_after_fork_drops_live_engines() -> None:
    owner = type("Owner", (), {})()
    binding = bindings.EngineBinding()
    bindings.register(binding, owner)
    assert binding in bindings.live()
    bindings._reset_after_fork()
    assert bindings.live() == ()


# --------------------------------------------------------------------------- :memory:
async def test_memory_stores_are_never_shared() -> None:
    a = _engine(":memory:", observability={"ops_ledger": True})
    b = _engine(":memory:", observability={"ops_ledger": True})
    await a.start()
    await b.start()
    try:
        categories = set(_refs())
        assert not categories & set(_SINGLE_WRITER)  # nothing about :memory: is registered
        assert a._sql is not None and b._sql is not None
        assert a._sql.engine is not b._sql.engine
        assert a._lexical is not b._lexical
        assert a._vector._client is not b._vector._client  # type: ignore[union-attr]
        assert a._ops_ledger is not b._ops_ledger  # per-store seams stay per Engine too
        rid = (await a.write("private to a", namespace="n")).record_id
        assert rid in await _ids(a, "private to a", "n")
        assert await _ids(b, "private to a", "n") == set()
    finally:
        await a.stop()
        await b.stop()


# --------------------------------------------------------------------------- no result sharing
async def test_results_and_caches_never_cross_engines_namespaces_or_tenants(
    tmp_path: Path,
) -> None:
    cfg: dict[str, Any] = {"read": {"cache": {"enabled": True}}}
    a, b = _engine(tmp_path / "c.db", **cfg), _engine(tmp_path / "c.db", **cfg)
    await a.start()
    await b.start()
    try:
        assert a._cache is not b._cache  # embedding / extraction result caches per Engine
        assert a._embedder is not b._embedder
        await a.write("Ana likes green tea", namespace="tenant-a")
        await a.write("Ana likes green tea too", namespace="tenant-b")
        await a.read("green tea", namespace="tenant-a")
        cache_a = a.__dict__["_read_result_cache"]
        assert len(cache_a) == 1
        assert "_read_result_cache" not in b.__dict__  # nothing of a's reads reaches b
        await b.read("green tea", namespace="tenant-a")
        assert len(b.__dict__["_read_result_cache"]) == 1 and len(cache_a) == 1
        assert b.__dict__["_read_result_cache"] is not cache_a
        # another namespace (tenant) is another cache key, never a hit on tenant-a's entry
        other = await a.read("green tea", namespace="tenant-b")
        assert len(cache_a) == 2
        assert {r.namespace for r in other.context.records} <= {"tenant-b"}
        # a write by b on the shared store makes a's cached result stale (RM-77 fix)
        before = await a.read("green tea", namespace="tenant-a")
        added = await b.write("Ben likes green tea", namespace="tenant-a")
        after = await a.read("green tea", namespace="tenant-a")
        assert added.record_id not in {r.record_id for r in before.context.records}
        assert added.record_id in {r.record_id for r in after.context.records}
        await b.forget(added.record_id, namespace="tenant-a", hard=True)
        again = await a.read("green tea", namespace="tenant-a")
        assert added.record_id not in {r.record_id for r in again.context.records}
    finally:
        await a.stop()
        await b.stop()
