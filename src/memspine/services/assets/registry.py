"""Asset registry port (RM-113 item 18) and its in-process adapter.

The registry is a cache of identity and derived evidence, not a second source of truth: the
record carries the ``asset:<id>`` tag, the registry row carries the URI and what resolving it
gave. Rows are keyed by ``(namespace, asset_id)``. The durable adapter is
:class:`~memspine.services.assets.sql_registry.SqlAssetRegistry` (tables of the records
database); :class:`MemoryAssetRegistry` serves a records store without SQL and the tests.
Downloaded bytes are not here: they go to the ``storage.documents`` slot by content hash.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from memspine.services.assets.models import AssetEntry

__all__ = ["AssetImportResult", "AssetRegistry", "MemoryAssetRegistry"]


@dataclass(frozen=True, slots=True)
class AssetImportResult:
    """One import of a legacy asset directory (``registry.sqlite`` plus ``blobs/``)."""

    source: str
    rows: int
    skipped: int
    documents: int
    already_imported: bool
    renamed_to: tuple[str, ...] = ()


@runtime_checkable
class AssetRegistry(Protocol):
    """Namespace-scoped asset rows. Every method is a short transaction of its own."""

    async def register(self, entry: AssetEntry) -> AssetEntry:
        """Idempotent: an asset already registered keeps its resolved state; only a missing
        ``record_id`` is filled in. Returns the stored row."""
        ...

    async def update(self, entry: AssetEntry) -> None:
        """Store ``entry`` as it is (insert or replace)."""
        ...

    async def get(self, namespace: str, asset_id: str) -> AssetEntry | None: ...

    async def all(self, namespace: str) -> builtins.list[AssetEntry]:
        """Every row of ``namespace``, ordered by asset id."""
        ...

    async def with_hash(self, namespace: str, content_hash: str) -> AssetEntry | None:
        """Another row of ``namespace`` whose bytes hashed the same and already has evidence."""
        ...

    async def hash_in_use(self, namespace: str, content_hash: str) -> bool:
        """True while any row of ``namespace`` still references ``content_hash``."""
        ...

    async def erase_records(
        self, namespace: str, record_ids: Sequence[str]
    ) -> builtins.list[AssetEntry]:
        """Erasure hook: delete the rows carried by ``record_ids``; returns them."""
        ...

    async def erase_namespace(self, namespace: str) -> builtins.list[AssetEntry]:
        """Delete every row of ``namespace``; returns them."""
        ...

    async def import_rows(
        self, entries: Sequence[AssetEntry], *, digest: str, source: str
    ) -> int | None:
        """One-time import: insert ``entries`` (existing rows win) and record ``digest`` in
        one transaction. None when ``digest`` was imported before (nothing is written)."""
        ...

    async def close(self) -> None: ...


class MemoryAssetRegistry:
    """In-process rows (lost at exit): a records store without SQL, tests and tools."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], AssetEntry] = {}
        self._imports: set[str] = set()

    async def register(self, entry: AssetEntry) -> AssetEntry:
        key = (entry.namespace, entry.asset_id)
        existing = self._rows.get(key)
        if existing is None:
            self._rows[key] = replace(entry)
            return replace(entry)
        if existing.record_id is None and entry.record_id is not None:
            existing.record_id = entry.record_id
        return replace(existing)

    async def update(self, entry: AssetEntry) -> None:
        self._rows[(entry.namespace, entry.asset_id)] = replace(entry)

    async def get(self, namespace: str, asset_id: str) -> AssetEntry | None:
        found = self._rows.get((namespace, asset_id))
        return None if found is None else replace(found)

    async def all(self, namespace: str) -> builtins.list[AssetEntry]:
        return [replace(e) for (ns, _), e in sorted(self._rows.items()) if ns == namespace]

    async def with_hash(self, namespace: str, content_hash: str) -> AssetEntry | None:
        for entry in await self.all(namespace):
            if entry.content_hash == content_hash and entry.evidence_status == "ok":
                return entry
        return None

    async def hash_in_use(self, namespace: str, content_hash: str) -> bool:
        return any(e.content_hash == content_hash for e in await self.all(namespace))

    async def erase_records(
        self, namespace: str, record_ids: Sequence[str]
    ) -> builtins.list[AssetEntry]:
        ids = set(record_ids)
        gone = [e for e in await self.all(namespace) if e.record_id in ids]
        for entry in gone:
            del self._rows[(namespace, entry.asset_id)]
        return gone

    async def erase_namespace(self, namespace: str) -> builtins.list[AssetEntry]:
        gone = await self.all(namespace)
        for entry in gone:
            del self._rows[(namespace, entry.asset_id)]
        return gone

    async def import_rows(
        self, entries: Sequence[AssetEntry], *, digest: str, source: str
    ) -> int | None:
        if digest in self._imports:
            return None
        rows = 0
        for entry in entries:
            key = (entry.namespace, entry.asset_id)
            if key not in self._rows:
                self._rows[key] = replace(entry)
                rows += 1
        self._imports.add(digest)
        return rows

    async def close(self) -> None:
        return None
