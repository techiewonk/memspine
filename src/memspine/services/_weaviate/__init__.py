"""Weaviate collection primitives shared by its vector and BM25 adapters (private).

The factory hands each adapter the shared Weaviate connector and a
:class:`WeaviateCollectionBinding` (collection name and text properties); the adapter runs
:func:`ensure_collection` in its ``start()`` (RM-113 item 12). No native handle crosses the
factory."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from numbers import Real
from typing import Any

from memspine.exceptions import ConfigError
from memspine.services.remote_search import bounded_query, document_id, owner_scope

#: Exact-match (``FIELD`` tokenized) properties every owned collection carries.
BASE_PROPERTIES: tuple[str, ...] = ("record_id", "namespace", "owner_scope", "embedder_id")
#: Word-tokenized (BM25-searchable) properties.
FULL_TEXT_PROPERTIES = frozenset({"content"})
#: The text properties of a vector collection and of a BM25 collection.
VECTOR_PROPERTIES = BASE_PROPERTIES
LEXICAL_PROPERTIES: tuple[str, ...] = (*BASE_PROPERTIES, "content")


@dataclass(frozen=True, slots=True)
class WeaviateCollectionBinding:
    """One owned collection: its name and its text properties (the base exact-match set,
    plus ``content`` for a BM25 collection)."""

    name: str
    properties: tuple[str, ...] = VECTOR_PROPERTIES

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a Weaviate collection binding needs a name")
        missing = [name for name in BASE_PROPERTIES if name not in self.properties]
        if missing:
            raise ValueError(f"a Weaviate collection binding needs properties {missing}")
        if len(set(self.properties)) != len(self.properties):
            raise ValueError("Weaviate collection properties must be unique")

    @property
    def include_content(self) -> bool:
        return "content" in self.properties


def collection_name(prefix: str, service: str, owner_id: str, generation: str) -> str:
    scope = document_id(owner_id, generation)[:20]
    return f"{prefix}{service}{scope}"


def record_uuid(owner_id: str, record_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memspine:weaviate:{owner_id}:{record_id}"))


def properties(record_id: str, namespace: str, owner_id: str, **extra: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "namespace": namespace,
        "owner_scope": owner_scope(owner_id),
        **extra,
    }


def exact_filter(**terms: str) -> Any:
    from weaviate.classes.query import Filter

    return Filter.all_of([Filter.by_property(key).equal(value) for key, value in terms.items()])


async def ensure_collection(client: Any, binding: WeaviateCollectionBinding) -> Any:
    """Create ``binding``'s collection unless it exists (race-safe) and return its handle.

    ``client`` is the connector's native async client (``WeaviateClient.handle``)."""
    from weaviate.classes.config import (
        Configure,
        DataType,
        Property,
        Tokenization,
        VectorDistances,
    )

    name = binding.name
    collections = client.collections
    if not await collections.exists(name):
        props = [
            Property(name=prop, data_type=DataType.TEXT)
            if prop in FULL_TEXT_PROPERTIES
            else Property(name=prop, data_type=DataType.TEXT, tokenization=Tokenization.FIELD)
            for prop in binding.properties
        ]
        try:
            await collections.create(
                name=name,
                vector_config=Configure.Vectors.self_provided(
                    vector_index_config=Configure.VectorIndex.hnsw(
                        distance_metric=VectorDistances.COSINE
                    )
                ),
                properties=props,
            )
        except Exception:
            # Parallel vector/lexical engine creation can race on the same target.
            if not await collections.exists(name):
                raise
    return collections.get(name)


def validate_vector(vector: list[float], expected_dim: int) -> list[float]:
    if len(vector) != expected_dim:
        raise ConfigError(
            f"Weaviate vector dimension mismatch: expected {expected_dim}, received {len(vector)}"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value))
        for value in vector
    ):
        raise ConfigError("Weaviate vector values must be finite")
    normalized = [float(value) for value in vector]
    if not any(value != 0.0 for value in normalized):
        raise ConfigError("Weaviate cosine vectors must have nonzero magnitude")
    return normalized


async def upsert_object(
    collection: Any,
    *,
    owner_id: str,
    record_id: str,
    props: dict[str, str],
    vector: list[float] | None = None,
) -> None:
    data = collection.data
    object_id = record_uuid(owner_id, record_id)
    if await data.exists(object_id):
        await data.replace(uuid=object_id, properties=props, vector=vector)
        return
    try:
        await data.insert(properties=props, uuid=object_id, vector=vector)
    except Exception:
        # Make replay concurrent with another identical write safe, while not
        # suppressing failures unless the deterministic object now exists.
        if not await data.exists(object_id):
            raise
        await data.replace(uuid=object_id, properties=props, vector=vector)


async def erase_owner(collection: Any, owner_id: str) -> None:
    from weaviate.classes.query import Filter

    owner_filter = Filter.by_property("owner_scope").equal(owner_scope(owner_id))
    result = await collection.data.delete_many(where=owner_filter)
    if int(getattr(result, "failed", 0) or 0):
        raise RuntimeError("Weaviate clear did not delete every owned object")
    remaining = await collection.query.fetch_objects(
        limit=1,
        filters=owner_filter,
        return_properties=["owner_scope"],
    )
    if remaining.objects:
        raise RuntimeError("Weaviate clear could not verify owned objects were removed")


__all__ = [
    "BASE_PROPERTIES",
    "FULL_TEXT_PROPERTIES",
    "LEXICAL_PROPERTIES",
    "VECTOR_PROPERTIES",
    "WeaviateCollectionBinding",
    "bounded_query",
    "collection_name",
    "document_id",
    "ensure_collection",
    "erase_owner",
    "exact_filter",
    "properties",
    "record_uuid",
    "upsert_object",
    "validate_vector",
]
