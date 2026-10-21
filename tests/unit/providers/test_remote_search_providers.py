from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from memspine.config.schema.remote_providers import OpenSearchSettings, WeaviateSettings
from memspine.exceptions import ConfigError
from memspine.providers.builtin.opensearch import _target_identity as opensearch_identity
from memspine.providers.builtin.weaviate import _target_identity as weaviate_identity
from memspine.providers.catalog import catalog
from memspine.providers.registry import LEXICAL_INDEXES, VECTOR_STORES, get_registry
from memspine.services.lexical.opensearch import OpenSearchLexical
from memspine.services.lexical.weaviate_store import WeaviateLexicalStore
from memspine.services.remote_search import bounded_query, owner_scope
from memspine.services.vector.weaviate_store import WeaviateVectorStore
from memspine.store.records import MemoryRecord


def test_remote_providers_are_lazy_registered_and_category_specific() -> None:
    assert "opensearch" in get_registry("lexical_indexes")
    assert "opensearch" not in VECTOR_STORES
    assert "weaviate" in get_registry("vector_stores")
    assert "weaviate" in LEXICAL_INDEXES
    assert LEXICAL_INDEXES.get("weaviate").settings_model is WeaviateSettings
    assert VECTOR_STORES.get("weaviate").settings_model is WeaviateSettings
    assert LEXICAL_INDEXES.get("opensearch").requires_deployment_id
    assert VECTOR_STORES.get("weaviate").requires_deployment_id
    rows = [
        row
        for row in catalog()
        if (row.category, row.name)
        in {("lexical", "opensearch"), ("vector", "weaviate"), ("lexical", "weaviate")}
    ]
    assert {(row.category, row.name) for row in rows} == {
        ("lexical", "opensearch"),
        ("vector", "weaviate"),
        ("lexical", "weaviate"),
    }
    assert all(
        row.availability
        in {"available", "missing_dependency:opensearchpy", "missing_dependency:weaviate"}
        for row in rows
    )


def test_provider_settings_are_strict_and_require_finite_timeouts() -> None:
    with pytest.raises(ValueError):
        OpenSearchSettings(connect_timeout_s=float("inf"), request_timeout_s=1.0)
    with pytest.raises(ValueError):
        OpenSearchSettings(username_env="USER", connect_timeout_s=1.0, request_timeout_s=1.0)
    with pytest.raises(ValueError):
        WeaviateSettings(connect_timeout_s=1.0, request_timeout_s=1.0, unrecognized=True)


def test_target_identities_resolve_endpoints_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_ENDPOINT", "https://search.example:9443")
    monkeypatch.setenv("SEARCH_USER", "example-user")
    monkeypatch.setenv("SEARCH_PASS", "never-return-this")
    os_settings = OpenSearchSettings(
        url_env="SEARCH_ENDPOINT",
        username_env="SEARCH_USER",
        password_env="SEARCH_PASS",
        connect_timeout_s=1.0,
        request_timeout_s=2.0,
    )
    os_identity = opensearch_identity(os_settings)
    assert os_identity["canonical_target"] == {
        "kind": "opensearch",
        "host": "search.example",
        "port": 9443,
        "index_prefix": "memspine",
    }
    assert "never-return-this" not in json.dumps(os_identity)
    monkeypatch.setenv("VECTOR_ENDPOINT", "http://weaviate.example:8080")
    monkeypatch.setenv("VECTOR_TOKEN", "also-secret")
    weaviate_settings = WeaviateSettings(
        url_env="VECTOR_ENDPOINT",
        api_key_env="VECTOR_TOKEN",
        connect_timeout_s=1.0,
        request_timeout_s=2.0,
    )
    wv_identity = weaviate_identity(weaviate_settings)
    assert wv_identity["canonical_target"] == {
        "kind": "weaviate",
        "host": "weaviate.example",
        "port": 8080,
        "grpc_host": "weaviate.example",
        "grpc_port": 50051,
    }
    assert "also-secret" not in json.dumps(wv_identity)
    monkeypatch.delenv("VECTOR_ENDPOINT")
    with pytest.raises(ConfigError, match="VECTOR_ENDPOINT"):
        weaviate_identity(weaviate_settings)


def test_bounded_query_treats_grammar_as_plain_text() -> None:
    assert bounded_query('namespace:other OR "secret"\x00hello', max_chars=100, max_terms=3) == (
        "namespace other OR"
    )
    assert bounded_query("!!!\x00", max_chars=10, max_terms=5) == ""


