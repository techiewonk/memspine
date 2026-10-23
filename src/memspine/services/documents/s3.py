"""S3 DocumentStore adapter (ADR-081 ``storage.documents: {provider: s3}``, RM-112).

Implements :class:`~memspine.services.documents.base.DocumentStore` over an injected, opened
:class:`~memspine.clients.s3.S3Connector`; every object operation goes through the connector
(RM-113 standard: no boto3 here). Keys, relative to the connector's prefix::

    ns/<ns>/blobs/<digest>           the bytes (content-addressed)
    ns/<ns>/heads/<digest>.json      the head: namespace, size, media type, metadata, references
    ns/<ns>/refs/<rid>/<digest>      an empty marker per (record, document) reference
    ns/<ns>/tmp/<uuid>               a multipart upload in flight (copied into place, deleted)

``<ns>`` is the first 32 hex characters of SHA-256(namespace) and ``<rid>`` the first 32 of
SHA-256(record id), so any string maps to a safe key; the namespace itself is recorded in the
head. **Reference index:** the ``refs/<rid>/`` markers let :meth:`S3DocumentStore.erase_records`
find a record's documents with one ``ListObjectsV2`` per erased record instead of reading every
head of the namespace; :meth:`S3DocumentStore.erase_namespace` lists and deletes only the
namespace's prefix, never the whole bucket.

**Writes.** A payload of at most one part is hashed in memory and written with one
``PutObject``; a larger one streams as a multipart upload to ``tmp/`` (one part in memory),
then a server-side ``CopyObject`` puts it at its content address. The blob is written before
the reference markers, and the head last: a head always points at stored bytes. The head is
updated with conditional writes (``If-None-Match: *`` to create, ``If-Match: <etag>`` to merge
references and metadata), retried on a lost race, so concurrent puts from any number of
processes merge every reference. With ``conditional_writes: false`` (a store without
``If-Match`` support) the merge runs in the connector's in-process exclusive section instead,
which is safe within one process only.

Not linearizable: a ``delete``/``erase_*`` racing a ``put`` of the same bytes may leave a head
whose blob was removed (``get`` then returns None) or remove a just-added reference. Deleting
an object is a logical delete, not physical-erasure proof (versioned buckets keep old
versions; configure a lifecycle rule). Abandoned multipart uploads (a crashed process) are
reaped by an ``AbortIncompleteMultipartUpload`` lifecycle rule; an orphaned ``tmp/`` object is
removed with its namespace.
"""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import hashlib
import json
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memspine.config import constants
from memspine.exceptions import StorageError
from memspine.services.documents.base import (
    DocumentHead,
    DocumentNotFoundError,
    DocumentTooLargeError,
    check_digest,
    check_metadata,
)

if TYPE_CHECKING:
    from memspine.clients.s3 import S3Connector

__all__ = ["S3DocumentStore"]

#: Conditional-write retries of one head update before giving up (each retry re-reads).
_CAS_ATTEMPTS = 64
#: Concurrent head reads while listing a namespace.
_LIST_CONCURRENCY = 16


