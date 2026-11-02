"""RM-160: one LanceDB write and one Tantivy commit per projection batch.

``ingest_messages`` projects its turns in one batch. The vector store holds the batch's
rows and writes them in one table ``add``; the lexical index holds its documents for one
commit. Reads inside or right after the batch still see every turn, a forget inside the
batch leaves no copy, and a crash between the batch's write and its checkpoint replays
from the log without duplicating a row.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import lancedb
import pytest

from memspine.api.engine import Engine
from memspine.clients.tantivy import TantivyConnector
from memspine.providers import shared
from memspine.providers.shared import ResourceRegistry
from memspine.services.vector.lancedb_store import LanceDBVectorStore

NS = "chat"
_TURNS = [
    {"role": "user", "content": f"Turn {i}: Pepper the beagle chased heron number {i * 7}."}
    for i in range(8)
]


@pytest.fixture(autouse=True)
def _private_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


def _engine(path: Path | str = ":memory:", template: str = "base") -> Engine:
    return Engine(
        template=template,
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
    )


def _store(engine: Engine) -> LanceDBVectorStore:
    store = engine._vector
    assert isinstance(store, LanceDBVectorStore)
    return store


class _Calls:
    """Counts native LanceDB table writes and Tantivy commits (no timing)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, table: Any) -> None:
        self.adds: list[int] = []
        self.merges: list[int] = []
        self.commits = 0
        cls = type(table)
        add, merge = cls.add, cls.merge_insert
        commit = TantivyConnector.commit

        def counted_add(this: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
            self.adds.append(len(data))
            return add(this, data, *args, **kwargs)

        def counted_merge(this: Any, *args: Any, **kwargs: Any) -> Any:
            self.merges.append(1)
            return merge(this, *args, **kwargs)

        def counted_commit(this: Any, *args: Any, **kwargs: Any) -> Any:
            self.commits += 1
            return commit(this, *args, **kwargs)

        monkeypatch.setattr(cls, "add", counted_add)
        monkeypatch.setattr(cls, "merge_insert", counted_merge)
        monkeypatch.setattr(TantivyConnector, "commit", counted_commit)


async def _vector_ids(engine: Engine, text: str) -> set[str]:
    assert engine._embedder is not None
    [vector] = await engine._embedder.embed([text])
    hits = await _store(engine).query(NS, vector, engine._embedder.embedder_id, top_k=50)
    return {hit.record_id for hit in hits}


async def _offsets(engine: Engine) -> dict[str, int]:
    assert engine._storage is not None
    return {p.name: await engine._storage.get_offset(p.name) for p in engine._projectors}


async def _head(engine: Engine) -> int:
    assert engine._storage is not None
    events = await engine._storage.read_events(after_seq=0, limit=1_000_000)
    return max(event.seq or 0 for event in events)


def _lance_ids(root: Path) -> list[str]:
    """Every record id in the latest version of every LanceDB table under ``root``."""
    ids: list[str] = []
    for lance_dir in root.rglob("*.lance"):
        if not lance_dir.is_dir():
            continue
        db = lancedb.connect(str(lance_dir))
        listed = db.list_tables()
        for name in getattr(listed, "tables", listed):
            ids.extend(str(v) for v in db.open_table(name).to_arrow().column("record_id"))
    return ids


@pytest.mark.parametrize("template", ["base", "assistant"])
async def test_an_n_record_ingest_writes_vectors_once_and_commits_lexical_once(
    monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    engine = _engine(template=template)
    await engine.start()
    try:
        await engine.write("warm the vector table", namespace=NS)
        store = _store(engine)
        calls = _Calls(monkeypatch, store._table)
        records = await engine.ingest_messages(_TURNS, namespace=NS, session_id="s")
        assert len(records) == len(_TURNS)
        assert calls.adds == [len(_TURNS)] and calls.merges == []
        assert calls.commits == 1
        assert store.held_rows == 0
    finally:
        await engine.stop()


async def test_reads_right_after_ingest_see_every_record() -> None:
    engine = _engine()
    await engine.start()
    try:
        records = await engine.ingest_messages(_TURNS, namespace=NS, session_id="s")
        ids = {r.record_id for r in records}
        assert ids <= await _vector_ids(engine, "Pepper the beagle chased a heron")
        assert engine._lexical is not None
        hits = await engine._lexical.search(NS, "Pepper beagle heron", top_k=50)
        assert ids <= {h.record_id for h in hits}
        served = {r.record_id for r, _ in await engine.search("beagle heron", namespace=NS)}
        assert served & ids
        head = await _head(engine)
        assert set((await _offsets(engine)).values()) == {head}
    finally:
        await engine.stop()


async def test_a_read_inside_the_batch_writes_the_held_rows_first() -> None:
    engine = _engine()
    await engine.start()
    try:
        store = _store(engine)
        async with engine._projection_batch():
            record = await engine.write("Pepper chased a heron at the lake.", namespace=NS)
            assert store.held_rows == 1
            assert record.record_id in await _vector_ids(engine, "Pepper chased a heron")
            assert store.held_rows == 0 and await store.exists(record.record_id)
            assert set((await _offsets(engine)).values()) == {0}  # marks wait for the flush
        head = await _head(engine)
        assert set((await _offsets(engine)).values()) == {head}
    finally:
        await engine.stop()


async def test_a_task_started_in_the_batch_stops_holding_when_it_ends() -> None:
    """A long-lived task spawned inside the batch (a background drain) copies its hold;
    after the batch flushes, that task's writes are written before its checkpoint."""
    engine = _engine()
    await engine.start()
    try:
        store = _store(engine)
        assert engine._lexical is not None
        lexical = engine._lexical
        batch_done, wrote = asyncio.Event(), asyncio.Event()
        held: list[tuple[int, bool]] = []

        async def background() -> None:
            await batch_done.wait()
            await engine.write("An otter slid into the reeds.", namespace=NS)
            held.append((store.held_rows, lexical.connector.uncommitted))  # type: ignore[attr-defined]
            wrote.set()

        async with engine._projection_batch():
            task = asyncio.create_task(background())
            await engine.write("Pepper chased a heron at the lake.", namespace=NS)
        batch_done.set()
        await wrote.wait()
        await task
        assert held == [(0, False)]
        head = await _head(engine)
        assert set((await _offsets(engine)).values()) == {head}
    finally:
        await engine.stop()


async def test_a_crash_between_the_batch_write_and_its_checkpoint_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "m.db"
    engine = _engine(db)
    await engine.start()
    assert engine._storage is not None
    real = engine._storage.set_offset

    async def crash(name: str, seq: int) -> None:
        raise RuntimeError("crash before the checkpoint")

    try:
        await engine.write("warm the vector table", namespace=NS)
        monkeypatch.setattr(engine._storage, "set_offset", crash)
        with pytest.raises(RuntimeError, match="crash before the checkpoint"):
            await engine.ingest_messages(_TURNS, namespace=NS, session_id="s")
        monkeypatch.setattr(engine._storage, "set_offset", real)
        store = _store(engine)
        assert store.held_rows == 0  # every projector still flushed its rows
        assert engine._lexical is not None
        assert not engine._lexical.connector.uncommitted  # type: ignore[attr-defined]
        behind = await _offsets(engine)
        head = await _head(engine)
        assert all(seq < head for seq in behind.values())
    finally:
        await engine.stop()
    # Restart: catch-up replays the tail past the stale marks (applies are idempotent).
    again = _engine(db)
    await again.start()
    try:
        head = await _head(again)
        assert set((await _offsets(again)).values()) == {head}
        records = await again._records(NS)
        ids = _lance_ids(tmp_path)
        assert len(ids) == len(set(ids))  # the replay merged, never duplicated a row
        assert {r.record_id for r in records} <= set(ids)
        hits = await again._lexical.search(NS, "Pepper beagle heron", top_k=50)  # type: ignore[union-attr]
        assert len(hits) == len(_TURNS)
    finally:
        await again.stop()


async def test_rows_lost_with_a_crash_before_the_batch_write_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "m.db"
    engine = _engine(db)
    await engine.start()
    try:
        await engine.write("warm the vector table", namespace=NS)
        store = _store(engine)

        async def crash(table: Any) -> None:
            raise RuntimeError("crash before the vector write")

        monkeypatch.setattr(store, "_write_pending", crash)
        with pytest.raises(RuntimeError, match="crash before the vector write"):
            await engine.ingest_messages(_TURNS, namespace=NS, session_id="s")
        offsets = await _offsets(engine)
        head = await _head(engine)
        assert offsets.pop("vectors") < head  # only the vector mark stays behind
        assert set(offsets.values()) == {head}
        store._pending.clear()  # the process dies: held rows are gone
        monkeypatch.undo()
    finally:
        await engine.stop()
    again = _engine(db)
    await again.start()
    try:
        head = await _head(again)
        assert set((await _offsets(again)).values()) == {head}
        ingested = {r.record_id for r in await again._records(NS) if r.content.startswith("Turn ")}
        assert len(ingested) == len(_TURNS)
        ids = _lance_ids(tmp_path)
        assert ingested <= set(ids) and len(ids) == len(set(ids))
    finally:
        await again.stop()


@pytest.mark.erasure_gate
@pytest.mark.parametrize("hard", [True, False])
async def test_a_forget_inside_the_batch_leaves_no_vector_or_lexical_copy(
    tmp_path: Path, hard: bool
) -> None:
    db = tmp_path / "m.db"
    engine = _engine(db)
    await engine.start()
    try:
        store = _store(engine)
        assert engine._lexical is not None
        async with engine._projection_batch():
            kept = await engine.write("Mira paddled a kayak upriver.", namespace=NS)
            gone = await engine.write("Zebulon Quartz lives in Vrbnik.", namespace=NS)
            assert store.held_rows >= 1  # the write's own neighbour query wrote the first
            await engine.forget(gone.record_id, namespace=NS, hard=hard)
        assert not await store.exists(gone.record_id)
        assert await store.exists(kept.record_id)
        if hard:
            assert await store.history_absent(gone.record_id) is True
        assert not await engine._lexical.exists(gone.record_id)  # type: ignore[attr-defined]
        assert await engine._lexical.exists(kept.record_id)  # type: ignore[attr-defined]
        served = {r.record_id for r, _ in await engine.search("Zebulon Vrbnik", namespace=NS)}
        assert gone.record_id not in served
    finally:
        await engine.stop()
    assert gone.record_id not in _lance_ids(tmp_path)
    if hard:  # no retained table version or file holds the erased text
        for lance_dir in tmp_path.rglob("*.lance"):
            if not lance_dir.is_dir():
                continue
            db_ = lancedb.connect(str(lance_dir))
            listed = db_.list_tables()
            for name in getattr(listed, "tables", listed):
                table = db_.open_table(name)
                for version in table.list_versions():
                    table.checkout(
                        version["version"] if isinstance(version, dict) else version.version
                    )
                    rows = table.to_arrow().column("record_id").to_pylist()
                    assert gone.record_id not in rows
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert b"vrbnik" not in path.read_bytes().lower(), path
