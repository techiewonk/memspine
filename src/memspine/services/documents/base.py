"""DocumentStore port (ADR-081 ``storage.documents``): non-structured payloads.

Attachments, assets, large payloads and ingest blobs live here; the event log keeps only the
reference (namespace plus content hash). A document is addressed by ``(namespace, digest)``,
where ``digest`` is the lowercase hex SHA-256 of its bytes, so the same bytes put twice in one
namespace are one document. Namespaces never share documents: the same bytes in two
namespaces are two documents, and every call is scoped to one namespace.

A document may name the records that reference it (``record_ids``). The erasure hook
:meth:`DocumentStore.erase_records` drops those references when the records are hard-erased
and deletes a document whose last reference went; a document put without references is left
to :meth:`DocumentStore.delete` and :meth:`DocumentStore.erase_namespace`. Removing a file or
an object is a logical delete: it is not physical-erasure proof (media remanence, snapshots,
object versioning), as for SQL ``DELETE``.

Conformance: ``tests/unit/services/documents/test_documents_conformance.py`` runs one suite
against every provider (filesystem today; MongoDB GridFS, S3 and Weaviate are RM-112).
"""

from __future__ import annotations

import builtins
import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from memspine.exceptions import StorageError

__all__ = [
    "DIGEST_PATTERN",
    "DocumentHead",
    "DocumentNotFoundError",
    "DocumentStore",
    "DocumentTooLargeError",
    "check_digest",
    "check_metadata",
]

#: A document address: lowercase hex SHA-256.
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
#: Metadata keys: short identifiers; values are strings (the store is not a query engine).
_METADATA_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class DocumentTooLargeError(StorageError):
    """The payload exceeded the provider's ``max_bytes``; nothing was stored."""


class DocumentNotFoundError(StorageError):
    """No document with that digest exists in the namespace."""


@dataclass(frozen=True, slots=True)
class DocumentHead:
    """What a document is, without its bytes."""

    namespace: str
    digest: str
    size: int
    media_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    record_ids: tuple[str, ...] = ()
    created_at: str = ""


def check_digest(digest: str) -> str:
    """Return ``digest`` when it is a lowercase hex SHA-256, else raise ``ValueError``."""
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("document digest must be a lowercase hex SHA-256")
    return digest


def check_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Validate metadata (string keys matching ``[A-Za-z0-9_.:-]{1,64}``, string values)."""
    out: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if not isinstance(key, str) or _METADATA_KEY.fullmatch(key) is None:
            raise ValueError("document metadata keys must match [A-Za-z0-9_.:-]{1,64}")
        if not isinstance(value, str):
            raise ValueError("document metadata values must be strings")
        out[key] = value
    return out


@runtime_checkable
class DocumentStore(Protocol):
    """Namespace-scoped, content-addressed document storage."""

    @property
    def max_bytes(self) -> int:
        """The per-document byte cap this store enforces."""
        ...

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
        """Store bytes (or a stream of chunks) and return the head.

        Idempotent by content: putting bytes that already exist in ``namespace`` stores
        nothing new; ``record_ids`` are added to the document's references and ``metadata``
        keys are merged (new values win). More than :attr:`max_bytes` raises
        :class:`DocumentTooLargeError`; a mismatching ``expected_digest`` raises
        :class:`~memspine.exceptions.StorageError`. Either way nothing is kept."""
        ...

    async def get(self, namespace: str, digest: str) -> bytes | None:
        """The document's bytes, or None when it does not exist."""
        ...

    def stream(self, namespace: str, digest: str, *, chunk_size: int = ...) -> AsyncIterator[bytes]:
        """The document's bytes in chunks; a missing document raises
        :class:`DocumentNotFoundError` on the first iteration."""
        ...

    async def head(self, namespace: str, digest: str) -> DocumentHead | None:
        """The document's head, or None when it does not exist."""
        ...

    async def delete(self, namespace: str, digest: str) -> bool:
        """Remove one document; False when it did not exist."""
        ...

    async def list(self, namespace: str) -> builtins.list[DocumentHead]:
        """Every document of ``namespace``, ordered by digest."""
        ...

    async def erase_records(self, namespace: str, record_ids: Sequence[str]) -> builtins.list[str]:
        """Erasure hook: drop ``record_ids`` from every document's references and delete the
        documents left with none. Returns the deleted digests."""
        ...

    async def erase_namespace(self, namespace: str) -> int:
        """Delete every document of ``namespace``; returns how many were deleted."""
        ...
