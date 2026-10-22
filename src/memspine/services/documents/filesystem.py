"""Filesystem DocumentStore adapter (ADR-081 ``storage.documents: {provider: filesystem}``).

Implements :class:`~memspine.services.documents.base.DocumentStore` over an injected, opened
:class:`~memspine.clients.fs.FilesystemConnector`; every file operation goes through the
connector (RM-113 standard). Layout under the root::

    <ns>/<dd>/<digest>.bin    the bytes (written atomically, content-addressed)
    <ns>/<dd>/<digest>.json   the head: namespace, size, media type, metadata, references

``<ns>`` is the first 32 hex characters of SHA-256(namespace), so any namespace string maps
to a safe directory name and the namespace itself is recorded in each head. Read-modify-write
of a head (reference merge, erasure) runs inside the connector's exclusive section; across
processes the store is single-writer (ADR-081 WP6).
"""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
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
    from memspine.clients.fs import FilesystemConnector

__all__ = ["FilesystemDocumentStore"]


def _namespace_dir(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("document namespace must be a non-empty string")
    return hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32]


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


class FilesystemDocumentStore:
    """Content-addressed documents in one root directory (see the module docstring)."""

    def __init__(
        self, connector: FilesystemConnector, *, max_bytes: int = constants.DOCUMENT_MAX_BYTES
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._fs = connector
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @staticmethod
    def _paths(namespace: str, digest: str) -> tuple[str, str]:
        base = f"{_namespace_dir(namespace)}/{digest[:2]}/{digest}"
        return f"{base}.bin", f"{base}.json"

    async def _head(self, namespace: str, digest: str) -> DocumentHead | None:
        _, meta = self._paths(namespace, digest)
        raw = await self._fs.read_bytes(meta)
        if raw is None:
            return None
        head = _head_from(raw)
        return head if head.namespace == namespace else None

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
        from memspine.clients.fs import FileLimitError

        _namespace_dir(namespace)
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
            staged = await self._fs.stage(chunks, max_bytes=self._max_bytes)
        except FileLimitError:
            raise DocumentTooLargeError(
                f"document exceeds storage.documents max_bytes={self._max_bytes}"
            ) from None
        if expected_digest is not None and staged.sha256 != expected_digest:
            await self._fs.discard(staged)
            raise StorageError("document bytes do not match expected_digest")
        blob, meta = self._paths(namespace, staged.sha256)
        async with self._fs.exclusive():
            existing = await self._head(namespace, staged.sha256)
            if existing is not None and await self._fs.exists(blob):
                await self._fs.discard(staged)
                head = DocumentHead(
                    namespace=namespace,
                    digest=existing.digest,
                    size=existing.size,
                    media_type=existing.media_type,
                    metadata={**existing.metadata, **extra},
                    record_ids=tuple(dict.fromkeys((*existing.record_ids, *refs))),
                    created_at=existing.created_at,
                )
            else:
                await self._fs.promote(staged, blob)
                head = DocumentHead(
                    namespace=namespace,
                    digest=staged.sha256,
                    size=staged.size,
                    media_type=media_type,
                    metadata=extra,
                    record_ids=refs,
                    created_at=datetime.now(UTC).isoformat(),
                )
            if head != existing:
                await self._fs.write_atomic(meta, _head_bytes(head))
        return head

    # -- reads -----------------------------------------------------------------------------
    async def get(self, namespace: str, digest: str) -> bytes | None:
        check_digest(digest)
        if await self._head(namespace, digest) is None:
            return None
        blob, _ = self._paths(namespace, digest)
        return await self._fs.read_bytes(blob)

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
        if await self._head(namespace, digest) is None:
            raise DocumentNotFoundError("no such document in this namespace")
        blob, _ = self._paths(namespace, digest)
        try:
            async for chunk in self._fs.iter_bytes(blob, chunk_size):
                yield chunk
        except DocumentNotFoundError:
            raise
        except StorageError:
            raise DocumentNotFoundError("no such document in this namespace") from None

    async def head(self, namespace: str, digest: str) -> DocumentHead | None:
        check_digest(digest)
        return await self._head(namespace, digest)

    async def list(self, namespace: str) -> builtins.list[DocumentHead]:
        heads: builtins.list[DocumentHead] = []
        for rel in await self._fs.list_files(_namespace_dir(namespace), suffix=".json"):
            raw = await self._fs.read_bytes(rel)
            if raw is None:
                continue
            head = _head_from(raw)
            if head.namespace == namespace:
                heads.append(head)
        return sorted(heads, key=lambda h: h.digest)

    # -- removals and erasure --------------------------------------------------------------
    async def _remove(self, namespace: str, digest: str) -> bool:
        blob, meta = self._paths(namespace, digest)
        had_meta = await self._fs.remove(meta)
        had_blob = await self._fs.remove(blob)
        return had_meta or had_blob

    async def delete(self, namespace: str, digest: str) -> bool:
        check_digest(digest)
        async with self._fs.exclusive():
            if await self._head(namespace, digest) is None:
                return False
            return await self._remove(namespace, digest)

    async def erase_records(self, namespace: str, record_ids: Sequence[str]) -> builtins.list[str]:
        erased = set(record_ids)
        if not erased:
            return []
        deleted: builtins.list[str] = []
        async with self._fs.exclusive():
            for head in await self.list(namespace):
                if not erased & set(head.record_ids):
                    continue
                kept = tuple(r for r in head.record_ids if r not in erased)
                if kept:
                    _, meta = self._paths(namespace, head.digest)
                    updated = DocumentHead(
                        namespace=head.namespace,
                        digest=head.digest,
                        size=head.size,
                        media_type=head.media_type,
                        metadata=head.metadata,
                        record_ids=kept,
                        created_at=head.created_at,
                    )
                    await self._fs.write_atomic(meta, _head_bytes(updated))
                else:
                    await self._remove(namespace, head.digest)
                    deleted.append(head.digest)
        return deleted

    async def erase_namespace(self, namespace: str) -> int:
        async with self._fs.exclusive():
            heads = await self.list(namespace)
            for head in heads:
                await self._remove(namespace, head.digest)
            # A namespace directory holds only this namespace's documents (hash collisions of
            # 128 bits aside), so whatever remains (orphaned blobs) goes with it.
            await self._fs.remove_tree(_namespace_dir(namespace))
        return len(heads)
