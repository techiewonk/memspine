"""cashews KVCache adapter (D-09, ``[cache]``): the unified on-disk + redis/valkey
cache (ADR-022 amendment — replaces the LMDB and hand-rolled Redis adapters).

Wraps a set-up ``cashews.Cache`` (owned by :class:`CashewsClient`): keys are
namespace-prefixed so many memspine instances can share one backend, TTL rides
cashews' native ``expire`` (no lazy-expiry bookkeeping), and byte values
round-trip verbatim. ``MemoryKV`` stays the zero-dep core ``memory`` default
(slim-core, D-03); cashews only backs the ``disk``/``redis``/``valkey`` backends.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from memspine.clients.cashews import CashewsClient
from memspine.observability.logging import get_logger

__all__ = ["CashewsCache"]

_log = get_logger(__name__)


class CashewsCache:
    def __init__(
        self,
        client: CashewsClient,
        *,
        namespace: str = "memspine",
        default_ttl_seconds: float | None = None,
        request_timeout_s: float | None = None,
    ) -> None:
        self._client = client
        self._prefix = f"{namespace}:"
        self._default_ttl = default_ttl_seconds
        self._request_timeout_s = request_timeout_s
        #: ADR-095: a delete since the last :meth:`compact` (its key may sit in free pages).
        self._deleted = False

    def _key(self, key: str) -> str:
        return self._prefix + key

    async def get(self, key: str) -> bytes | None:
        async with asyncio.timeout(self._request_timeout_s):
            value: bytes | None = await self._client.cache.get(self._key(key))
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        # cashews ``expire=None`` means no expiry; a number is seconds.
        async with asyncio.timeout(self._request_timeout_s):
            await self._client.cache.set(self._key(key), value, expire=ttl)

    async def delete(self, key: str) -> None:
        async with asyncio.timeout(self._request_timeout_s):
            await self._client.cache.delete(self._key(key))
        self._deleted = True

    async def delete_prefix(self, prefix: str) -> int:
        """ADR-095: drop every key of this cache's namespace starting with ``prefix``. A disk
        backend is sharded, and cashews' pattern scan skips sharded disk caches, so its shards
        are walked directly; other backends use cashews' ``delete_match``. Returns how many
        keys went (-1 when the backend does not report it)."""
        full = self._key(prefix)
        shards = [
            shard
            for backend in getattr(self._client.cache, "_backends", {}).values()
            for shard in _disk_shards(backend)
        ]
        self._deleted = True
        if shards:
            return await asyncio.to_thread(_delete_prefix, shards, full)
        async with asyncio.timeout(self._request_timeout_s):
            await self._client.cache.delete_match(full + "*")
        return -1

    async def compact(self) -> bool:
        """ADR-095 (caches copy class): a disk backend (diskcache SQLite) deletes a key only
        logically; the key, which carries the erased text's hash, stays in the database's free
        pages and its WAL. Rewrite every shard (``VACUUM``) and truncate its WAL, so no page of
        the files holds a deleted key. Called after a hard erase. A remote backend (redis or
        valkey) frees a deleted key itself: nothing to do. Returns True when every disk shard
        was compacted (or there is none, or nothing was deleted since the last compaction)."""
        if not self._deleted:
            return True
        shards = [
            shard
            for backend in getattr(self._client.cache, "_backends", {}).values()
            for shard in _disk_shards(backend)
        ]
        if not shards:
            return True
        # No request timeout: a rewrite takes as long as the cache is large, and a cancelled
        # await would leave the worker thread running anyway.
        ok = await asyncio.to_thread(_vacuum_all, shards)
        self._deleted = not ok
        return ok


def _disk_shards(backend: object) -> list[Any]:
    """The diskcache ``Cache`` shards behind a cashews disk backend (none for other kinds)."""
    inner = getattr(backend, "_cache", None)
    if inner is None or not hasattr(inner, "directory"):
        return []
    shards = getattr(inner, "_shards", None)
    return list(shards) if shards is not None else [inner]


def _delete_prefix(shards: list[Any], prefix: str) -> int:
    count = 0
    for shard in shards:
        for key in [k for k in shard.iterkeys() if isinstance(k, str) and k.startswith(prefix)]:
            if shard.delete(key):
                count += 1
    return count


def _vacuum_all(shards: list[Any]) -> bool:
    ok = True
    for shard in shards:
        try:
            sql = shard._sql  # the shard's own (thread-local) connection
            sql("PRAGMA wal_checkpoint(TRUNCATE)")
            sql("VACUUM")
            sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # busy: the key stays in free pages until the next erase
            _log.warning("cache.compact_failed", error=str(exc)[:200])
            ok = False
        finally:
            # The connection belongs to this worker thread: close it so no idle handle keeps
            # the WAL from truncating later.
            local: Any = getattr(shard, "_local", None)
            con = getattr(local, "con", None)
            if con is not None:
                con.close()
                local.con = None
    return ok
