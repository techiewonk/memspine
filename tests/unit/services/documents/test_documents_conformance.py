"""ADR-081 ``storage.documents``: one conformance suite for every DocumentStore provider.

Each provider is built through its registered descriptor (settings -> spec -> shared connector
-> adapter), exactly as the container builds it. ``filesystem`` runs everywhere. The remote
providers (RM-112) run against a live service when its variable is set: ``mongodb`` (GridFS,
needs ``MEMSPINE_TEST_MONGO_URL``, a replica set, the same variable as the records suite; each
test gets a throwaway database), ``s3`` (needs ``MEMSPINE_TEST_S3_URL``, see
:func:`_s3_settings`) and ``weaviate`` (needs ``MEMSPINE_TEST_WEAVIATE_URL``). A provider whose
descriptor is not registered yet is skipped with the reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from memspine.exceptions import StorageError
from memspine.providers import shared
from memspine.providers.contexts import DocumentsBuildContext, SharedBuildContext
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import get_registry, validate_settings
from memspine.providers.shared import ResourceRegistry
from memspine.services.documents.base import (
    DocumentNotFoundError,
    DocumentStore,
    DocumentTooLargeError,
)

CAP = 4096  # max_bytes for every provider under test


@dataclass(frozen=True)
class Case:
    name: str
    settings: Callable[[Path], dict[str, Any]]
    env: str | None = None


def _s3_settings(tmp: Path) -> dict[str, Any]:
    """MinIO or another S3-compatible store: ``MEMSPINE_TEST_S3_URL`` (endpoint),
    ``MEMSPINE_TEST_S3_ACCESS_KEY`` / ``MEMSPINE_TEST_S3_SECRET_KEY`` (credentials; unset =
    the boto3 chain) and ``MEMSPINE_TEST_S3_BUCKET`` (created when missing). Each test gets
    its own key prefix."""
    settings: dict[str, Any] = {
        "bucket": os.environ.get("MEMSPINE_TEST_S3_BUCKET", "memspine-conformance"),
        "prefix": f"conformance/{uuid.uuid4().hex}/",
        "endpoint_url_env": "MEMSPINE_TEST_S3_URL",
        "region": os.environ.get("MEMSPINE_TEST_S3_REGION", "us-east-1"),
        "addressing_style": "path",
        "connect_timeout_s": 10.0,
        "request_timeout_s": 30.0,
        "max_bytes": CAP,
        "create_bucket": True,
    }
    if os.environ.get("MEMSPINE_TEST_S3_ACCESS_KEY"):
        settings["access_key_id_env"] = "MEMSPINE_TEST_S3_ACCESS_KEY"
        settings["secret_access_key_env"] = "MEMSPINE_TEST_S3_SECRET_KEY"
    return settings


def _weaviate_settings(tmp: Path) -> dict[str, Any]:
    """``MEMSPINE_TEST_WEAVIATE_URL`` (and ``MEMSPINE_TEST_WEAVIATE_GRPC_PORT``); each test gets
    its own collection, and a small chunk size so streams span several chunk objects."""
    return {
        "url_env": "MEMSPINE_TEST_WEAVIATE_URL",
        "collection_prefix": f"Conf{uuid.uuid4().hex[:12]}",
        "grpc_port": int(os.environ.get("MEMSPINE_TEST_WEAVIATE_GRPC_PORT", "50051")),
        "connect_timeout_s": 10.0,
        "request_timeout_s": 30.0,
        "max_bytes": CAP,
        "chunk_bytes": 1000,
    }


CASES = (
    Case("filesystem", lambda tmp: {"root": str(tmp / "docs"), "max_bytes": CAP}),
    Case(
        "mongodb",
        lambda tmp: {
            "url": os.environ["MEMSPINE_TEST_MONGO_URL"],
            "database": f"dc_{uuid.uuid4().hex[:12]}",
            "max_bytes": CAP,
        },
        env="MEMSPINE_TEST_MONGO_URL",
    ),
    Case("s3", _s3_settings, env="MEMSPINE_TEST_S3_URL"),
    Case("weaviate", _weaviate_settings, env="MEMSPINE_TEST_WEAVIATE_URL"),
)


#: throwaway MongoDB databases made by the mongodb case, dropped after the test
_MONGO_DATABASES: list[str] = []


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())
    yield
    if _MONGO_DATABASES:
        from pymongo import MongoClient

        with MongoClient(os.environ["MEMSPINE_TEST_MONGO_URL"]) as admin:
            while _MONGO_DATABASES:
                admin.drop_database(_MONGO_DATABASES.pop())


async def _build(case: Case, tmp_path: Path, resources: ProviderLifecycleManager) -> Any:
    registry = get_registry("document_stores")
    descriptor = registry.find(case.name)
    if descriptor is None:
        pytest.skip(f"documents provider {case.name!r} is planned (RM-112), not registered yet")
    if case.env is not None and not os.environ.get(case.env):
        pytest.skip(f"documents provider {case.name!r}: set {case.env}")
    raw = case.settings(tmp_path)
    settings = validate_settings(descriptor, raw)
    if case.name == "mongodb":
        _MONGO_DATABASES.append(str(raw["database"]))
    ctx = DocumentsBuildContext(
        SharedBuildContext(resources, "conformance-owner", 5.0),
        ProjectionPlacement(tmp_path / "records.db", False, "conformance-owner"),
    )
    return await descriptor.builder.build(settings, ctx)


@pytest.fixture(params=CASES, ids=lambda c: c.name)
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[DocumentStore]:
    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    result = await _build(request.param, tmp_path, resources)
    assert isinstance(result.service, DocumentStore)
    assert {"documents", "namespace_filter", "erase"} <= result.effective_capabilities
    assert result.health_probe is not None and await result.health_probe()
    try:
        yield result.service
    finally:
        assert (await resources.close_all()).complete


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


# ------------------------------------------------------------------------------ put/get/head
async def test_put_get_head_round_trip(store: DocumentStore) -> None:
    data = b"hello documents"
    head = await store.put("ns", data, media_type="text/plain", metadata={"origin": "test"})
    assert head.digest == _sha(data)
    assert (head.namespace, head.size, head.media_type) == ("ns", len(data), "text/plain")
    assert dict(head.metadata) == {"origin": "test"}
    assert await store.get("ns", head.digest) == data
    again = await store.head("ns", head.digest)
    assert again is not None and again.digest == head.digest and again.size == len(data)
    assert again.created_at


async def test_missing_documents_are_none_and_stream_raises(store: DocumentStore) -> None:
    digest = _sha(b"never stored")
    assert await store.get("ns", digest) is None
    assert await store.head("ns", digest) is None
    assert not await store.delete("ns", digest)
    with pytest.raises(DocumentNotFoundError):
        async for _ in store.stream("ns", digest):
            pass


async def test_put_accepts_sync_and_async_chunk_streams(store: DocumentStore) -> None:
    parts = (b"alpha-", b"beta-", b"gamma")
    sync_head = await store.put("ns", iter(parts))
    async_head = await store.put("ns2", _chunks(*parts))
    assert sync_head.digest == async_head.digest == _sha(b"".join(parts))
    assert await store.get("ns2", async_head.digest) == b"".join(parts)


async def test_stream_returns_the_bytes_in_chunks(store: DocumentStore) -> None:
    data = bytes(range(256)) * 10
    head = await store.put("ns", data)
    chunks = [c async for c in store.stream("ns", head.digest, chunk_size=300)]
    assert b"".join(chunks) == data
    assert len(chunks) == -(-len(data) // 300) and all(len(c) <= 300 for c in chunks)


async def test_put_is_idempotent_by_content_and_merges_references(store: DocumentStore) -> None:
    first = await store.put("ns", b"same", metadata={"a": "1"}, record_ids=["r1"])
    second = await store.put("ns", b"same", metadata={"b": "2"}, record_ids=["r2", "r1"])
    assert first.digest == second.digest
    assert second.record_ids == ("r1", "r2")
    assert dict(second.metadata) == {"a": "1", "b": "2"}
    assert second.created_at == first.created_at
    assert [h.digest for h in await store.list("ns")] == [first.digest]


# ------------------------------------------------------------------------------ limits
async def test_size_cap_refuses_and_keeps_nothing(store: DocumentStore) -> None:
    assert store.max_bytes == CAP
    with pytest.raises(DocumentTooLargeError):
        await store.put("ns", b"x" * (CAP + 1))
    with pytest.raises(DocumentTooLargeError):
        await store.put("ns", _chunks(b"x" * CAP, b"y"))
    assert await store.list("ns") == []
    exact = await store.put("ns", b"z" * CAP)
    assert exact.size == CAP


async def test_expected_digest_mismatch_keeps_nothing(store: DocumentStore) -> None:
    with pytest.raises(StorageError, match="expected_digest"):
        await store.put("ns", b"payload", expected_digest=_sha(b"other"))
    assert await store.list("ns") == []
    ok = await store.put("ns", b"payload", expected_digest=_sha(b"payload"))
    assert ok.digest == _sha(b"payload")


@pytest.mark.parametrize("digest", ["", "ABC", "../" + "a" * 61, "g" * 64, "a" * 63])
async def test_invalid_digests_are_refused(store: DocumentStore, digest: str) -> None:
    with pytest.raises(ValueError, match="digest"):
        await store.get("ns", digest)


async def test_invalid_metadata_is_refused(store: DocumentStore) -> None:
    with pytest.raises(ValueError, match="metadata"):
        await store.put("ns", b"m", metadata={"bad key/": "v"})


# ------------------------------------------------------------------------------ namespaces
async def test_namespaces_are_isolated(store: DocumentStore) -> None:
    head = await store.put("tenant-a", b"secret")
    assert await store.get("tenant-b", head.digest) is None
    assert await store.head("tenant-b", head.digest) is None
    assert await store.list("tenant-b") == []
    assert not await store.delete("tenant-b", head.digest)
    assert await store.get("tenant-a", head.digest) == b"secret"
    odd = "ns/with:odd\\chars/../x"
    other = await store.put(odd, b"secret")
    assert [h.namespace for h in await store.list(odd)] == [odd]
    assert other.digest == head.digest  # same bytes, separate document per namespace


async def test_list_orders_by_digest(store: DocumentStore) -> None:
    digests = [(await store.put("ns", bytes([i]) * 3)).digest for i in range(5)]
    assert [h.digest for h in await store.list("ns")] == sorted(digests)


async def test_delete_removes_one_document(store: DocumentStore) -> None:
    keep = await store.put("ns", b"keep")
    drop = await store.put("ns", b"drop")
    assert await store.delete("ns", drop.digest)
    assert await store.get("ns", drop.digest) is None
    assert [h.digest for h in await store.list("ns")] == [keep.digest]


# ------------------------------------------------------------------------------ erasure hook
async def test_erase_records_drops_references_and_last_reference_deletes(
    store: DocumentStore,
) -> None:
    shared_doc = await store.put("ns", b"shared", record_ids=["r1", "r2"])
    only_r1 = await store.put("ns", b"only-r1", record_ids=["r1"])
    unreferenced = await store.put("ns", b"no refs")
    other_ns = await store.put("other", b"only-r1", record_ids=["r1"])
    deleted = await store.erase_records("ns", ["r1"])
    assert deleted == [only_r1.digest]
    assert await store.get("ns", only_r1.digest) is None
    kept = await store.head("ns", shared_doc.digest)
    assert kept is not None and kept.record_ids == ("r2",)
    assert await store.get("ns", unreferenced.digest) == b"no refs"
    assert await store.get("other", other_ns.digest) == b"only-r1"  # scoped to the namespace
    assert await store.erase_records("ns", ["r2"]) == [shared_doc.digest]
    assert await store.erase_records("ns", []) == []


async def test_erase_namespace_removes_everything_in_it(store: DocumentStore) -> None:
    for i in range(3):
        await store.put("gone", bytes([i]) * 4, record_ids=[f"r{i}"])
    survivor = await store.put("stays", b"survivor")
    assert await store.erase_namespace("gone") == 3
    assert await store.list("gone") == []
    assert await store.get("stays", survivor.digest) == b"survivor"
    assert await store.erase_namespace("gone") == 0


async def test_concurrent_puts_of_the_same_bytes_merge_every_reference(
    store: DocumentStore,
) -> None:
    heads = await asyncio.gather(
        *(store.put("ns", b"race", record_ids=[f"r{i}"]) for i in range(8))
    )
    assert len({h.digest for h in heads}) == 1
    final = await store.head("ns", heads[0].digest)
    assert final is not None and set(final.record_ids) == {f"r{i}" for i in range(8)}
