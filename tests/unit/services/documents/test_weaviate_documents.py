"""RM-112: Weaviate DocumentStore specifics beyond the shared conformance suite.

Offline: settings caps, deployment-id requirement and the shared, credential-free identity.
Live (``MEMSPINE_TEST_WEAVIATE_URL`` and ``MEMSPINE_TEST_WEAVIATE_GRPC_PORT``): one client
shared with the lexical slot, keyset pagination, multi-chunk documents, no vectors, and
corruption detection."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from memspine.exceptions import ConfigError, StorageError
from memspine.providers import shared
from memspine.providers.contexts import (
    DocumentsBuildContext,
    LexicalBuildContext,
    SharedBuildContext,
)
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import (
    ProviderRegistryError,
    get_registry,
    provider_target_identity,
    validate_settings,
)
from memspine.providers.shared import ResourceRegistry
from memspine.services.documents.weaviate import WeaviateDocumentStore

URL = os.environ.get("MEMSPINE_TEST_WEAVIATE_URL")
MiB = 1024 * 1024


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


def _settings(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "url_env": "MEMSPINE_TEST_WEAVIATE_URL",
        "collection_prefix": f"Wd{uuid.uuid4().hex[:12]}",
        "grpc_port": int(os.environ.get("MEMSPINE_TEST_WEAVIATE_GRPC_PORT", "50051")),
        "connect_timeout_s": 10.0,
        "request_timeout_s": 30.0,
    }
    base.update(changes)
    return base


def _ctx(resources: ProviderLifecycleManager, tmp: Path, owner: str = "owner-1") -> Any:
    return SharedBuildContext(resources, owner, 5.0), ProjectionPlacement(
        tmp / "records.db", False, owner or "placement-owner"
    )


# ------------------------------------------------------------------ offline
@pytest.mark.parametrize(
    ("changes", "field"),
    [({"max_bytes": 64 * MiB + 1}, "max_bytes"), ({"chunk_bytes": 4 * MiB + 1}, "chunk_bytes")],
)
def test_settings_refuse_limits_weaviate_cannot_meet(changes: dict[str, Any], field: str) -> None:
    descriptor = get_registry("document_stores").get("weaviate")
    with pytest.raises(ProviderRegistryError, match=field):
        validate_settings(descriptor, _settings(**changes))


def test_descriptor_traits() -> None:
    descriptor = get_registry("document_stores").get("weaviate")
    assert descriptor.locality == "cluster" and not descriptor.single_writer
    assert descriptor.requires_deployment_id


def test_identity_matches_the_vector_slot_and_hides_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMSPINE_TEST_WEAVIATE_URL", "https://weaviate.example:8443")
    monkeypatch.setenv("MEMSPINE_TEST_WV_KEY", "wv-api-key-123")
    raw = _settings(api_key_env="MEMSPINE_TEST_WV_KEY", collection_prefix="Memspine")
    documents = get_registry("document_stores").get("weaviate")
    vector = get_registry("vector_stores").get("weaviate")
    doc_identity = provider_target_identity(documents, validate_settings(documents, raw))
    vec_settings = {k: v for k, v in raw.items() if k not in ("max_bytes", "chunk_bytes")}
    vec_identity = provider_target_identity(vector, validate_settings(vector, vec_settings))
    assert doc_identity == vec_identity  # one deployment, one client
    assert "wv-api-key-123" not in repr(doc_identity)
    assert doc_identity is not None and doc_identity["host"] == "weaviate.example"


async def test_a_deployment_id_is_required(tmp_path: Path) -> None:
    descriptor = get_registry("document_stores").get("weaviate")
    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    shared_ctx, placement = _ctx(resources, tmp_path, owner="")
    with pytest.raises(ConfigError, match="deployment.id"):
        await descriptor.builder.build(
            validate_settings(descriptor, _settings()), DocumentsBuildContext(shared_ctx, placement)
        )


# ------------------------------------------------------------------ live
@pytest.fixture
async def built(tmp_path: Path) -> AsyncIterator[tuple[WeaviateDocumentStore, Any]]:
    if not URL:
        pytest.skip("set MEMSPINE_TEST_WEAVIATE_URL")
    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    shared_ctx, placement = _ctx(resources, tmp_path)
    settings = _settings(chunk_bytes=MiB)
    docs_descriptor = get_registry("document_stores").get("weaviate")
    lexical_descriptor = get_registry("lexical_indexes").get("weaviate")
    docs = await docs_descriptor.builder.build(
        validate_settings(docs_descriptor, settings), DocumentsBuildContext(shared_ctx, placement)
    )
    lexical_settings = {k: v for k, v in settings.items() if k != "chunk_bytes"}
    lexical = await lexical_descriptor.builder.build(
        validate_settings(lexical_descriptor, lexical_settings),
        LexicalBuildContext(shared_ctx, placement),
    )
    try:
        yield docs.service, lexical.service
    finally:
        client = docs.service._connector.handle
        await client.collections.delete(docs.service.collection_name)
        assert (await resources.close_all()).complete


async def test_live_documents_share_the_lexical_slots_client(built: Any) -> None:
    docs, lexical = built
    assert docs._connector is lexical._connector
    assert await docs.probe(5.0)
    assert "api" not in repr(docs.describe()).lower()


async def test_live_keyset_pagination_lists_and_erases_across_pages(
    built: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs, _ = built
    monkeypatch.setattr("memspine.services.documents.weaviate._PAGE", 2)
    heads = [
        await docs.put("ns", f"doc-{i}".encode(), record_ids=["shared", f"r{i}"]) for i in range(5)
    ]
    listed = await docs.list("ns")
    assert [h.digest for h in listed] == sorted(h.digest for h in heads)
    assert all(h.record_ids[0] == "shared" and len(h.record_ids) == 2 for h in listed)
    assert await docs.erase_records("ns", ["shared"]) == []
    erased = await docs.erase_records("ns", [f"r{i}" for i in range(5)])
    assert erased == sorted(h.digest for h in heads)
    assert await docs.list("ns") == []


async def test_live_multi_chunk_documents_and_no_vectors(built: Any) -> None:
    docs, _ = built
    data = os.urandom(5 * MiB + 123)
    head = await docs.put("ns", (data[i : i + 700_000] for i in range(0, len(data), 700_000)))
    assert await docs.get("ns", head.digest) == data
    streamed = [c async for c in docs.stream("ns", head.digest, chunk_size=2 * MiB)]
    assert b"".join(streamed) == data and [len(c) for c in streamed][:2] == [2 * MiB, 2 * MiB]
    page = await docs._coll().query.fetch_objects(limit=20, include_vector=True)
    kinds = sorted(obj.properties["kind"] for obj in page.objects)
    assert kinds == ["chunk"] * 6 + ["head"]
    assert all(not obj.vector for obj in page.objects)  # nothing is embedded


async def test_live_a_missing_chunk_is_reported_not_returned(built: Any) -> None:
    docs, _ = built
    head = await docs.put("ns", os.urandom(2 * MiB + 1))
    obj = await docs._head_object("ns", head.digest)
    await docs._coll().data.delete_by_id(docs._chunk_id(str(obj.properties["upload"]), 1))
    with pytest.raises(StorageError, match="chunk is missing"):
        await docs.get("ns", head.digest)


async def test_live_failed_put_leaves_no_chunks(built: Any) -> None:
    docs, _ = built
    with pytest.raises(StorageError, match="expected_digest"):
        await docs.put("ns", os.urandom(3 * MiB), expected_digest="0" * 64)
    page = await docs._coll().query.fetch_objects(limit=10)
    assert page.objects == []
