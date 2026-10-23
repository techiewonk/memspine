"""Weaviate collection primitives for the documents adapter (private, RM-112).

The documents collection is separate from the vector and BM25 collections and holds three
kinds of objects, told apart by the ``kind`` property: ``head`` (one per document), ``chunk``
(the bytes as BLOB pieces) and ``ref`` (one per record reference). Every property is exact
match (``FIELD`` tokenized, not BM25-searchable); ``blob`` and ``metadata`` are not indexed.
There is no vectorizer: the collection declares a self-provided ``flat`` vector index and no
object carries a vector, so nothing is embedded and no HNSW graph is built.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DOCUMENT_PROPERTIES",
    "WeaviateDocumentsBinding",
    "all_of",
    "any_value",
    "both",
    "documents_collection_name",
    "ensure_documents_collection",
    "key_after",
    "object_uuid",
    "sort_by_key",
]

#: (name, type) of every property of a documents collection; type is ``text``, ``int`` or
#: ``blob``.
DOCUMENT_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("kind", "text"),
    ("namespace", "text"),
    ("digest", "text"),
    ("key", "text"),
    ("upload", "text"),
    ("seq", "int"),
    ("size", "int"),
    ("chunk_count", "int"),
    ("media_type", "text"),
    ("metadata", "json"),
    ("record_id", "text"),
    ("created_at", "text"),
    ("blob", "blob"),
)


@dataclass(frozen=True, slots=True)
class WeaviateDocumentsBinding:
    """One owned documents collection (its name; the schema is :data:`DOCUMENT_PROPERTIES`)."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name[0].isupper() or not self.name.isalnum():
            raise ValueError("a Weaviate documents collection name is an upper-case identifier")


def documents_collection_name(prefix: str, owner_id: str) -> str:
    """``<prefix>Documents<owner digest>``. Unlike projections, documents are source data, so
    the name never depends on a projection generation."""
    from memspine.services._weaviate import collection_name

    return collection_name(prefix, "Documents", owner_id, "documents-v1")


def object_uuid(collection: str, *parts: str) -> str:
    """A deterministic object id within ``collection``."""
    joined = "\0".join(parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memspine:weaviate:documents:{collection}:{joined}"))


async def ensure_documents_collection(client: Any, binding: WeaviateDocumentsBinding) -> Any:
    """Create the documents collection unless it exists (race-safe); return its handle."""
    from weaviate.classes.config import Configure, DataType, Property, Tokenization

    name = binding.name
    collections = client.collections
    if not await collections.exists(name):
        props = []
        for prop, kind in DOCUMENT_PROPERTIES:
            if kind == "blob":
                props.append(Property(name=prop, data_type=DataType.BLOB))
            elif kind == "int":
                props.append(
                    Property(
                        name=prop,
                        data_type=DataType.INT,
                        index_filterable=True,
                        index_searchable=False,
                    )
                )
            elif kind == "json":
                props.append(
                    Property(
                        name=prop,
                        data_type=DataType.TEXT,
                        index_filterable=False,
                        index_searchable=False,
                        skip_vectorization=True,
                    )
                )
            else:
                props.append(
                    Property(
                        name=prop,
                        data_type=DataType.TEXT,
                        tokenization=Tokenization.FIELD,
                        index_filterable=True,
                        index_searchable=False,
                        skip_vectorization=True,
                    )
                )
        try:
            await collections.create(
                name=name,
                vector_config=Configure.Vectors.self_provided(
                    vector_index_config=Configure.VectorIndex.flat()
                ),
                properties=props,
            )
        except Exception:
            if not await collections.exists(name):
                raise
    return collections.get(name)


def all_of(**terms: str) -> Any:
    """Exact-match filter on every ``property=value`` pair."""
    from weaviate.classes.query import Filter

    return Filter.all_of([Filter.by_property(key).equal(value) for key, value in terms.items()])


def any_value(prop: str, values: list[str]) -> Any:
    """``prop`` equals any of ``values``."""
    from weaviate.classes.query import Filter

    return Filter.by_property(prop).contains_any(values)


def key_after(base: Any, last: str | None) -> Any:
    """``base`` restricted to ``key > last`` (keyset pagination)."""
    if last is None:
        return base
    from weaviate.classes.query import Filter

    return Filter.all_of([base, Filter.by_property("key").greater_than(last)])


def sort_by_key() -> Any:
    from weaviate.classes.query import Sort

    return Sort.by_property("key", ascending=True)


def both(left: Any, right: Any) -> Any:
    from weaviate.classes.query import Filter

    return Filter.all_of([left, right])
