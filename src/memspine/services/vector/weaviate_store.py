"""Self-provided-vector Weaviate store for rebuildable memory projections.

The adapter takes the shared Weaviate connector and a collection binding; :meth:`start`
ensures the collection (RM-113 item 12). The connector's owner closes the client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memspine.exceptions import ConfigError, StorageError
from memspine.services._weaviate import (
    WeaviateCollectionBinding,
    ensure_collection,
    erase_owner,
    exact_filter,
    properties,
    record_uuid,
    upsert_object,
    validate_vector,
)
from memspine.services.vector.base import VectorHit

if TYPE_CHECKING:
    from memspine.clients.weaviate import WeaviateClient

_PROBE_ID = "__memspine_health_probe__"


class WeaviateVectorStore:
    def __init__(
        self,
        connector: WeaviateClient,
        binding: WeaviateCollectionBinding,
        *,
        owner_id: str,
        embedder_id: str,
        dimension: int,
    ) -> None:
        self._connector = connector
        self._binding = binding
        self._collection: Any = None
        self._owner_id = owner_id
        self._embedder_id = embedder_id
        self._dimension = dimension

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
            raise StorageError("Weaviate vector store is not started — call start() first")
        return self._collection

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        values = self._validate(embedder_id, vector)
        await upsert_object(
            self._owned,
            owner_id=self._owner_id,
            record_id=record_id,
            props=properties(record_id, namespace, self._owner_id, embedder_id=embedder_id),
            vector=values,
        )

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        values = self._validate(embedder_id, vector)
        if top_k <= 0:
            return []
        from weaviate.classes.query import MetadataQuery

        result = await self._owned.query.near_vector(
            near_vector=values,
            filters=exact_filter(
                namespace=namespace,
                owner_scope=properties("", "", self._owner_id)["owner_scope"],
                embedder_id=self._embedder_id,
            ),
            limit=min(top_k, 1000),
            return_metadata=MetadataQuery(distance=True),
            return_properties=["record_id"],
        )
        hits: list[VectorHit] = []
        for obj in result.objects:
            record_id = obj.properties.get("record_id")
            distance = getattr(obj.metadata, "distance", None)
            if record_id is not None and distance is not None:
                hits.append(VectorHit(str(record_id), 1.0 - float(distance)))
        return sorted(hits, key=lambda hit: (-hit.score, hit.record_id))[:top_k]

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        return await self.query(namespace, vector, embedder_id, top_k)

    async def delete(self, record_id: str) -> None:
        object_id = record_uuid(self._owner_id, record_id)
        if await self.exists(record_id):
            await self._owned.data.delete_by_id(object_id)

    async def delete_all(self) -> None:
        await erase_owner(self._owned, self._owner_id)

    async def exists(self, record_id: str) -> bool:
        obj = await self._owned.query.fetch_object_by_id(
            record_uuid(self._owner_id, record_id), return_properties=["owner_scope"]
        )
        return (
            obj is not None
            and obj.properties.get("owner_scope")
            == properties("", "", self._owner_id)["owner_scope"]
        )

    def _validate(self, embedder_id: str, vector: list[float]) -> list[float]:
        if embedder_id != self._embedder_id:
            raise ConfigError("Weaviate request embedder does not match the opened vector space")
        return validate_vector(vector, self._dimension)
