"""Real OpenSearch and Weaviate adapter tests; each is opt-in or runner provisioned."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from memspine.clients.opensearch import OpenSearchClient
from memspine.clients.weaviate import WeaviateClient
from memspine.config.schema.remote_providers import OpenSearchSettings, WeaviateSettings
from memspine.services.lexical.opensearch import OpenSearchLexical
from memspine.services.lexical.weaviate_store import WeaviateLexicalStore
from memspine.services.vector.weaviate_store import WeaviateVectorStore
from memspine.services.weaviate_projection import collection_name, ensure_collection
from memspine.store.records import MemoryRecord


def _configured(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"local remote-provider service unavailable; set {name}")
    return value


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_opensearch_lexical_crud_namespace_and_deployment_isolation() -> None:
    endpoint = _configured("MEMSPINE_TEST_LIVE_OPENSEARCH_URL")
    settings = OpenSearchSettings(
        url_env="MEMSPINE_TEST_LIVE_OPENSEARCH_URL",
        connect_timeout_s=20.0,
        request_timeout_s=20.0,
    )
    client = OpenSearchClient(settings)
    await client.connect()
    marker = uuid.uuid4().hex
    index_name = f"memspine-live-{marker}"
    left = OpenSearchLexical(
        client, index=index_name, owner_id=f"left-{marker}", request_timeout_s=20.0
    )
    right = OpenSearchLexical(
        client, index=index_name, owner_id=f"right-{marker}", request_timeout_s=20.0
    )
    try:
        await left.ensure_index()
        await right.ensure_index()
        await left.index_many(
            [
                MemoryRecord(
                    record_id=f"left-alpha-{marker}",
                    namespace="alpha",
                    memory_type="semantic",
                    content=f"cobalt observability {marker}",
                ),
                MemoryRecord(
                    record_id=f"left-beta-{marker}",
                    namespace="beta",
                    memory_type="semantic",
                    content=f"cobalt observability {marker}",
                ),
            ]
        )
        await right.index(
            MemoryRecord(
                record_id=f"right-alpha-{marker}",
                namespace="alpha",
                memory_type="semantic",
                content=f"cobalt observability {marker}",
            )
        )
        assert [hit.record_id for hit in await left.search("alpha", f"cobalt {marker}")] == [
            f"left-alpha-{marker}"
        ]
        assert await left.exists(f"left-alpha-{marker}")
        await left.delete(f"left-alpha-{marker}")
        assert not await left.exists(f"left-alpha-{marker}")
        await left.clear()
        assert await right.exists(f"right-alpha-{marker}")
        assert await right.search("alpha", f"cobalt {marker}")
    finally:
        await left.clear()
        await right.clear()
        await client.native.indices.delete(index=index_name, ignore=[404])
        await client.close()


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_weaviate_vector_and_bm25_crud_namespace_and_deployment_isolation() -> None:
    _configured("MEMSPINE_TEST_LIVE_WEAVIATE_URL")
    grpc_port = int(os.environ.get("MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT", "50051"))
    settings = WeaviateSettings(
        url_env="MEMSPINE_TEST_LIVE_WEAVIATE_URL",
        grpc_port=grpc_port,
        connect_timeout_s=20.0,
        request_timeout_s=20.0,
    )
    client = WeaviateClient(settings)
    await client.connect()
    marker = uuid.uuid4().hex
    collection_id = collection_name("MemspineLive", "Both", f"contract-{marker}", "active")
    try:
        collection = await ensure_collection(client.native, collection_id, include_content=True)
        lexical_left = WeaviateLexicalStore(collection, owner_id=f"left-{marker}")
        lexical_right = WeaviateLexicalStore(collection, owner_id=f"right-{marker}")
        await lexical_left.index(
            MemoryRecord(
                record_id=f"lex-left-{marker}",
                namespace="alpha",
                memory_type="semantic",
                content=f"amber telescope {marker}",
            )
        )
        await lexical_right.index(
            MemoryRecord(
                record_id=f"lex-right-{marker}",
                namespace="alpha",
                memory_type="semantic",
                content=f"amber telescope {marker}",
            )
        )
        assert [hit.record_id for hit in await lexical_left.search("alpha", f"amber {marker}")] == [
            f"lex-left-{marker}"
        ]

        vector_left = WeaviateVectorStore(
            collection, owner_id=f"left-{marker}", embedder_id="test:2", dimension=2
        )
        vector_right = WeaviateVectorStore(
            collection, owner_id=f"right-{marker}", embedder_id="test:2", dimension=2
        )
        await vector_left.upsert(f"vec-left-{marker}", "alpha", "test:2", [1.0, 0.0])
        await vector_right.upsert(f"vec-right-{marker}", "alpha", "test:2", [1.0, 0.0])
        assert [
            hit.record_id for hit in await vector_left.query("alpha", [1.0, 0.0], "test:2")
        ] == [f"vec-left-{marker}"]
        assert await vector_left.exists(f"vec-left-{marker}")
        await vector_left.delete(f"vec-left-{marker}")
        assert not await vector_left.exists(f"vec-left-{marker}")
        assert await vector_right.exists(f"vec-right-{marker}")

        await lexical_left.clear()
        assert await lexical_right.exists(f"lex-right-{marker}")
        assert await lexical_right.search("alpha", f"amber {marker}")
    finally:
        if await client.native.collections.exists(collection_id):
            await client.native.collections.delete(collection_id)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_engine_builds_selected_remote_vector_and_lexical_providers(tmp_path: Path) -> None:
    os_url = _configured("MEMSPINE_TEST_LIVE_OPENSEARCH_URL")
    weaviate_url = _configured("MEMSPINE_TEST_LIVE_WEAVIATE_URL")
    grpc_port = int(os.environ.get("MEMSPINE_TEST_LIVE_WEAVIATE_GRPC_PORT", "50051"))
    from memspine import Engine

    deployment_id = f"remote-engine-{uuid.uuid4().hex}"
    engine = Engine(
        template="core",
        dotenv_path=None,
        runtime={"deployment_id": deployment_id},
        storage={
            "provider": "sqlite",
            "settings": {"path": str(tmp_path / "events.sqlite"), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {"dim": 64}},
        vector={
            "provider": "weaviate",
            "settings": {
                "url_env": "MEMSPINE_TEST_LIVE_WEAVIATE_URL",
                "grpc_port": grpc_port,
                "connect_timeout_s": 20.0,
                "request_timeout_s": 20.0,
            },
        },
        cache={"provider": "memory", "settings": {}},
        graph={"provider": "sql_adjacency", "settings": {}},
        read={
            "lexical_provider": "opensearch",
            "lexical_settings": {
                "url_env": "MEMSPINE_TEST_LIVE_OPENSEARCH_URL",
                "index_prefix": f"memspine{uuid.uuid4().hex[:10]}",
                "connect_timeout_s": 20.0,
                "request_timeout_s": 20.0,
            },
        },
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True},
        },
    )
    assert os_url and weaviate_url
    content = f"remote engine live check {uuid.uuid4().hex} cobalt retrieval"
    await engine.start()
    try:
        health = await engine.provider_health()
        assert {
            (item.category, item.provider, item.status.value)
            for item in health.providers
            if item.category in {"vector", "lexical"}
        } == {("vector", "weaviate", "healthy"), ("lexical", "opensearch", "healthy")}
        assert await engine.write(content, namespace="remote-engine")
        results = await engine.search("cobalt retrieval", namespace="remote-engine", top_k=5)
        assert any(record.content == content for record, _score in results)
    finally:
        await engine.stop()
