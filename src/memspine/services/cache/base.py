"""KV cache port with TTL (D-09). LMDB/Valkey adapters land in later phases;
``MemoryKV`` is the in-process zero-dep default every profile can boot with."""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from memspine.config.constants import MEMORY_KV_MAX_ENTRIES

__all__ = ["KVCache", "MemoryKV"]


@runtime_checkable
class KVCache(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...


class MemoryKV:
    """In-process dict KV with lazy TTL expiry — the core-install default."""

    def __init__(self, max_entries: int = MEMORY_KV_MAX_ENTRIES) -> None:
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self._max_entries = max_entries

    async def get(self, key: str) -> bytes | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._data[key]
            return None
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        if len(self._data) >= self._max_entries and key not in self._data:
            # Drop the oldest-inserted entry (dicts preserve insertion order).
            self._data.pop(next(iter(self._data)))
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds is not None else None
        self._data[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
