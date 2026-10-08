"""I4 (isolation review 2026-10-08): one LanceDB table per namespace.

``vector.isolation: per_namespace`` routes every vector operation of a namespace to its
own table (``memspine_<embedder>__ns_<hash>``), so a search never touches another
user's rows, and a user's vectors can be dropped by dropping one table. The default
``shared`` keeps one table per embedder with a namespace prefilter (unchanged).

Operations that only know a ``record_id`` (``delete``, ``exists``, ``history_absent``)
fan out over every namespace table this database holds: erasure must find the row
wherever it is. Tables opened in an earlier process are discovered from the database's
table list, so nothing depends on in-process state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import xxhash

from memspine.services.vector.base import VectorHit
from memspine.services.vector.lancedb_store import LanceDBVectorStore, _table_name

__all__ = ["PerNamespaceVectorStore", "namespace_suffix"]


def namespace_suffix(namespace: str) -> str:
    """The table-name suffix of a namespace (hashed: namespaces may hold ``/`` or ``:``)."""
    return "__ns_" + xxhash.xxh64_hexdigest(namespace.encode())


class PerNamespaceVectorStore:
    """A vector store that keeps one :class:`LanceDBVectorStore` table per namespace."""

    def __init__(
        self, client: Any, embedder: Any, make: Callable[[str], LanceDBVectorStore]
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._make = make  # suffix -> store over that table
        self._stores: dict[str, LanceDBVectorStore] = {}
        self._lock = asyncio.Lock()

    def _store(self, namespace: str) -> LanceDBVectorStore:
        suffix = namespace_suffix(namespace)
        store = self._stores.get(suffix)
        if store is None:
            store = self._stores[suffix] = self._make(suffix)
        return store

    async def _all(self) -> list[LanceDBVectorStore]:
        """Every namespace table of this embedder in the database, opened lazily."""
        prefix = _table_name(self._embedder.embedder_id) + "__ns_"
        names = await asyncio.to_thread(lambda: list(self._client.db.table_names()))
        for name in names:
            if name.startswith(prefix):
                suffix = name[len(prefix) - len("__ns_") :]
                if suffix not in self._stores:
                    self._stores[suffix] = self._make(suffix)
        return list(self._stores.values())

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        await self._store(namespace).upsert(record_id, namespace, embedder_id, vector)

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        return await self._store(namespace).query(namespace, vector, embedder_id, top_k)

    async def query_many(
        self, namespace: str, vectors: list[list[float]], embedder_id: str, top_k: int = 8
    ) -> list[list[VectorHit]]:
        return await self._store(namespace).query_many(namespace, vectors, embedder_id, top_k)

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        return await self._store(namespace).search_rescore(namespace, vector, embedder_id, top_k)

    async def delete(self, record_id: str) -> None:
        for store in await self._all():
            await store.delete(record_id)

    async def delete_all(self) -> None:
        for store in await self._all():
            await store.delete_all()

    async def purge_deleted(self) -> bool:
        results = [await store.purge_deleted() for store in await self._all()]
        return all(results)

    async def history_absent(self, record_id: str) -> bool | None:
        verdicts = [await store.history_absent(record_id) for store in await self._all()]
        if any(v is False for v in verdicts):
            return False
        if any(v is None for v in verdicts):
            return None
        return True

    async def exists(self, record_id: str) -> bool:
        for store in await self._all():
            if await store.exists(record_id):
                return True
        return False

    async def drop_namespace(self, namespace: str) -> None:
        """Remove one namespace's whole table (erasure of a user's vectors)."""
        suffix = namespace_suffix(namespace)
        name = _table_name(self._embedder.embedder_id) + suffix
        self._stores.pop(suffix, None)
        names = await asyncio.to_thread(lambda: list(self._client.db.table_names()))
        if name in names:
            await asyncio.to_thread(self._client.db.drop_table, name)
