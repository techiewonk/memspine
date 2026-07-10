"""Lexical provider parity (Phase 5): sqlite_fts5 and tantivy satisfy the same
LexicalStore port. The same corpus + queries must yield equivalent top-k
MEMBERSHIP and the same exists/delete/clear semantics through both — the two are
interchangeable behind ``read.lexical_provider`` (BM25 *scores* differ in scale
between backends, so parity is asserted on ranking membership, not raw scores).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.lexical.base import LexicalStore
from memspine.services.lexical.sqlite_fts5 import SQLiteFTS5Lexical


def _rec(record_id: str, namespace: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
    )


async def _make_sqlite() -> SQLiteFTS5Lexical:
    from memspine.clients.sqlite import SQLiteClient
    from memspine.services.storage.sqlite.schema import metadata

    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return SQLiteFTS5Lexical(client)


async def _make_tantivy() -> LexicalStore:
    pytest.importorskip("tantivy")
    from memspine.services.lexical.tantivy import TantivyLexical

    return TantivyLexical()


#: Each provider factory, run through the identical parity assertions below.
_PROVIDERS: dict[str, Callable[[], Awaitable[Any]]] = {
    "sqlite_fts5": _make_sqlite,
    "tantivy": _make_tantivy,
}


async def _seed(store: LexicalStore) -> None:
    await store.index_many(
        [
            _rec("r1", "team/a", "the quick brown fox jumps over the lazy dog"),
            _rec("r2", "team/a", "oceans hold deep cold water and strange fish"),
            _rec("r3", "team/a", "a fox is a clever fox and the fox runs fast"),
            _rec("r4", "team/b", "the fox in namespace b must never leak across"),
        ]
    )


@pytest.mark.parametrize("provider", list(_PROVIDERS))
async def test_top1_and_membership_match_across_providers(provider: str) -> None:
    store = await _PROVIDERS[provider]()
    try:
        await _seed(store)
        hits = await store.search("team/a", "fox", top_k=8)
        ids = [h.record_id for h in hits]
        # r3 mentions "fox" three times → unambiguous BM25 top-1 on both backends.
        assert ids[0] == "r3"
        # membership: both fox-bearing records in team/a surface, nothing from b.
        assert set(ids) == {"r1", "r3"}
        assert "r4" not in ids  # namespace isolation
    finally:
        await store.close()


@pytest.mark.parametrize("provider", list(_PROVIDERS))
async def test_namespace_isolation(provider: str) -> None:
    store = await _PROVIDERS[provider]()
    try:
        await _seed(store)
        b_hits = await store.search("team/b", "fox", top_k=8)
        assert {h.record_id for h in b_hits} == {"r4"}  # only namespace b's record
    finally:
        await store.close()


@pytest.mark.parametrize("provider", list(_PROVIDERS))
async def test_exists_delete_clear_semantics(provider: str) -> None:
    store = await _PROVIDERS[provider]()
    try:
        await _seed(store)
        assert await store.exists("r1")
        await store.delete("r1")
        assert not await store.exists("r1")  # gone from the index (FORGET cascade)
        assert "r1" not in {h.record_id for h in await store.search("team/a", "fox", top_k=8)}

        await store.clear()
        assert not await store.exists("r3")  # whole index truncated (rebuild)
        assert await store.search("team/a", "fox", top_k=8) == []
    finally:
        await store.close()
