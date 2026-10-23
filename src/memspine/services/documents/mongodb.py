"""MongoDB GridFS DocumentStore adapter (ADR-081 ``storage.documents: {provider: mongodb}``, RM-112).

Implements :class:`~memspine.services.documents.base.DocumentStore` over an injected, opened
:class:`~memspine.clients.mongodb.MongoConnector` (the RM-113 standard: the adapter never opens a
connection; pymongo names come from ``clients/mongodb/driver.py``). Layout, for bucket ``<b>``:

* ``<b>.files`` / ``<b>.chunks`` - GridFS: the bytes, one GridFS file per stored document
  (written in chunks while hashing, so a stream never sits in memory whole);
* ``<b>.heads`` - one head per ``(namespace, digest)``: size, media type, metadata, record
  references, created_at, the GridFS file id and a revision counter.

A put uploads first and binds the head second. A new head is an insert (a concurrent first
writer wins on the unique ``_id`` and the loser merges into it); a merge into an existing head
is a compare-and-set on its revision, retried on conflict, so concurrent puts of the same bytes
keep every reference. A put whose bytes already exist deletes its own upload. The size cap and
an ``expected_digest`` mismatch abort the upload, so nothing is kept.

Locality ``cluster``, not single-writer: every engine and process on the deployment may write.
A crash between the upload and the head leaves an orphaned GridFS file that no head names; it is
invisible to every read and removed by :meth:`erase_namespace`. Deleting a document is a logical
delete (no physical-erasure proof: replica oplogs, backups and storage snapshots keep bytes).
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memspine.clients.mongodb.driver import ASCENDING, DuplicateKeyError, NoFile
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
    from memspine.clients.mongodb import MongoConnector
    from memspine.clients.mongodb.driver import AsyncCollection, AsyncGridFSBucket

__all__ = ["MongoDocumentStore"]

Doc = dict[str, Any]

#: Head merges retried on a revision conflict before giving up (each retry re-reads the head).
_CAS_ATTEMPTS = 64


def _check_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("document namespace must be a non-empty string")
    return namespace


def _key(namespace: str, digest: str) -> Doc:
    return {"ns": namespace, "d": digest}


def _head(doc: Doc) -> DocumentHead:
    return DocumentHead(
        namespace=str(doc["namespace"]),
        digest=str(doc["digest"]),
        size=int(doc["size"]),
        media_type=str(doc["media_type"]),
        metadata={str(k): str(v) for k, v in json.loads(doc["metadata"]).items()},
        record_ids=tuple(str(r) for r in doc.get("record_ids", ())),
        created_at=str(doc["created_at"]),
    )


def _metadata_json(metadata: Mapping[str, str]) -> str:
    # Metadata keys may hold "." (a path separator in MongoDB), so they are stored as JSON.
    return json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"))


async def _iter_chunks(
    data: bytes | Iterable[bytes] | AsyncIterable[bytes],
) -> AsyncIterator[bytes]:
    if isinstance(data, bytes | bytearray | memoryview):
        yield bytes(data)
    elif isinstance(data, AsyncIterable):
        async for chunk in data:
            yield bytes(chunk)
    else:
        for chunk in data:
            yield bytes(chunk)


class MongoDocumentStore:
    """Content-addressed documents in one GridFS bucket (see the module docstring)."""

    def __init__(
        self,
        connector: MongoConnector,
        *,
        bucket: str = "documents",
        max_bytes: int = constants.DOCUMENT_MAX_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._connector = connector
        self._bucket_name = bucket
        self._max_bytes = max_bytes
        self._started = False

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def _heads(self) -> AsyncCollection[Doc]:
        return self._connector.database[f"{self._bucket_name}.heads"]

    @property
    def _gridfs(self) -> AsyncGridFSBucket:
        return self._connector.gridfs_bucket(self._bucket_name)

    async def start(self) -> None:
        """Create the head index (idempotent); GridFS creates its own indexes on first write."""
        await self._heads.create_index(
            [("namespace", ASCENDING), ("digest", ASCENDING)], name="ns_digest"
        )
        await self._heads.create_index(
            [("namespace", ASCENDING), ("record_ids", ASCENDING)], name="ns_refs"
        )
        self._started = True

    async def health(self, deadline_s: float = 5.0) -> bool:
        return self._started and await self._connector.health(deadline_s)

    # -- writes ----------------------------------------------------------------------------
    async def _upload(
        self, namespace: str, data: bytes | Iterable[bytes] | AsyncIterable[bytes]
    ) -> tuple[Any, str, int]:
        """Stream ``data`` into a new GridFS file while hashing; abort it past the cap."""
        hasher = hashlib.sha256()
        size = 0
        upload = self._gridfs.open_upload_stream(
            "staged", metadata={"namespace": namespace, "state": "staged"}
        )
        try:
            async for chunk in _iter_chunks(data):
                size += len(chunk)
                if size > self._max_bytes:
                    raise DocumentTooLargeError(
                        f"document exceeds storage.documents max_bytes={self._max_bytes}"
                    )
                hasher.update(chunk)
                await upload.write(chunk)
            await upload.close()
        except BaseException:
            with contextlib.suppress(Exception):
                await upload.abort()
            raise
        return upload._id, hasher.hexdigest(), size

    async def _drop_file(self, file_id: Any) -> None:
        with contextlib.suppress(NoFile):
            await self._gridfs.delete(file_id)

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
        refs = list(dict.fromkeys(str(r) for r in record_ids if r))
        if expected_digest is not None:
            check_digest(expected_digest)
        if not media_type or not isinstance(media_type, str):
            raise ValueError("media_type must be a non-empty string")
        file_id, digest, size = await self._upload(namespace, data)
        if expected_digest is not None and digest != expected_digest:
            await self._drop_file(file_id)
            raise StorageError("document bytes do not match expected_digest")
        key = _key(namespace, digest)
        try:
            for _ in range(_CAS_ATTEMPTS):
                current = await self._heads.find_one({"_id": key})
                if current is None:
                    fresh: Doc = {
                        "_id": key,
                        "namespace": namespace,
                        "digest": digest,
                        "size": size,
                        "media_type": media_type,
                        "metadata": _metadata_json(extra),
                        "record_ids": refs,
                        "created_at": datetime.now(UTC).isoformat(),
                        "file_id": file_id,
                        "rev": 0,
                    }
                    try:
                        await self._heads.insert_one(fresh)
                    except DuplicateKeyError:
                        continue  # a concurrent first put won: merge into its head
                    file_id = None  # the head now owns the upload
                    return _head(fresh)
                merged_refs = list(dict.fromkeys((*current.get("record_ids", ()), *refs)))
                merged_meta = {**json.loads(current["metadata"]), **extra}
                update = {"record_ids": merged_refs, "metadata": _metadata_json(merged_meta)}
                if all(current.get(k) == v for k, v in update.items()):
                    return _head(current)
                result = await self._heads.update_one(
                    {"_id": key, "rev": current["rev"]},
                    {"$set": update, "$inc": {"rev": 1}},
                )
                if result.matched_count == 1:
                    return _head({**current, **update})
            raise StorageError("document head kept changing under concurrent puts; retry")
        finally:
            if file_id is not None:  # the bytes already existed (or the put failed)
                await self._drop_file(file_id)

    # -- reads -----------------------------------------------------------------------------
    async def _find(self, namespace: str, digest: str) -> Doc | None:
        check_digest(digest)
        _check_namespace(namespace)
        return await self._heads.find_one({"_id": _key(namespace, digest)})

    async def get(self, namespace: str, digest: str) -> bytes | None:
        doc = await self._find(namespace, digest)
        if doc is None:
            return None
        try:
            download = await self._gridfs.open_download_stream(doc["file_id"])
        except NoFile:
            return None  # deleted between the head read and the download
        return bytes(await download.read())

    async def stream(
        self,
        namespace: str,
        digest: str,
        *,
        chunk_size: int = constants.DOCUMENT_STREAM_CHUNK_BYTES,
    ) -> AsyncIterator[bytes]:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        doc = await self._find(namespace, digest)
        if doc is None:
            raise DocumentNotFoundError("no such document in this namespace")
        try:
            download = await self._gridfs.open_download_stream(doc["file_id"])
        except NoFile:
            raise DocumentNotFoundError("no such document in this namespace") from None
        while chunk := await download.read(chunk_size):
            yield bytes(chunk)

    async def head(self, namespace: str, digest: str) -> DocumentHead | None:
        doc = await self._find(namespace, digest)
        return None if doc is None else _head(doc)

    async def list(self, namespace: str) -> builtins.list[DocumentHead]:
        _check_namespace(namespace)
        cursor = self._heads.find({"namespace": namespace}, sort=[("digest", ASCENDING)])
        return [_head(doc) async for doc in cursor]

    # -- removals and erasure --------------------------------------------------------------
    async def _remove(self, doc: Doc) -> bool:
        """Delete one head (only the revision that was read) and then its bytes."""
        result = await self._heads.delete_one({"_id": doc["_id"], "rev": doc["rev"]})
        if result.deleted_count != 1:
            return False
        await self._drop_file(doc["file_id"])
        return True

    async def delete(self, namespace: str, digest: str) -> bool:
        for _ in range(_CAS_ATTEMPTS):
            doc = await self._find(namespace, digest)
            if doc is None:
                return False
            if await self._remove(doc):
                return True
        raise StorageError("document head kept changing under concurrent writes; retry")

    async def erase_records(self, namespace: str, record_ids: Sequence[str]) -> builtins.list[str]:
        _check_namespace(namespace)
        erased = [str(r) for r in record_ids if r]
        if not erased:
            return []
        deleted: builtins.list[str] = []
        cursor = self._heads.find(
            {"namespace": namespace, "record_ids": {"$in": erased}}, sort=[("digest", ASCENDING)]
        )
        for doc in [d async for d in cursor]:
            for _ in range(_CAS_ATTEMPTS):
                kept = [r for r in doc.get("record_ids", ()) if r not in set(erased)]
                if not kept:
                    if await self._remove(doc):
                        deleted.append(str(doc["digest"]))
                        break
                else:
                    result = await self._heads.update_one(
                        {"_id": doc["_id"], "rev": doc["rev"]},
                        {"$set": {"record_ids": kept}, "$inc": {"rev": 1}},
                    )
                    if result.matched_count == 1:
                        break
                again = await self._heads.find_one({"_id": doc["_id"]})
                if again is None:
                    break  # deleted concurrently
                doc = again
            else:
                raise StorageError("document head kept changing during erasure; retry")
        return deleted

    async def erase_namespace(self, namespace: str) -> int:
        _check_namespace(namespace)
        count = 0
        async for doc in self._heads.find({"namespace": namespace}):
            await self._heads.delete_one({"_id": doc["_id"]})
            await self._drop_file(doc["file_id"])
            count += 1
        # Orphaned uploads of this namespace (a crash between upload and head) go too.
        async for grid_file in self._gridfs.find({"metadata.namespace": namespace}):
            await self._drop_file(grid_file._id)
        return count
