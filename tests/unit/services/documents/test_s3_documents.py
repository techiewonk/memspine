"""RM-112: S3 DocumentStore specifics beyond the shared conformance suite.

Fake-connector tests (run everywhere) pin the conditional-write merge, its retry on a lost
race, the in-process fallback and the reference-index layout. Live tests (MinIO, with
``MEMSPINE_TEST_S3_URL``) cover the multipart path and erasure through the index."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from memspine.clients._common.secrets import Secret
from memspine.clients.s3 import (
    ObjectLimitError,
    PreconditionFailedError,
    S3Connector,
    S3Credentials,
    S3Spec,
    StagedObject,
)
from memspine.exceptions import StorageError
from memspine.services.documents.base import DocumentTooLargeError
from memspine.services.documents.s3 import S3DocumentStore

MiB = 1024 * 1024
S3_URL = os.environ.get("MEMSPINE_TEST_S3_URL")
live = pytest.mark.skipif(not S3_URL, reason="set MEMSPINE_TEST_S3_URL (MinIO)")


class FakeS3:
    """The connector's object API over a dict, with ETag conditional writes."""

    def __init__(self, *, lose_races: int = 0) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.lose_races = lose_races
        self.conditional_calls = 0
        self.unconditional_head_writes = 0
        self.reads: list[str] = []
        self._lock = asyncio.Lock()
        self._n = 0

    def describe(self) -> dict[str, Any]:
        return {"kind": "s3", "bucket": "fake"}

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._lock:
            yield

    async def put_bytes(
        self,
        rel: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str:
        if if_match is not None or if_none_match:
            self.conditional_calls += 1
            if self.lose_races > 0:
                self.lose_races -= 1
                raise PreconditionFailedError("lost")
            current = self.objects.get(rel)
            if if_none_match and current is not None:
                raise PreconditionFailedError("exists")
            if if_match is not None and (current is None or current[1] != if_match):
                raise PreconditionFailedError("changed")
        elif rel.endswith(".json"):
            self.unconditional_head_writes += 1
        self._n += 1
        etag = f'"{self._n}"'
        self.objects[rel] = (bytes(data), etag)
        return etag

    async def get_bytes(self, rel: str) -> tuple[bytes, str] | None:
        self.reads.append(rel)
        return self.objects.get(rel)

    async def iter_bytes(self, rel: str, chunk_size: int) -> AsyncIterator[bytes]:
        if rel not in self.objects:
            raise StorageError("missing")
        data = self.objects[rel][0]
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    async def copy(self, source_rel: str, target_rel: str, *, content_type: str) -> None:
        self.objects[target_rel] = self.objects[source_rel]

    async def delete(self, rels: Sequence[str]) -> None:
        for rel in rels:
            self.objects.pop(rel, None)

    async def list_keys(self, rel_prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(rel_prefix))

    async def upload_stream(
        self,
        staging_rel: str,
        chunks: Iterable[bytes] | AsyncIterable[bytes],
        *,
        max_bytes: int,
        part_size: int,
        content_type: str = "application/octet-stream",
    ) -> StagedObject:
        data = bytearray()
        if isinstance(chunks, AsyncIterable):
            async for chunk in chunks:
                data += chunk
        else:
            for chunk in chunks:
                data += chunk
        if len(data) > max_bytes:
            raise ObjectLimitError("over")
        digest = hashlib.sha256(data).hexdigest()
        if len(data) <= part_size:
            return StagedObject(len(data), digest, inline=bytes(data))
        self.objects[staging_rel] = (bytes(data), '"staged"')
        return StagedObject(len(data), digest, key=staging_rel)


def _store(fake: FakeS3, **kwargs: Any) -> S3DocumentStore:
    return S3DocumentStore(cast(S3Connector, fake), **kwargs)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ fake connector
async def test_layout_is_content_addressed_with_a_reference_index() -> None:
    fake = FakeS3()
    store = _store(fake)
    head = await store.put("tenant", b"bytes", record_ids=["r1"])
    ns = hashlib.sha256(b"tenant").hexdigest()[:32]
    rid = hashlib.sha256(b"r1").hexdigest()[:32]
    assert set(fake.objects) == {
        f"ns/{ns}/blobs/{head.digest}",
        f"ns/{ns}/heads/{head.digest}.json",
        f"ns/{ns}/refs/{rid}/{head.digest}",
    }


async def test_a_lost_conditional_write_is_retried_and_merges() -> None:
    fake = FakeS3()
    store = _store(fake)
    await store.put("ns", b"same", record_ids=["r1"])
    fake.lose_races = 3
    head = await store.put("ns", b"same", record_ids=["r2"])
    assert head.record_ids == ("r1", "r2")
    assert fake.conditional_calls == 5  # first create + 3 lost + 1 won


async def test_endless_races_give_up_with_a_storage_error() -> None:
    fake = FakeS3(lose_races=10_000)
    with pytest.raises(StorageError, match="conditional-write races"):
        await _store(fake).put("ns", b"x")


async def test_without_conditional_writes_merges_run_in_the_exclusive_section() -> None:
    fake = FakeS3()
    store = _store(fake, conditional_writes=False)
    heads = await asyncio.gather(
        *(store.put("ns", b"race", record_ids=[f"r{i}"]) for i in range(6))
    )
    final = await store.head("ns", heads[0].digest)
    assert final is not None and set(final.record_ids) == {f"r{i}" for i in range(6)}
    assert fake.conditional_calls == 0 and fake.unconditional_head_writes == 6


async def test_erase_records_reads_only_the_referenced_heads() -> None:
    fake = FakeS3()
    store = _store(fake)
    target = await store.put("ns", b"target", record_ids=["gone"])
    for i in range(20):
        await store.put("ns", f"other-{i}".encode(), record_ids=[f"keep-{i}"])
    fake.reads.clear()
    assert await store.erase_records("ns", ["gone"]) == [target.digest]
    assert [r for r in fake.reads if "/heads/" in r] == [
        f"ns/{hashlib.sha256(b'ns').hexdigest()[:32]}/heads/{target.digest}.json"
    ]
    assert len(await store.list("ns")) == 20


async def test_large_payloads_are_copied_from_staging_and_staging_is_removed() -> None:
    fake = FakeS3()
    store = _store(fake, part_size=5 * MiB, max_bytes=16 * MiB)
    data = b"x" * (6 * MiB)
    head = await store.put("ns", data)
    assert await store.get("ns", head.digest) == data
    assert not [k for k in fake.objects if "/tmp/" in k]
    with pytest.raises(StorageError, match="expected_digest"):
        await store.put("ns", data + b"y", expected_digest=_sha(b"other"))
    assert not [k for k in fake.objects if "/tmp/" in k]
    with pytest.raises(DocumentTooLargeError):
        await store.put("ns", b"z" * (17 * MiB))


def test_adapter_rejects_a_part_size_under_the_s3_minimum() -> None:
    with pytest.raises(ValueError, match="part_size"):
        _store(FakeS3(), part_size=MiB)


def test_describe_never_shows_credentials() -> None:
    spec = S3Spec(
        "memspine-docs",
        2.0,
        5.0,
        endpoint_url="https://s3.example.com",
        credentials=S3Credentials(Secret("AKIAEXAMPLE"), Secret("top-secret")),
    )
    described = repr(S3DocumentStore(S3Connector(spec)).describe())
    assert "top-secret" not in described and "AKIAEXAMPLE" not in described
    assert "memspine-docs" in described and "'credentials': 'static'" in described


# ------------------------------------------------------------------ live (MinIO)
@pytest.fixture
async def live_store() -> AsyncIterator[S3DocumentStore]:
    if not S3_URL:
        pytest.skip("set MEMSPINE_TEST_S3_URL (MinIO)")
    creds = None
    if os.environ.get("MEMSPINE_TEST_S3_ACCESS_KEY"):
        creds = S3Credentials(
            Secret(os.environ["MEMSPINE_TEST_S3_ACCESS_KEY"]),
            Secret(os.environ.get("MEMSPINE_TEST_S3_SECRET_KEY", "")),
        )
    connector = S3Connector(
        S3Spec(
            os.environ.get("MEMSPINE_TEST_S3_BUCKET", "memspine-conformance"),
            10.0,
            60.0,
            prefix=f"adapter/{uuid.uuid4().hex}/",
            endpoint_url=S3_URL,
            region="us-east-1",
            credentials=creds,
            addressing_style="path",
            create_bucket=True,
        )
    )
    await connector.open()
    try:
        yield S3DocumentStore(connector, part_size=5 * MiB)
    finally:
        await connector.delete(await connector.list_keys(""))
        await connector.close()


@live
async def test_live_multipart_document_round_trip_and_cap(live_store: S3DocumentStore) -> None:
    data = os.urandom(13 * MiB + 5)

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(data), MiB):
            yield data[i : i + MiB]

    head = await live_store.put("ns", chunks(), expected_digest=_sha(data), record_ids=["r"])
    assert head.size == len(data) and head.digest == _sha(data)
    assert await live_store.get("ns", head.digest) == data
    streamed = [c async for c in live_store.stream("ns", head.digest, chunk_size=3 * MiB)]
    assert b"".join(streamed) == data and all(len(c) <= 3 * MiB for c in streamed)
    big = S3DocumentStore(live_store._s3, part_size=5 * MiB, max_bytes=8 * MiB)
    with pytest.raises(DocumentTooLargeError):
        await big.put("ns2", chunks())
    assert await big.list("ns2") == []
    keys = await live_store._s3.list_keys("")
    assert not [k for k in keys if "/tmp/" in k]
    assert await live_store.erase_records("ns", ["r"]) == [head.digest]
    assert await live_store._s3.list_keys("") == []


@live
async def test_live_concurrent_puts_merge_through_conditional_writes(
    live_store: S3DocumentStore,
) -> None:
    heads = await asyncio.gather(
        *(live_store.put("ns", b"race", record_ids=[f"r{i}"]) for i in range(12))
    )
    final = await live_store.head("ns", heads[0].digest)
    assert final is not None and set(final.record_ids) == {f"r{i}" for i in range(12)}
