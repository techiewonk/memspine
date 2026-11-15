"""On-disk Tantivy ownership across close, cancellation and reopen."""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from memspine.clients.tantivy import TantivyConnector
from memspine.clients.tantivy import connector as tantivy_connector
from memspine.services.lexical.base import LexicalHit
from memspine.services.lexical.tantivy import TantivyLexical, tantivy_spec
from memspine.store.records import MemoryRecord


def _record(record_id: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace="n",
        memory_type="episodic",
        content=f"heron {record_id}",
    )


async def test_close_commits_held_docs_and_releases_index_for_reopen_and_delete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "idx"
    first = TantivyConnector(tantivy_spec(path))
    store = TantivyLexical(first)
    store.defer_commits()
    await store.index(_record("one"))
    assert first.uncommitted
    await first.close()
    assert not first.is_open and first._writer is None and not first.uncommitted
    with pytest.raises(RuntimeError, match="closed"):
        await first.open()

    second = TantivyConnector(tantivy_spec(path))
    reopened = TantivyLexical(second)
    try:
        assert await reopened.exists("one")
        await reopened.index(_record("two"))
        assert {hit.record_id for hit in await reopened.search("n", "heron")} == {"one", "two"}
    finally:
        await second.close()
    assert not second.is_open
    shutil.rmtree(path)  # Windows must see no connector-owned live index/writer handle.
    assert not path.exists()


async def test_cancelled_search_worker_finishes_before_close_releases_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "idx"
    connector = TantivyConnector(tantivy_spec(path))
    store = TantivyLexical(connector)
    await store.index(_record("one"))
    started, release = threading.Event(), threading.Event()
    original = store._search_unlocked

    def blocked(namespace: str, terms: list[str], top_k: int) -> list[LexicalHit]:
        started.set()
        assert release.wait(5), "test search worker was not released"
        return original(namespace, terms, top_k)

    monkeypatch.setattr(store, "_search_unlocked", blocked)
    search = asyncio.create_task(store.search("n", "heron"))
    close: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        close = asyncio.create_task(connector.close())
        await asyncio.sleep(0.05)
        assert not close.done(), "close released the index while a worker still used it"
    finally:
        release.set()
        if close is not None:
            await close
        else:
            await connector.close()
    assert not connector.is_open
    shutil.rmtree(path)


async def test_cancelled_write_worker_commits_before_close(tmp_path: Path) -> None:
    path = tmp_path / "idx"
    connector = TantivyConnector(tantivy_spec(path))
    store = TantivyLexical(connector)
    started, release = threading.Event(), threading.Event()
    add_doc = store._add_doc

    def blocked(record_id: str, namespace: str, content: str) -> None:
        started.set()
        assert release.wait(5), "test write worker was not released"
        add_doc(record_id, namespace, content)

    store._add_doc = blocked  # type: ignore[method-assign]
    write = asyncio.create_task(store.index(_record("one")))
    close: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        close = asyncio.create_task(connector.close())
        await asyncio.sleep(0.05)
        assert not close.done(), "close released the writer while a worker still used it"
    finally:
        release.set()
        if close is not None:
            await close
        else:
            await connector.close()
    assert not connector.is_open
    reopened = TantivyConnector(tantivy_spec(path))
    try:
        assert await TantivyLexical(reopened).exists("one")
    finally:
        await reopened.close()
    shutil.rmtree(path)


async def test_busy_join_failure_keeps_writer_for_a_later_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = TantivyConnector(tantivy_spec(tmp_path / "idx"))
    store = TantivyLexical(connector)
    await store.index(_record("one"))
    join = tantivy_connector.wait_merging_threads

    def still_busy(writer: object) -> None:
        raise RuntimeError("Already borrowed")

    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", still_busy)
    with pytest.raises(RuntimeError, match="Already borrowed"):
        await connector.close()
    assert connector.is_open and connector._writer is not None
    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", join)
    await connector.close()
    assert not connector.is_open and connector._writer is None


async def test_cancelled_close_waits_for_native_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "idx"
    connector = TantivyConnector(tantivy_spec(path))
    await TantivyLexical(connector).index(_record("one"))
    started, release = threading.Event(), threading.Event()
    join = tantivy_connector.wait_merging_threads

    def blocked_join(writer: object) -> None:
        started.set()
        assert release.wait(5), "test close worker was not released"
        join(writer)

    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", blocked_join)
    closing = asyncio.create_task(connector.close())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        closing.cancel()
        await asyncio.sleep(0.05)
        assert not closing.done(), "cancelled close returned while its worker held the writer"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not connector.is_open
    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", join)
    reopened = TantivyConnector(tantivy_spec(path))
    try:
        assert await TantivyLexical(reopened).exists("one")
    finally:
        await reopened.close()
    shutil.rmtree(path)


async def test_double_cancel_keeps_writer_owned_until_native_join_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "idx"
    connector = TantivyConnector(tantivy_spec(path))
    await TantivyLexical(connector).index(_record("one"))
    started, release = threading.Event(), threading.Event()
    join = tantivy_connector.wait_merging_threads

    def blocked_join(writer: object) -> None:
        started.set()
        assert release.wait(5), "test join was not released"
        join(writer)

    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", blocked_join)
    closing = asyncio.create_task(connector.close())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0.05)
        assert not closing.done(), "second cancel abandoned a native writer join"
        assert connector.is_open and connector._writer is not None
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not connector.is_open and connector._writer is None
    monkeypatch.setattr(tantivy_connector, "wait_merging_threads", join)
    reopened = TantivyConnector(tantivy_spec(path))
    try:
        assert await TantivyLexical(reopened).exists("one")
    finally:
        await reopened.close()
    shutil.rmtree(path)
