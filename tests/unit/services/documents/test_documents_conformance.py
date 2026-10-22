"""ADR-081 ``storage.documents``: one conformance suite for every DocumentStore provider.

Each provider is built through its registered descriptor (settings -> spec -> shared connector
-> adapter), exactly as the container builds it. ``filesystem`` runs everywhere. The planned
providers (RM-112) are listed so their factories join the suite when they ship: ``mongodb``
(GridFS, needs ``MEMSPINE_TEST_MONGODB_URL``), ``s3`` (needs ``MEMSPINE_TEST_S3_URL``) and
``weaviate`` (needs ``MEMSPINE_WEAVIATE_URL``). Until a descriptor is registered under the name
the case is skipped with the reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Callable
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


CASES = (
    Case("filesystem", lambda tmp: {"root": str(tmp / "docs"), "max_bytes": CAP}),
    Case("mongodb", lambda tmp: {"max_bytes": CAP}, env="MEMSPINE_TEST_MONGODB_URL"),
    Case("s3", lambda tmp: {"max_bytes": CAP}, env="MEMSPINE_TEST_S3_URL"),
    Case("weaviate", lambda tmp: {"max_bytes": CAP}, env="MEMSPINE_WEAVIATE_URL"),
)


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


async def _build(case: Case, tmp_path: Path, resources: ProviderLifecycleManager) -> Any:
    registry = get_registry("document_stores")
    descriptor = registry.find(case.name)
    if descriptor is None:
        pytest.skip(f"documents provider {case.name!r} is planned (RM-112), not registered yet")
    if case.env is not None and not os.environ.get(case.env):
        pytest.skip(f"documents provider {case.name!r}: set {case.env}")
    settings = validate_settings(descriptor, case.settings(tmp_path))
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
