"""Weaviate DocumentStore adapter (ADR-081 ``storage.documents: {provider: weaviate}``, RM-112).

Implements :class:`~memspine.services.documents.base.DocumentStore` over the shared
:class:`~memspine.clients.weaviate.WeaviateClient` (the vector and lexical slots on the same
deployment use the same client) and a dedicated documents collection
(:class:`~memspine.services._weaviate.documents.WeaviateDocumentsBinding`, created in
:meth:`WeaviateDocumentStore.start`). Weaviate serves documents, never records (ADR-081).

Objects of the collection (deterministic UUIDs, so a replay writes the same objects):

- ``head`` (one per ``(namespace, digest)``): size, media type, metadata (JSON), creation
  time, and the ``upload`` id plus ``chunk_count`` of the chunks that hold the bytes;
- ``chunk``: one BLOB piece of at most ``chunk_bytes`` (default 1 MiB) of an upload. A 64 MiB
  document is 64 objects; no single request approaches Weaviate's message limits, so the
  64 MiB ``max_bytes`` cap holds;
- ``ref``: one per ``(namespace, digest, record id)`` reference. References are separate
  objects, so concurrent puts never read-modify-write a reference list, and
  :meth:`WeaviateDocumentStore.erase_records` finds a record's documents with one filtered
  query instead of reading every head.

**Writes.** A put streams its chunks under a fresh upload id while hashing (one chunk in
memory), then inserts its references, then creates the head. When the head already exists
(the same bytes were put before) the new chunks are deleted and only references and metadata
merge. The head step runs under an Engine-local lock; across Engines and processes the head
create is an insert-if-absent on its deterministic UUID, and a metadata merge is
last-writer-wins (references are always race-free). Weaviate has no multi-object
transactions: a crash mid-put leaves unreferenced chunk objects, which
:meth:`WeaviateDocumentStore.erase_namespace` removes with the namespace. A ``delete`` or
``erase_*`` racing a ``put`` of the same bytes is not linearizable. Deleting an object is a
logical delete, not physical-erasure proof (backups, replicas).

No document is vectorized: the collection has a self-provided ``flat`` index and no object
carries a vector.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memspine.config import constants
from memspine.exceptions import StorageError
from memspine.services._weaviate.documents import (
    WeaviateDocumentsBinding,
    all_of,
    any_value,
    both,
    ensure_documents_collection,
    key_after,
    object_uuid,
    sort_by_key,
)
from memspine.services.documents.base import (
    DocumentHead,
    DocumentNotFoundError,
    DocumentTooLargeError,
    check_digest,
    check_metadata,
)

if TYPE_CHECKING:
    from memspine.clients.weaviate import WeaviateClient

__all__ = ["WeaviateDocumentStore"]

#: Objects per keyset page.
_PAGE = 500
_HEAD_PROPERTIES = [
    "namespace",
    "digest",
    "upload",
    "size",
    "chunk_count",
    "media_type",
    "metadata",
    "created_at",
]


def _check_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("document namespace must be a non-empty string")
    return namespace


def _rid_hash(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16]


class WeaviateDocumentStore:
    """Content-addressed documents in one Weaviate collection (see the module docstring)."""

    def __init__(
        self,
        connector: WeaviateClient,
        binding: WeaviateDocumentsBinding,
        *,
        max_bytes: int = constants.DOCUMENT_MAX_BYTES,
        chunk_bytes: int = constants.DOCUMENT_WEAVIATE_CHUNK_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if not 1 <= chunk_bytes <= constants.DOCUMENT_WEAVIATE_MAX_CHUNK_BYTES:
            raise ValueError(
                f"chunk_bytes must be between 1 and {constants.DOCUMENT_WEAVIATE_MAX_CHUNK_BYTES}"
            )
        self._connector = connector
        self._binding = binding
        self._max_bytes = max_bytes
        self._chunk_bytes = chunk_bytes
        self._collection: Any = None
        self._head_lock = asyncio.Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def collection_name(self) -> str:
        return self._binding.name

    def describe(self) -> dict[str, Any]:
        """Safe facts for diagnostics (no endpoint credentials)."""
        return {
            "provider": "weaviate",
            "collection": self._binding.name,
            "max_bytes": self._max_bytes,
            "chunk_bytes": self._chunk_bytes,
            "open": self._collection is not None,
        }

    async def start(self) -> None:
        if self._collection is None:
            self._collection = await ensure_documents_collection(
                self._connector.handle, self._binding
            )

    async def probe(self, deadline_s: float) -> bool:
        """The connector's readiness plus the documents collection's presence."""
        return bool(
            await self._connector.health(deadline_s)
            and await self._connector.handle.collections.exists(self._binding.name)
        )

    def _coll(self) -> Any:
        if self._collection is None:
            raise StorageError("Weaviate document store is not started — call start() first")
        return self._collection

    # -- object ids ------------------------------------------------------------------------
    def _head_id(self, namespace: str, digest: str) -> str:
        return object_uuid(self._binding.name, "head", namespace, digest)

    def _chunk_id(self, upload: str, seq: int) -> str:
        return object_uuid(self._binding.name, "chunk", upload, str(seq))

    def _ref_id(self, namespace: str, digest: str, record_id: str) -> str:
        return object_uuid(self._binding.name, "ref", namespace, digest, record_id)

    # -- low-level helpers -----------------------------------------------------------------
    async def _insert_absent(self, object_id: str, props: dict[str, Any]) -> bool:
        """Insert unless the id exists; True when this call created it."""
        data = self._coll().data
        if await data.exists(object_id):
            return False
        try:
            await data.insert(properties=props, uuid=object_id)
        except Exception:
            if await data.exists(object_id):
                return False
            raise
        return True

    async def _fetch_all(self, filters: Any, props: builtins.list[str]) -> builtins.list[Any]:
        """Every object matching ``filters``, by ascending ``key`` (keyset pagination, so no
        result window limit applies)."""
        out: builtins.list[Any] = []
        last: str | None = None
        query = self._coll().query
        while True:
            page = await query.fetch_objects(
                filters=key_after(filters, last),
                sort=sort_by_key(),
                limit=_PAGE,
                return_properties=[*props, "key"],
            )
            out.extend(page.objects)
            if len(page.objects) < _PAGE:
                return out
            last = str(page.objects[-1].properties["key"])

    async def _delete_where(self, filters: Any) -> None:
        data = self._coll().data
        while True:
            result = await data.delete_many(where=filters)
            if int(getattr(result, "failed", 0) or 0):
                raise StorageError("Weaviate did not delete every document object")
            if not int(getattr(result, "matches", 0) or 0):
                return

    async def _head_object(self, namespace: str, digest: str) -> Any:
        obj = await self._coll().query.fetch_object_by_id(
            self._head_id(namespace, digest), return_properties=_HEAD_PROPERTIES
        )
        if obj is None or obj.properties.get("namespace") != namespace:
            return None
        return obj

    async def _refs(self, namespace: str, digest: str) -> tuple[str, ...]:
        refs = await self._fetch_all(
            all_of(kind="ref", namespace=namespace, digest=digest), ["record_id"]
        )
        return tuple(str(obj.properties["record_id"]) for obj in refs)

    @staticmethod
    def _head(obj: Any, record_ids: tuple[str, ...]) -> DocumentHead:
        props = obj.properties
        raw = props.get("metadata") or "{}"
        return DocumentHead(
            namespace=str(props["namespace"]),
            digest=str(props["digest"]),
            size=int(props["size"]),
            media_type=str(props["media_type"]),
            metadata={str(k): str(v) for k, v in dict(json.loads(raw)).items()},
            record_ids=record_ids,
            created_at=str(props.get("created_at") or ""),
        )

    async def _drop_upload(self, upload: str) -> None:
        await self._delete_where(all_of(kind="chunk", upload=upload))

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
        _check_namespace(namespace)
        extra = check_metadata(metadata)
        refs = tuple(dict.fromkeys(str(r) for r in record_ids if r))
        if expected_digest is not None:
            check_digest(expected_digest)
        if not media_type or not isinstance(media_type, str):
            raise ValueError("media_type must be a non-empty string")
        chunks: Iterable[bytes] | AsyncIterable[bytes] = (
            (bytes(data),) if isinstance(data, bytes | bytearray | memoryview) else data
        )
        upload = uuid.uuid4().hex
        try:
            size, digest, count = await self._stage(namespace, upload, chunks)
            if expected_digest is not None and digest != expected_digest:
                raise StorageError("document bytes do not match expected_digest")
            for record_id in refs:
                await self._insert_absent(
                    self._ref_id(namespace, digest, record_id),
                    {
                        "kind": "ref",
                        "namespace": namespace,
                        "digest": digest,
                        "record_id": record_id,
                        "key": f"{digest}:{time.time_ns():020d}:{_rid_hash(record_id)}",
                    },
                )
            async with self._head_lock:
                created = await self._create_or_merge_head(
                    namespace, digest, upload, size, count, media_type, extra
                )
        except BaseException:
            await self._drop_upload(upload)
            raise
        if not created:
            await self._drop_upload(upload)
        obj = await self._head_object(namespace, digest)
        if obj is None:
            raise StorageError("Weaviate document head vanished during put")
        return self._head(obj, await self._refs(namespace, digest))

    async def _stage(
        self, namespace: str, upload: str, chunks: Iterable[bytes] | AsyncIterable[bytes]
    ) -> tuple[int, str, int]:
        """Insert the bytes as chunk objects of ``upload``; return (size, sha256, chunks)."""
        hasher = hashlib.sha256()
        size = 0
        seq = 0
        buffer = bytearray()

        async def flush(piece: bytes) -> None:
            nonlocal seq
            await self._coll().data.insert(
                properties={
                    "kind": "chunk",
                    "namespace": namespace,
                    "upload": upload,
                    "seq": seq,
                    "key": f"{upload}:{seq:08d}",
                    "blob": base64.b64encode(piece).decode("ascii"),
                },
                uuid=self._chunk_id(upload, seq),
            )
            seq += 1

        async def take(chunk: bytes) -> None:
            nonlocal size
            if not isinstance(chunk, bytes | bytearray | memoryview):
                raise TypeError("document chunks must be bytes")
            size += len(chunk)
            if size > self._max_bytes:
                raise DocumentTooLargeError(
                    f"document exceeds storage.documents max_bytes={self._max_bytes}"
                )
            hasher.update(chunk)
            buffer.extend(chunk)
            while len(buffer) >= self._chunk_bytes:
                await flush(bytes(buffer[: self._chunk_bytes]))
                del buffer[: self._chunk_bytes]

        if isinstance(chunks, AsyncIterable):
            async for chunk in chunks:
                await take(chunk)
        else:
            for chunk in chunks:
                await take(chunk)
        if buffer:
            await flush(bytes(buffer))
        return size, hasher.hexdigest(), seq

    async def _create_or_merge_head(
        self,
        namespace: str,
        digest: str,
        upload: str,
        size: int,
        count: int,
        media_type: str,
        extra: dict[str, str],
    ) -> bool:
        """Create the head (True) or merge metadata into the existing one (False)."""
        head_id = self._head_id(namespace, digest)
        existing = await self._head_object(namespace, digest)
        if existing is None:
            created = await self._insert_absent(
                head_id,
                {
                    "kind": "head",
                    "namespace": namespace,
                    "digest": digest,
                    "key": digest,
                    "upload": upload,
                    "size": size,
                    "chunk_count": count,
                    "media_type": media_type,
                    "metadata": json.dumps(extra, sort_keys=True),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            if created:
                return True
            existing = await self._head_object(namespace, digest)
            if existing is None:
                raise StorageError("Weaviate document head could not be created")
        current = dict(json.loads(existing.properties.get("metadata") or "{}"))
        merged = {**current, **extra}
        if merged != current:
            await self._coll().data.update(
                uuid=head_id, properties={"metadata": json.dumps(merged, sort_keys=True)}
            )
        return False

    # -- reads -----------------------------------------------------------------------------
    async def _chunk(self, upload: str, seq: int) -> bytes:
        obj = await self._coll().query.fetch_object_by_id(
            self._chunk_id(upload, seq), return_properties=["blob"]
        )
        if obj is None or obj.properties.get("blob") is None:
            raise StorageError("Weaviate document chunk is missing")
        return base64.b64decode(obj.properties["blob"])

    async def get(self, namespace: str, digest: str) -> bytes | None:
        check_digest(digest)
        _check_namespace(namespace)
        obj = await self._head_object(namespace, digest)
        if obj is None:
            return None
        upload = str(obj.properties["upload"])
        count = int(obj.properties["chunk_count"])
        data = b"".join([await self._chunk(upload, seq) for seq in range(count)])
        if hashlib.sha256(data).hexdigest() != digest:
            raise StorageError("Weaviate document bytes do not match their digest")
        return data

    async def stream(
        self,
        namespace: str,
        digest: str,
        *,
        chunk_size: int = constants.DOCUMENT_STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        check_digest(digest)
        _check_namespace(namespace)
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        obj = await self._head_object(namespace, digest)
        if obj is None:
            raise DocumentNotFoundError("no such document in this namespace")
        upload = str(obj.properties["upload"])
        buffer = bytearray()
        for seq in range(int(obj.properties["chunk_count"])):
            buffer += await self._chunk(upload, seq)
            while len(buffer) >= chunk_size:
                yield bytes(buffer[:chunk_size])
                del buffer[:chunk_size]
        if buffer:
            yield bytes(buffer)

    async def head(self, namespace: str, digest: str) -> DocumentHead | None:
        check_digest(digest)
        _check_namespace(namespace)
        obj = await self._head_object(namespace, digest)
        if obj is None:
            return None
        return self._head(obj, await self._refs(namespace, digest))

    async def list(self, namespace: str) -> builtins.list[DocumentHead]:
        _check_namespace(namespace)
        heads = await self._fetch_all(all_of(kind="head", namespace=namespace), _HEAD_PROPERTIES)
        refs: dict[str, builtins.list[str]] = {}
        for obj in await self._fetch_all(
            all_of(kind="ref", namespace=namespace), ["digest", "record_id"]
        ):
            refs.setdefault(str(obj.properties["digest"]), []).append(
                str(obj.properties["record_id"])
            )
        out = [
            self._head(obj, tuple(refs.get(str(obj.properties["digest"]), ())))
            for obj in heads
            if obj.properties.get("namespace") == namespace
        ]
        return sorted(out, key=lambda h: h.digest)

    # -- removals and erasure --------------------------------------------------------------
    async def _remove(self, namespace: str, digest: str, upload: str) -> None:
        # Head first: once it is gone the document is invisible, whatever fails after.
        await self._coll().data.delete_by_id(self._head_id(namespace, digest))
        await self._drop_upload(upload)
        await self._delete_where(all_of(kind="ref", namespace=namespace, digest=digest))

    async def delete(self, namespace: str, digest: str) -> bool:
        check_digest(digest)
        _check_namespace(namespace)
        async with self._head_lock:
            obj = await self._head_object(namespace, digest)
            if obj is None:
                return False
            await self._remove(namespace, digest, str(obj.properties["upload"]))
        return True

    async def erase_records(self, namespace: str, record_ids: Sequence[str]) -> builtins.list[str]:
        _check_namespace(namespace)
        erased = sorted({str(r) for r in record_ids if r})
        if not erased:
            return []
        where = both(all_of(kind="ref", namespace=namespace), any_value("record_id", erased))
        digests = sorted(
            {str(obj.properties["digest"]) for obj in await self._fetch_all(where, ["digest"])}
        )
        deleted: builtins.list[str] = []
        async with self._head_lock:
            await self._delete_where(where)
            for digest in digests:
                remaining = await self._coll().query.fetch_objects(
                    filters=all_of(kind="ref", namespace=namespace, digest=digest),
                    limit=1,
                    return_properties=["record_id"],
                )
                if remaining.objects:
                    continue
                obj = await self._head_object(namespace, digest)
                if obj is None:
                    continue
                await self._remove(namespace, digest, str(obj.properties["upload"]))
                deleted.append(digest)
        return deleted

    async def erase_namespace(self, namespace: str) -> int:
        _check_namespace(namespace)
        async with self._head_lock:
            heads = await self._fetch_all(all_of(kind="head", namespace=namespace), ["digest"])
            # Heads first, so a partial failure never leaves a visible head without its bytes.
            await self._delete_where(all_of(kind="head", namespace=namespace))
            await self._delete_where(all_of(namespace=namespace))
        return len(heads)