def _hash32(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _namespace_dir(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("document namespace must be a non-empty string")
    return f"ns/{_hash32(namespace)}"


def _head_from(raw: bytes) -> DocumentHead:
    data: dict[str, Any] = json.loads(raw)
    return DocumentHead(
        namespace=str(data["namespace"]),
        digest=str(data["digest"]),
        size=int(data["size"]),
        media_type=str(data["media_type"]),
        metadata={str(k): str(v) for k, v in dict(data.get("metadata", {})).items()},
        record_ids=tuple(str(r) for r in data.get("record_ids", ())),
        created_at=str(data.get("created_at", "")),
    )


def _head_bytes(head: DocumentHead) -> bytes:
    return json.dumps(
        {
            "namespace": head.namespace,
            "digest": head.digest,
            "size": head.size,
            "media_type": head.media_type,
            "metadata": dict(head.metadata),
            "record_ids": list(head.record_ids),
            "created_at": head.created_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _with(head: DocumentHead, **changes: Any) -> DocumentHead:
    return dataclasses.replace(head, **changes)


class S3DocumentStore:
    """Content-addressed documents under one bucket prefix (see the module docstring)."""

    def __init__(
        self,
        connector: S3Connector,
        *,
        max_bytes: int = constants.DOCUMENT_MAX_BYTES,
        part_size: int = constants.DOCUMENT_S3_PART_BYTES,
        conditional_writes: bool = True,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if part_size < constants.DOCUMENT_S3_MIN_PART_BYTES:
            raise ValueError(
                f"part_size must be at least {constants.DOCUMENT_S3_MIN_PART_BYTES} bytes"
            )
        self._s3 = connector
        self._max_bytes = max_bytes
        self._part_size = part_size
        self._conditional = conditional_writes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def describe(self) -> dict[str, Any]:
        """Safe facts for diagnostics (no credentials)."""
        return {
            "provider": "s3",
            **self._s3.describe(),
            "max_bytes": self._max_bytes,
            "part_size": self._part_size,
            "conditional_writes": self._conditional,
        }

    # -- keys ------------------------------------------------------------------------------
    @staticmethod
    def _blob(namespace: str, digest: str) -> str:
        return f"{_namespace_dir(namespace)}/blobs/{digest}"

    @staticmethod
    def _meta(namespace: str, digest: str) -> str:
        return f"{_namespace_dir(namespace)}/heads/{digest}.json"

    @staticmethod
    def _ref(namespace: str, record_id: str, digest: str) -> str:
        return f"{_namespace_dir(namespace)}/refs/{_hash32(record_id)}/{digest}"

    def _section(self) -> AbstractAsyncContextManager[Any]:
        return nullcontext() if self._conditional else self._s3.exclusive()

    async def _read_head(self, namespace: str, digest: str) -> tuple[DocumentHead, str] | None:
        got = await self._s3.get_bytes(self._meta(namespace, digest))
        if got is None:
            return None
        head = _head_from(got[0])
        return (head, got[1]) if head.namespace == namespace else None

    async def _write_head(self, head: DocumentHead, etag: str | None) -> None:
        """Create (``etag`` None) or replace the head; conditional unless disabled."""
        key = self._meta(head.namespace, head.digest)
        if not self._conditional:
            await self._s3.put_bytes(key, _head_bytes(head), content_type="application/json")
            return
        await self._s3.put_bytes(
            key,
            _head_bytes(head),
            content_type="application/json",
            if_match=etag,
            if_none_match=etag is None,
        )

    # -- writes ----------------------------------------------------------------------------
    async def put(
        self,
        namespace: str,
        data: bytes | Iterable[bytes] | AsyncIterable[bytes],
        *,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, str] | None = None,
        record_ids: Sequence[str] = (),
        expected_digest: str | None = None,
    ) -> DocumentHead:
        from memspine.clients.s3 import ObjectLimitError

        ns_dir = _namespace_dir(namespace)
        extra = check_metadata(metadata)
        refs = tuple(dict.fromkeys(str(r) for r in record_ids if r))
        if expected_digest is not None:
            check_digest(expected_digest)
        if not media_type or not isinstance(media_type, str):
            raise ValueError("media_type must be a non-empty string")
        chunks: Iterable[bytes] | AsyncIterable[bytes] = (
            (bytes(data),) if isinstance(data, bytes | bytearray | memoryview) else data
        )
        try:
            staged = await self._s3.upload_stream(
                f"{ns_dir}/tmp/{uuid.uuid4().hex}",
                chunks,
                max_bytes=self._max_bytes,
                part_size=self._part_size,
                content_type=media_type,
            )
        except ObjectLimitError:
            raise DocumentTooLargeError(
                f"document exceeds storage.documents max_bytes={self._max_bytes}"
            ) from None
        try:
            if expected_digest is not None and staged.sha256 != expected_digest:
                raise StorageError("document bytes do not match expected_digest")
            blob = self._blob(namespace, staged.sha256)
            # Always (re)write the blob: identical bytes, and a racing delete cannot leave
            # this put's head pointing at nothing it did not just write.
            if staged.inline is not None:
                await self._s3.put_bytes(blob, staged.inline, content_type=media_type)
            else:
                assert staged.key is not None
                await self._s3.copy(staged.key, blob, content_type=media_type)
        finally:
            if staged.key is not None:
                await self._s3.delete([staged.key])
        for record_id in refs:
            await self._s3.put_bytes(self._ref(namespace, record_id, staged.sha256), b"")
        return await self._merge_head(
            namespace, staged.sha256, staged.size, media_type, extra, refs
        )

    async def _merge_head(
        self,
        namespace: str,
        digest: str,
        size: int,
        media_type: str,
        extra: dict[str, str],
        refs: tuple[str, ...],
    ) -> DocumentHead:
        from memspine.clients.s3 import PreconditionFailedError

        async with self._section():
            for attempt in range(_CAS_ATTEMPTS):
                current = await self._read_head(namespace, digest)
                if current is None:
                    head = DocumentHead(
                        namespace=namespace,
                        digest=digest,
                        size=size,
                        media_type=media_type,
                        metadata=extra,
                        record_ids=refs,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                    etag: str | None = None
                else:
                    existing, etag = current
                    head = _with(
                        existing,
                        metadata={**existing.metadata, **extra},
                        record_ids=tuple(dict.fromkeys((*existing.record_ids, *refs))),
                    )
                    if head == existing:
                        return head
                try:
                    await self._write_head(head, etag)
                    return head
                except PreconditionFailedError:
                    await asyncio.sleep(min(0.002 * (attempt + 1), 0.05))
        raise StorageError("S3 document head update kept losing conditional-write races")

    # -- reads -----------------------------------------------------------------------------
    async def get(self, namespace: str, digest: str) -> bytes | None:
        check_digest(digest)
        if await self._read_head(namespace, digest) is None:
            return None
        got = await self._s3.get_bytes(self._blob(namespace, digest))
        return None if got is None else got[0]

    async def stream(
        self,
        namespace: str,
        digest: str,
        *,
        chunk_size: int = constants.DOCUMENT_STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        check_digest(digest)
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if await self._read_head(namespace, digest) is None:
            raise DocumentNotFoundError("no such document in this namespace")
        try:
            async for chunk in self._s3.iter_bytes(self._blob(namespace, digest), chunk_size):
                yield chunk
        except DocumentNotFoundError:
            raise
        except StorageError:
            raise DocumentNotFoundError("no such document in this namespace") from None

    async def head(self, namespace: str, digest: str) -> DocumentHead | None:
        check_digest(digest)
        current = await self._read_head(namespace, digest)
        return None if current is None else current[0]

    async def list(self, namespace: str) -> builtins.list[DocumentHead]:
        keys = await self._s3.list_keys(f"{_namespace_dir(namespace)}/heads/")
        gate = asyncio.Semaphore(_LIST_CONCURRENCY)

        async def read(key: str) -> DocumentHead | None:
            async with gate:
                got = await self._s3.get_bytes(key)
            if got is None:
                return None
            head = _head_from(got[0])
            return head if head.namespace == namespace else None

        heads = [h for h in await asyncio.gather(*(read(k) for k in keys)) if h is not None]
        return sorted(heads, key=lambda h: h.digest)

    # -- removals and erasure --------------------------------------------------------------
    async def _remove(self, head: DocumentHead) -> None:
        ns, digest = head.namespace, head.digest
        # Head first: once it is gone the document is invisible, whatever fails after.
        await self._s3.delete([self._meta(ns, digest)])
        await self._s3.delete(
            [self._blob(ns, digest), *(self._ref(ns, r, digest) for r in head.record_ids)]
        )

    async def delete(self, namespace: str, digest: str) -> bool:
        check_digest(digest)
        async with self._section():
            current = await self._read_head(namespace, digest)
            if current is None:
                return False
            await self._remove(current[0])
        return True

    async def erase_records(self, namespace: str, record_ids: Sequence[str]) -> builtins.list[str]:
        from memspine.clients.s3 import PreconditionFailedError

        erased = {str(r) for r in record_ids if r}
        if not erased:
            return []
        ns_dir = _namespace_dir(namespace)
        digests: set[str] = set()
        for record_id in erased:
            for key in await self._s3.list_keys(f"{ns_dir}/refs/{_hash32(record_id)}/"):
                digests.add(key.rsplit("/", 1)[-1])
        deleted: builtins.list[str] = []
        async with self._section():
            for digest in sorted(digests):
                markers = [self._ref(namespace, r, digest) for r in erased]
                for attempt in range(_CAS_ATTEMPTS):
                    current = await self._read_head(namespace, digest)
                    if current is None:
                        break
                    head, etag = current
                    kept = tuple(r for r in head.record_ids if r not in erased)
                    if kept == head.record_ids:
                        break
                    if not kept:
                        await self._remove(head)
                        deleted.append(digest)
                        break
                    try:
                        await self._write_head(_with(head, record_ids=kept), etag)
                        break
                    except PreconditionFailedError:
                        await asyncio.sleep(min(0.002 * (attempt + 1), 0.05))
                else:
                    raise StorageError(
                        "S3 document head update kept losing conditional-write races"
                    )
                await self._s3.delete(markers)
        return deleted

    async def erase_namespace(self, namespace: str) -> int:
        ns_dir = _namespace_dir(namespace)
        async with self._section():
            keys = await self._s3.list_keys(f"{ns_dir}/")
            heads = [k for k in keys if k.startswith(f"{ns_dir}/heads/")]
            # Heads first, so a partial failure never leaves a visible head without its bytes.
            await self._s3.delete(heads)
            await self._s3.delete([k for k in keys if not k.startswith(f"{ns_dir}/heads/")])
        return len(heads)
