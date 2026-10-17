"""Weaviate collection primitives shared by its vector and BM25 adapters."""

from __future__ import annotations

import math
import uuid
from numbers import Real
from typing import Any

from memspine.exceptions import ConfigError
from memspine.services.remote_search import bounded_query, document_id, owner_scope


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


async def ensure_collection(client: Any, name: str, *, include_content: bool) -> Any:
    from weaviate.classes.config import (
        Configure,
        DataType,
        Property,
        Tokenization,
        VectorDistances,
    )

    collections = client.collections
    if not await collections.exists(name):
        props = [
            Property(name="record_id", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
            Property(name="namespace", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
            Property(name="owner_scope", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
            Property(name="embedder_id", data_type=DataType.TEXT, tokenization=Tokenization.FIELD),
        ]
        if include_content:
            props.append(Property(name="content", data_type=DataType.TEXT))
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
        raise ConfigError(f"Weaviate vector dimension mismatch: expected {expected_dim}, received {len(vector)}")
    if any(
        isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value))
        for value in vector
    ):
        raise ConfigError("Weaviate vector values must be finite")
    normalized = [float(value) for value in vector]
    if not any(value != 0.0 for value in normalized):
        raise ConfigError("Weaviate cosine vectors must have nonzero magnitude")
    return normalized


async def upsert_object(collection: Any, *, owner_id: str, record_id: str, props: dict[str, str], vector: list[float] | None = None) -> None:
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