class _OpenSearchNative:
    def __init__(self) -> None:
        self.indexed: list[dict[str, Any]] = []
        self.search_request: dict[str, Any] | None = None
        self.delete_query: dict[str, Any] | None = None

    async def index(self, **kwargs: Any) -> None:
        self.indexed.append(kwargs)

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.search_request = kwargs
        return {
            "hits": {
                "hits": [
                    {"_source": {"record_id": "r1"}, "_score": 2.5},
                    {"_source": {"record_id": "other"}, "_score": 1.0},
                ]
            }
        }

    async def delete_by_query(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_query = kwargs
        return {"failures": []}

    async def count(self, **kwargs: Any) -> dict[str, int]:
        return {"count": 0}


class _OpenSearchClient:
    def __init__(self) -> None:
        self.native = _OpenSearchNative()


@pytest.mark.asyncio
async def test_opensearch_search_and_clear_are_namespace_and_owner_scoped() -> None:
    client = _OpenSearchClient()
    store = OpenSearchLexical(
        client, index="memspine-owned", owner_id="deploy-a", request_timeout_s=2
    )
    record = MemoryRecord(
        namespace="ns-a", memory_type="semantic", content="Cobalt notes", record_id="r1"
    )
    await store.index(record)
    assert client.native.indexed[0]["id"] != "r1"
    hits = await store.search("ns-a", 'cobalt OR namespace:ns-b "', top_k=4)
    assert [hit.record_id for hit in hits] == ["r1", "other"]
    request = client.native.search_request
    assert request is not None
    filters = request["body"]["query"]["bool"]["filter"]
    assert {"term": {"namespace": "ns-a"}} in filters
    assert any("owner_scope" in term["term"] for term in filters)
    assert request["body"]["query"]["bool"]["must"][0]["match"]["content"]["query"] == (
        "cobalt OR namespace ns b"
    )
    await store.clear()
    assert client.native.delete_query is not None
    clear_filters = client.native.delete_query["body"]["query"]["bool"]["filter"]
    assert clear_filters == [{"term": {"owner_scope": owner_scope("deploy-a")}}]


class _WeaviateData:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[dict[str, str], list[float] | None]] = {}
        self.deleted_ids: list[str] = []
        self.owner_filter: Any = None

    async def exists(self, object_id: str) -> bool:
        return object_id in self.rows

    async def insert(
        self, *, properties: dict[str, str], uuid: str, vector: list[float] | None
    ) -> None:
        self.rows[uuid] = properties, vector

    async def replace(
        self, *, properties: dict[str, str], uuid: str, vector: list[float] | None
    ) -> None:
        self.rows[uuid] = properties, vector

    async def delete_by_id(self, object_id: str) -> None:
        self.deleted_ids.append(object_id)
        self.rows.pop(object_id, None)

    async def delete_many(self, *, where: Any) -> None:
        self.owner_filter = where
        self.rows.clear()


class _WeaviateQuery:
    def __init__(self, data: _WeaviateData) -> None:
        self.data = data
        self.vector_call: dict[str, Any] | None = None
        self.bm25_call: dict[str, Any] | None = None

    async def near_vector(self, **kwargs: Any) -> Any:
        self.vector_call = kwargs
        return SimpleNamespace(
            objects=[
                SimpleNamespace(
                    properties={"record_id": "r1"}, metadata=SimpleNamespace(distance=0.2)
                )
            ]
        )

    async def bm25(self, **kwargs: Any) -> Any:
        self.bm25_call = kwargs
        return SimpleNamespace(
            objects=[
                SimpleNamespace(properties={"record_id": "r1"}, metadata=SimpleNamespace(score=2.0))
            ]
        )

    async def fetch_object_by_id(self, object_id: str, *, return_properties: list[str]) -> Any:
        row = self.data.rows.get(object_id)
        return None if row is None else SimpleNamespace(properties=row[0])

    async def fetch_objects(self, *, limit: int, filters: Any, return_properties: list[str]) -> Any:
        return SimpleNamespace(objects=[])


class _WeaviateCollection:
    def __init__(self) -> None:
        self.data = _WeaviateData()
        self.query = _WeaviateQuery(self.data)


@pytest.mark.asyncio
async def test_weaviate_vector_lexical_scopes_writes_queries_and_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Filter:
        @staticmethod
        def by_property(name: str) -> Any:
            return SimpleNamespace(equal=lambda value: {name: value})

        @staticmethod
        def all_of(filters: list[Any]) -> list[Any]:
            return filters

    query_module = ModuleType("weaviate.classes.query")
    query_module.Filter = _Filter  # type: ignore[attr-defined]
    query_module.MetadataQuery = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    classes_module = ModuleType("weaviate.classes")
    classes_module.__path__ = []  # type: ignore[attr-defined]
    weaviate_module = ModuleType("weaviate")
    weaviate_module.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weaviate", weaviate_module)
    monkeypatch.setitem(sys.modules, "weaviate.classes", classes_module)
    monkeypatch.setitem(sys.modules, "weaviate.classes.query", query_module)
    collection = _WeaviateCollection()
    vector = WeaviateVectorStore(
        collection, owner_id="deployment-a", embedder_id="hash:4", dimension=4
    )
    await vector.upsert("record-1", "private", "hash:4", [1.0, 0.0, 0.0, 0.0])
    hits = await vector.query("private", [1.0, 0.0, 0.0, 0.0], "hash:4")
    assert [(hit.record_id, hit.score) for hit in hits] == [("r1", 0.8)]
    assert collection.query.vector_call is not None
    assert collection.query.vector_call["filters"] is not None
    await vector.delete("record-1")
    assert not collection.data.rows
    with pytest.raises(ConfigError, match="dimension"):
        await vector.upsert("bad", "private", "hash:4", [1.0])
    with pytest.raises(ConfigError, match="finite"):
        await vector.upsert("bool", "private", "hash:4", [True, 0.0, 0.0, 0.0])  # type: ignore[list-item]
    with pytest.raises(ConfigError, match="nonzero"):
        await vector.upsert("zero", "private", "hash:4", [0.0, 0.0, 0.0, 0.0])

    lexical = WeaviateLexicalStore(collection, owner_id="deployment-a")
    record = MemoryRecord(
        namespace="private", memory_type="semantic", content="violet mountain", record_id="record-1"
    )
    await lexical.index(record)
    lexical_hits = await lexical.search("private", "violet namespace:other")
    assert [(hit.record_id, hit.score) for hit in lexical_hits] == [("r1", 2.0)]
    assert collection.query.bm25_call is not None
    assert collection.query.bm25_call["query"] == "violet namespace other"
    assert collection.query.bm25_call["filters"] is not None
    assert await lexical.exists("record-1")
    await lexical.clear()
    assert collection.data.owner_filter is not None
