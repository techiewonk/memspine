"""OpenSearch BM25 index implementing the namespace-scoped lexical port."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memspine.config.constants import MAX_LEXICAL_QUERY_CHARS, MAX_LEXICAL_QUERY_TERMS
from memspine.services.lexical.base import LexicalHit
from memspine.services.remote_search import bounded_query, document_id, owner_scope
from memspine.store.records import MemoryRecord


class OpenSearchLexical:
    def __init__(self, client: Any, *, index: str, owner_id: str, request_timeout_s: float) -> None:
        self._client = client
        self._index = index
        self._owner_id = owner_id
        self._owner_scope = owner_scope(owner_id)
        self._timeout = request_timeout_s
        self._lock = asyncio.Lock()

    async def ensure_index(self) -> None:
        client = self._client.native
        if await client.indices.exists(index=self._index):
            return
        try:
            await client.indices.create(
                index=self._index,
                body={
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": {
                        "dynamic": "strict",
                        "properties": {
                            "record_id": {"type": "keyword"},
                            "namespace": {"type": "keyword"},
                            "owner_scope": {"type": "keyword"},
                            "content": {"type": "text"},
                        },
                    },
                },
            )
        except Exception:
            # Concurrent engine startup may race to create the same owned index.
            if not await client.indices.exists(index=self._index):
                raise

    async def index(self, record: MemoryRecord) -> None:
        await self._client.native.index(
            index=self._index,
            id=document_id(self._owner_id, record.record_id),
            body={
                "record_id": record.record_id,
                "namespace": record.namespace,
                "owner_scope": self._owner_scope,
                "content": record.content,
            },
            refresh="wait_for",
            request_timeout=self._timeout,
        )

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        if not records:
            return
        from opensearchpy.helpers import async_bulk

        async with self._lock:
            actions = (
                {
                    "_op_type": "index",
                    "_index": self._index,
                    "_id": document_id(self._owner_id, record.record_id),
                    "_source": {
                        "record_id": record.record_id,
                        "namespace": record.namespace,
                        "owner_scope": self._owner_scope,
                        "content": record.content,
                    },
                }
                for record in records
            )
            _count, errors = await async_bulk(
                self._client.native,
                actions,
                raise_on_error=False,
                refresh="wait_for",
                request_timeout=self._timeout,
            )
            if errors:
                raise RuntimeError("OpenSearch bulk indexing failed for one or more records")

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        if top_k <= 0:
            return []
        safe_query = bounded_query(
            query, max_chars=MAX_LEXICAL_QUERY_CHARS, max_terms=MAX_LEXICAL_QUERY_TERMS
        )
        if not safe_query:
            return []
        response = await self._client.native.search(
            index=self._index,
            body={
                "query": {
                    "bool": {
                        "must": [{"match": {"content": {"query": safe_query, "operator": "or"}}}],
                        "filter": [
                            {"term": {"namespace": namespace}},
                            {"term": {"owner_scope": self._owner_scope}},
                        ],
                    }
                },
                "sort": ["_score", {"record_id": "asc"}],
                "_source": ["record_id"],
            },
            size=min(top_k, 1000),
            request_timeout=self._timeout,
        )
        hits = response.get("hits", {}).get("hits", [])
        return [
            LexicalHit(str(hit["_source"]["record_id"]), float(hit.get("_score") or 0.0))
            for hit in hits
            if hit.get("_source", {}).get("record_id") is not None
        ]

    def _scope_query(self, **filters: str) -> dict[str, Any]:
        must: list[dict[str, Any]] = [{"term": {"owner_scope": self._owner_scope}}]
        must.extend({"term": {key: value}} for key, value in filters.items())
        return {"query": {"bool": {"filter": must}}}

    async def delete(self, record_id: str) -> None:
        await self._client.native.delete(
            index=self._index,
            id=document_id(self._owner_id, record_id),
            refresh="wait_for",
            ignore=[404],
            request_timeout=self._timeout,
        )

    async def exists(self, record_id: str) -> bool:
        return bool(
            await self._client.native.exists(
                index=self._index,
                id=document_id(self._owner_id, record_id),
                request_timeout=self._timeout,
            )
        )

    async def clear(self) -> None:
        response = await self._client.native.delete_by_query(
            index=self._index,
            body=self._scope_query(),
            refresh=True,
            conflicts="proceed",
            request_timeout=self._timeout,
        )
        if (
            response.get("failures")
            or response.get("version_conflicts", 0)
            or response.get("timed_out", False)
        ):
            raise RuntimeError("OpenSearch clear did not delete every owned document")
        remaining = await self._client.native.count(
            index=self._index,
            body=self._scope_query(),
            request_timeout=self._timeout,
        )
        if int(remaining.get("count", -1)) != 0:
            raise RuntimeError("OpenSearch clear could not verify owned documents were removed")

    async def close(self) -> None:
        return None
