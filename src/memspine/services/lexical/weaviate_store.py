"""Weaviate BM25 lexical projection with strict owner and namespace filters.

The adapter takes the shared Weaviate connector and a collection binding; :meth:`start`
ensures the collection (RM-113 item 12). The connector's owner closes the client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from memspine.config.constants import MAX_LEXICAL_QUERY_CHARS, MAX_LEXICAL_QUERY_TERMS
from memspine.exceptions import ConfigError, StorageError
from memspine.services._weaviate import (
    WeaviateCollectionBinding,
    bounded_query,
    ensure_collection,
    erase_owner,
    exact_filter,
    properties,
    record_uuid,
    upsert_object,
)
from memspine.services.lexical.base import LexicalHit
from memspine.store.records import MemoryRecord

if TYPE_CHECKING:
    from memspine.clients.weaviate import WeaviateClient

_PROBE_ID = "__memspine_health_probe__"


class WeaviateLexicalStore:
    def __init__(
        self,
        connector: WeaviateClient,
        binding: WeaviateCollectionBinding,
        *,
        owner_id: str,
    ) -> None:
        if not binding.include_content:
            raise ConfigError("a Weaviate BM25 collection binding needs the content property")
        self._connector = connector
        self._binding = binding
        self._collection: Any = None
        self._owner_id = owner_id
        self._owner_scope = properties("", "", owner_id)["owner_scope"]

    @property
    def binding(self) -> WeaviateCollectionBinding:
        return self._binding

    async def start(self) -> None:
        """Ensure the bound collection exists (idempotent, race-safe)."""
        if self._collection is None:
            self._collection = await ensure_collection(self._connector.handle, self._binding)

    async def probe(self, deadline_s: float) -> bool:
        """Connector readiness, the bound collection's presence and one owned lookup."""
        return bool(
            await self._connector.health(deadline_s)
            and await self._connector.handle.collections.exists(self._binding.name)
            and await self.exists(_PROBE_ID) is False
        )

    @property
    def _owned(self) -> Any:
        if self._collection is None:
            raise StorageError("Weaviate lexical store is not started — call start() first")
        return self._collection

    async def index(self, record: MemoryRecord) -> None:
        await upsert_object(
            self._owned,
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

        result = await self._owned.query.bm25(
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
        obj = await self._owned.query.fetch_object_by_id(
            object_id, return_properties=["owner_scope"]
        )
        if obj is not None and obj.properties.get("owner_scope") == self._owner_scope:
            await self._owned.data.delete_by_id(object_id)

    async def exists(self, record_id: str) -> bool:
        obj = await self._owned.query.fetch_object_by_id(
            record_uuid(self._owner_id, record_id), return_properties=["owner_scope"]
        )
        return obj is not None and obj.properties.get("owner_scope") == self._owner_scope

    async def clear(self) -> None:
        await erase_owner(self._owned, self._owner_id)

    async def close(self) -> None:
        """Nothing to release: the connector's owner closes the client."""
        return None
