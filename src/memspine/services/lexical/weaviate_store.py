"""Weaviate BM25 lexical projection with strict owner and namespace filters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memspine.config.constants import MAX_LEXICAL_QUERY_CHARS, MAX_LEXICAL_QUERY_TERMS
from memspine.services.lexical.base import LexicalHit
from memspine.services.weaviate_projection import (
    bounded_query,
    exact_filter,
    properties,
    record_uuid,
    upsert_object,
)
from memspine.store.records import MemoryRecord


class WeaviateLexicalStore:
    def __init__(self, collection: Any, *, owner_id: str) -> None:
        self._collection = collection
        self._owner_id = owner_id
        self._owner_scope = properties("", "", owner_id)["owner_scope"]

    async def index(self, record: MemoryRecord) -> None:
        await upsert_object(
            self._collection,
            owner_id=self._owner_id,
            record_id=record.record_id,
            props=properties(
                record.record_id, record.namespace, self._owner_id, content=record.content
            ),
        )

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        for record in records:
            await self.index(record)

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        if top_k <= 0:
            return []
        safe_query = bounded_query(
            query, max_chars=MAX_LEXICAL_QUERY_CHARS, max_terms=MAX_LEXICAL_QUERY_TERMS
        )
        if not safe_query:
            return []
        from weaviate.classes.query import MetadataQuery

        result = await self._collection.query.bm25(
            query=safe_query,
            query_properties=["content"],
            filters=exact_filter(namespace=namespace, owner_scope=self._owner_scope),
            limit=min(top_k, 1000),
            return_metadata=MetadataQuery(score=True),
            return_properties=["record_id"],
        )
        hits = [
            LexicalHit(str(obj.properties["record_id"]), float(obj.metadata.score or 0.0))
            for obj in result.objects
            if obj.properties.get("record_id") is not None
        ]
        return sorted(hits, key=lambda hit: (-hit.score, hit.record_id))[:top_k]

    async def delete(self, record_id: str) -> None:
        object_id = record_uuid(self._owner_id, record_id)
        obj = await self._collection.query.fetch_object_by_id(
            object_id, return_properties=["owner_scope"]
        )
        if obj is not None and obj.properties.get("owner_scope") == self._owner_scope:
            await self._collection.data.delete_by_id(object_id)

    async def exists(self, record_id: str) -> bool:
        obj = await self._collection.query.fetch_object_by_id(
            record_uuid(self._owner_id, record_id), return_properties=["owner_scope"]
        )
        return obj is not None and obj.properties.get("owner_scope") == self._owner_scope

    async def clear(self) -> None:
        from memspine.services.weaviate_projection import erase_owner

        await erase_owner(self._collection, self._owner_id)

    async def close(self) -> None:
        return None
