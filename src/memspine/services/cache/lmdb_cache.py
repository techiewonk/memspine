"""LMDB KVCache adapter (D-09), ``[lmdb]``: persistent, single-process cache.

LMDB has no native key expiry, so — like :class:`MemoryKV` — each value is
stored as ``<f64 expires_at><value>`` and expiry is checked lazily on read (an
``expires_at`` of ``0.0`` means "never"). Expiry uses **wall-clock** time
(``time.time``), not ``monotonic``: an LMDB env persists across process
restarts, so a monotonic clock — which resets each run — would make persisted
TTLs meaningless. Keys are prefixed with the configured namespace so several
memspine instances can share one env without colliding (mirrors the
``emb:``/``ext:`` producer prefixes the cached services already use).
"""

from __future__ import annotations

import asyncio
import struct
import time

from memspine.clients.lmdb import LmdbClient

__all__ = ["LmdbCache"]

_HEADER = struct.Struct("<d")  # 8-byte little-endian f64 absolute expiry


class LmdbCache:
    def __init__(
        self,
        client: LmdbClient,
        *,
        namespace: str = "memspine",
        default_ttl_seconds: float | None = None,
    ) -> None:
        self._client = client
        self._prefix = f"{namespace}:".encode()
        self._default_ttl = default_ttl_seconds

    def _key(self, key: str) -> bytes:
        return self._prefix + key.encode()

    async def get(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, self._key(key))

    def _get_sync(self, raw_key: bytes) -> bytes | None:
        env = self._client.env
        with env.begin() as txn:
            raw = txn.get(raw_key)
        if raw is None:
            return None
        (expires_at,) = _HEADER.unpack_from(raw)
        if expires_at and time.time() >= expires_at:
            # Lazy eviction: reclaim the space so an unread expired key does not
            # linger forever. A separate write txn (the read one is closed).
            with env.begin(write=True) as txn:
                txn.delete(raw_key)
            return None
        return bytes(raw[_HEADER.size :])

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires_at = time.time() + ttl if ttl is not None else 0.0
        payload = _HEADER.pack(expires_at) + value
        await asyncio.to_thread(self._set_sync, self._key(key), payload)

    def _set_sync(self, raw_key: bytes, payload: bytes) -> None:
        with self._client.env.begin(write=True) as txn:
            txn.put(raw_key, payload)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, self._key(key))

    def _delete_sync(self, raw_key: bytes) -> None:
        with self._client.env.begin(write=True) as txn:
            txn.delete(raw_key)
