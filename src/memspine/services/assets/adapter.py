"""The controlled asset adapter: resolve one registered asset, at most once.

``resolve`` fetches only the asset's own URI, stores the bytes in the ``storage.documents``
slot by content hash (RM-113 item 18; referenced by the record that carried the attachment, so
erasing the record cascades), runs the vision port once, and records every outcome on the
registry row. A failed download is final (``availability: unavailable`` with the reason); it
is not retried and nothing else is substituted. With ``allow_network=False`` only
already-stored bytes and already-computed evidence are used. Without a documents slot the
bytes are described right after the download and not kept.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import structlog

from memspine.exceptions import StorageError
from memspine.services.assets.fetch import AssetFetcher, AssetFetchError
from memspine.services.assets.models import AssetEntry
from memspine.services.assets.registry import AssetRegistry
from memspine.services.documents.base import DocumentStore
from memspine.services.vision import VisionBackend

__all__ = ["AssetAdapter", "AssetStats"]

_log = structlog.get_logger("memspine.assets")


@dataclass(slots=True)
class AssetStats:
    """Cost of the arm: network fetches and vision calls actually made."""

    fetches: int = 0
    fetch_failures: int = 0
    vision_calls: int = 0
    vision_failures: int = 0
    cache_hits: int = 0


class AssetAdapter:
    def __init__(
        self,
        registry: AssetRegistry,
        fetcher: AssetFetcher,
        vision: VisionBackend,
        documents: DocumentStore | None,
        *,
        max_bytes: int = 5_000_000,
        timeout: float = 15.0,
    ) -> None:
        self.registry = registry
        self.fetcher = fetcher
        self.vision = vision
        self.documents = documents
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.stats = AssetStats()
        self._lock = asyncio.Lock()

    async def _read_cached(self, entry: AssetEntry) -> bytes | None:
        if self.documents is None or not entry.content_hash:
            return None
        return await self.documents.get(entry.namespace, entry.content_hash)

    async def resolve(
        self, namespace: str, asset_id: str, *, allow_network: bool
    ) -> AssetEntry | None:
        """The registry row for ``asset_id`` after resolving it as far as allowed."""
        async with self._lock:
            entry = await self.registry.get(namespace, asset_id)
            if entry is None:
                return None
            if entry.evidence_status in ("ok", "skipped") or entry.availability == "unavailable":
                return entry  # resolved once; the outcome (or the failed download) is final
            # evidence_status "failed" (the vision call, with the bytes stored) is retried
            data = await self._read_cached(entry)
            if data is not None:
                self.stats.cache_hits += 1
            elif not allow_network:
                return entry  # cached-only mode and nothing stored: unresolved, not failed
            else:
                data = await self._download(entry)
                if data is None:
                    return entry
            await self._describe(entry, data)
            return entry

    async def download(self, namespace: str, asset_id: str) -> AssetEntry | None:
        """Pre-compute phase 1: fetch and store the bytes only (no vision). Idempotent: bytes
        already stored, or a download that already failed, are left as they are."""
        entry = await self.registry.get(namespace, asset_id)
        if entry is None:
            return None
        if entry.availability == "unavailable" or await self._read_cached(entry) is not None:
            return entry
        await self._download(entry)
        return entry

    async def describe(self, namespace: str, asset_id: str) -> AssetEntry | None:
        """Pre-compute phase 2: describe stored bytes (no network). Idempotent: a finished
        description (``ok``/``skipped``) is kept; a failed vision call is retried."""
        entry = await self.registry.get(namespace, asset_id)
        if entry is None or entry.evidence_status in ("ok", "skipped"):
            return entry
        data = await self._read_cached(entry)
        if data is None:
            return entry  # nothing downloaded (or the download failed): nothing to describe
        await self._describe(entry, data)
        return entry

    async def _download(self, entry: AssetEntry) -> bytes | None:
        self.stats.fetches += 1
        try:
            fetched = await self.fetcher.fetch(
                entry.uri, max_bytes=self.max_bytes, timeout_s=self.timeout
            )
        except AssetFetchError as exc:
            self.stats.fetch_failures += 1
            entry.availability = "unavailable"
            entry.error = exc.reason if not exc.detail else f"{exc.reason}: {exc.detail}"
            await self.registry.update(entry)
            _log.warning("asset.fetch_failed", asset_id=entry.asset_id, reason=exc.reason)
            return None
        digest = hashlib.sha256(fetched.data).hexdigest()
        entry.availability = "cached"
        entry.content_hash = digest
        entry.mime = fetched.mime
        entry.size = len(fetched.data)
        if self.documents is not None:  # content-addressed; the record reference cascades
            try:
                await self.documents.put(
                    entry.namespace,
                    fetched.data,
                    media_type=fetched.mime,
                    metadata={"kind": "asset"},
                    record_ids=(entry.record_id,) if entry.record_id else (),
                    expected_digest=digest,
                )
            except StorageError as exc:  # e.g. over the slot's max_bytes: described, not kept
                _log.warning("asset.store_failed", asset_id=entry.asset_id, error=str(exc))
        await self.registry.update(entry)
        return fetched.data

    async def _describe(self, entry: AssetEntry, data: bytes) -> None:
        reuse = (
            await self.registry.with_hash(entry.namespace, entry.content_hash)
            if entry.content_hash
            else None
        )
        if reuse is not None and reuse.asset_id != entry.asset_id:
            entry.evidence_status = "ok"
            entry.evidence = reuse.evidence
            entry.evidence_backend = reuse.evidence_backend
            await self.registry.update(entry)
            return
        entry.evidence_backend = self.vision.name
        if self.vision.name == "none":
            entry.evidence_status = "skipped"
            entry.error = "no_vision_backend"
            await self.registry.update(entry)
            return
        self.stats.vision_calls += 1
        try:
            result = await self.vision.describe(data, entry.mime or "image/jpeg")
        except Exception as exc:  # a vision outage is recorded, never raised into the read
            self.stats.vision_failures += 1
            entry.evidence_status = "failed"
            entry.error = f"vision_error: {exc}"[:300]
            await self.registry.update(entry)
            _log.warning("asset.vision_failed", asset_id=entry.asset_id, error=str(exc))
            return
        text = " ".join(result.text.split())
        if not text:
            entry.evidence_status = "failed"
            entry.error = "empty_description"
        else:
            entry.evidence_status = "ok"
            entry.evidence = text
        await self.registry.update(entry)
