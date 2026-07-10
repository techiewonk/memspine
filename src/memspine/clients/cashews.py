"""cashews cache connection client (D-24), ``[cache]``.

Owns one set-up ``cashews.Cache`` for a single backend URL (``mem://`` /
``disk://`` / ``redis://``); the cache service receives this client injected and
never sets cashews up itself. Import of cashews is deferred to ``connect()`` so a
core install never imports it and a missing ``[cache]`` extra fails with the
D-10 error. Replaces the LMDB + hand-rolled Redis clients (ADR-022 amendment).
"""

from __future__ import annotations

from typing import Any

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["CashewsClient"]


class CashewsClient(Client):
    def __init__(self, url: str) -> None:
        self._url = url
        self._cache: Any = None

    @property
    def cache(self) -> Any:
        if self._cache is None:
            raise StorageError("CashewsClient is not connected — call connect() first")
        return self._cache

    async def connect(self) -> None:
        if self._cache is not None:
            return
        try:
            from cashews import Cache
        except ImportError as exc:
            raise MissingServiceError("cache:cashews", extra="cache") from exc
        cache = Cache()
        cache.setup(self._url)
        # Verify reachability at start (D-10): for redis this forces a connection
        # so a bad DSN / down server fails loudly here, not mid-request. For
        # mem/disk it is a cheap no-op miss.
        await cache.get("__memspine_ping__")
        self._cache = cache

    async def close(self) -> None:
        if self._cache is not None:
            cache, self._cache = self._cache, None
            await cache.close()

    async def health(self) -> bool:
        return self._cache is not None
