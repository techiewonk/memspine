"""Redis / Valkey connection client (D-24), ``[redis]`` / ``[valkey]``.

Owns one async Redis-protocol connection pool; the cache service receives the
raw client injected and never opens a connection itself. ``valkey`` is a
drop-in fork of ``redis`` with the same async surface (``from_url`` + the
``get``/``set``/``delete`` verbs), so either package satisfies either backend —
the ``backend`` name only decides which extra the D-10 error names when neither
is installed. Import is deferred to ``connect()`` (D-03/D-10).
"""

from __future__ import annotations

from typing import Any

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError

__all__ = ["RedisClient"]


def _load_asyncio_client(url: str, backend: str) -> Any:
    """Return an async Redis-protocol client from whichever lib is installed.

    Prefers the package matching ``backend`` (redis→redis, valkey→valkey) and
    falls back to the other (they are wire-compatible). Raises the D-10 error
    naming ``backend``'s own extra when neither package is importable."""
    order = ("valkey", "redis") if backend == "valkey" else ("redis", "valkey")
    for name in order:
        try:
            mod = __import__(f"{name}.asyncio", fromlist=["asyncio"])
        except ImportError:
            continue
        # redis exposes ``Redis``; valkey exposes ``Valkey`` (with a ``Redis``
        # alias on newer builds) — accept whichever the module provides.
        cls = getattr(mod, "Redis", None) or getattr(mod, "Valkey", None)
        if cls is not None:
            return cls.from_url(url)
    raise MissingServiceError(f"cache:{backend}", extra=backend)


class RedisClient(Client):
    def __init__(self, url: str, *, backend: str = "redis") -> None:
        self._url = url
        self._backend = backend
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise MissingServiceError(f"cache:{self._backend}", extra=self._backend)
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = _load_asyncio_client(self._url, self._backend)
        # from_url is lazy; a ping proves the endpoint is actually reachable so a
        # misconfigured cache fails loudly at start() (D-10) rather than on the
        # first embedding write mid-request.
        await client.ping()
        self._client = client

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.aclose()

    async def health(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.ping()
        except Exception:
            return False
        return True
