"""RM-72 open check: one Tantivy index shared by two Engines (ADR-079) and ``defer_commits``.

Deferral belongs to the caller that asked for it (one Engine's projection batch), not to
the shared handle: another Engine's write must still be committed before that Engine
advances its checkpoint, one Engine's flush must not end another's batch, and stopping
one Engine must neither drop nor wrongly require the other's held documents.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from memspine import Engine
from memspine.clients.tantivy import TantivyConnector
from memspine.providers import shared
from memspine.providers.shared import ResourceRegistry
from memspine.services.lexical.tantivy import TantivyLexical, tantivy_spec
from memspine.store.records import MemoryRecord


@pytest.fixture(autouse=True)
def _private_process_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


def _engine(db: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": True},
        runtime={"shared_resources": {"warmup": "lazy"}},
    )


def _record(text: str) -> MemoryRecord:
    return MemoryRecord(namespace="n", memory_type="episodic", content=text)


def _committed_docs(store: TantivyLexical) -> int:
    """Live documents in the last commit point (``meta.json``), never held documents.

    Read from disk rather than through a second ``tantivy.Index``: opening another
    index on the path garbage-collects the writer's uncommitted segment files."""
    assert store.connector.spec.path is not None
    meta = Path(store.connector.spec.path, "meta.json")
    if not meta.exists():
        return 0
    segments = json.loads(meta.read_text(encoding="utf-8"))["segments"]
    return sum(
        s["max_doc"] - ((s.get("deletes") or {}).get("num_deleted_docs") or 0) for s in segments
    )


async def test_deferral_is_scoped_to_the_caller_not_the_shared_handle(tmp_path: Path) -> None:
    store = TantivyLexical(TantivyConnector(tantivy_spec(tmp_path / "idx")))
    entered, release = asyncio.Event(), asyncio.Event()

    async def batch_a() -> None:
        store.defer_commits()
        await store.index(_record("heron by the lake"))
        entered.set()
        await release.wait()
        await store.flush()

    task = asyncio.create_task(batch_a())
    try:
        await entered.wait()
        assert _committed_docs(store) == 0  # held by A's batch
        # B is not batching: its document is committed before index() returns
        # (A's held document is committed with it, which is always safe).
        await store.index(_record("kayak on the river"))
        assert _committed_docs(store) == 2 and not store.connector.uncommitted
        release.set()
        await task
        assert len(await store.search("n", "heron kayak", top_k=5)) == 2
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await store.connector.close()


async def test_one_flush_does_not_end_another_callers_batch(tmp_path: Path) -> None:
    store = TantivyLexical(TantivyConnector(tantivy_spec(tmp_path / "idx")))
    b_in_batch, a_flushed = asyncio.Event(), asyncio.Event()
    held_in_b: list[int] = []

    async def batch_b() -> None:
        store.defer_commits()
        b_in_batch.set()
        await a_flushed.wait()
        await store.index(_record("otter in the reeds"))
        held_in_b.append(_committed_docs(store))
        await store.flush()

    task = asyncio.create_task(batch_b())
    try:
        await b_in_batch.wait()
        store.defer_commits()
        await store.index(_record("heron by the lake"))
        await store.flush()
        assert _committed_docs(store) == 1
        a_flushed.set()
        await task
        assert held_in_b == [1]  # A's flush did not end B's batch: otter was held
        assert _committed_docs(store) == 2
    finally:
        a_flushed.set()
        await asyncio.gather(task, return_exceptions=True)
        await store.connector.close()


async def test_nested_holds_end_with_the_outermost_flush(tmp_path: Path) -> None:
    store = TantivyLexical(TantivyConnector(tantivy_spec(tmp_path / "idx")))
    try:
        store.defer_commits()
        store.defer_commits()
        await store.index(_record("heron by the lake"))
        await store.flush()  # inner: commits what is held, the outer hold continues
        await store.index(_record("kayak on the river"))
        assert _committed_docs(store) == 1 and store.connector.uncommitted
        await store.flush()
        await store.index(_record("otter in the reeds"))  # no hold: committed at once
        assert _committed_docs(store) == 3
    finally:
        await store.connector.close()


async def test_two_engines_sharing_one_index(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    a = await _engine(db).start()
    b = await _engine(db).start()
    stopped_b = False
    a_wrote, b_done, a_wrote_again, release = (asyncio.Event() for _ in range(4))

    async def a_batch() -> None:
        async with a._projection_batch():
            await a.write("Pepper chased a heron at the lake.", namespace="chat")
            a_wrote.set()
            await b_done.wait()
            await a.write("Pepper found an otter in the reeds.", namespace="chat")
            a_wrote_again.set()
            await release.wait()

    task = asyncio.create_task(a_batch())
    try:
        # One connector (index and writer) per path per process; an adapter per Engine.
        assert a._lexical.connector is b._lexical.connector
        lexical = a._lexical
        assert isinstance(lexical, TantivyLexical)
        await a_wrote.wait()
        before = _committed_docs(lexical)
        assert lexical.connector.uncommitted  # A's heron is held by A's batch
        # B's write advances B's checkpoint at once, so it must be committed at once
        # (A's held heron goes into the same commit, which is always safe).
        await b.write("Mira paddled a kayak upriver.", namespace="chat")
        assert _committed_docs(lexical) == before + 2 and not lexical.connector.uncommitted
        # B stops while A's batch is open: the shared index stays open for A.
        await b.stop()
        stopped_b = True
        assert lexical.connector.is_open
        b_done.set()
        await a_wrote_again.wait()
        assert lexical.connector.uncommitted  # A's batch still holds its own document
        release.set()
        await task
        assert _committed_docs(lexical) == before + 3 and not lexical.connector.uncommitted
        hits = await lexical.search("chat", "heron kayak otter", top_k=5)
        assert len(hits) == 3
    finally:
        release.set()
        b_done.set()
        await asyncio.gather(task, return_exceptions=True)
        if not stopped_b:
            await b.stop()
        await a.stop()
