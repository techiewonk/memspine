"""Redis / Valkey connector for the taskiq streams broker (RM-113 item 6): the one
``Redis.from_url`` outside the cashews cache connector.

The taskiq runner (``workers/taskiq_runner.py``) receives an opened connector and uses its
:attr:`RedisConnector.handle` (a ``redis.asyncio.Redis``). ``redis`` is imported only in
:meth:`RedisConnector.open` (the ``[taskiq]`` extra). The client connects lazily on its first
command, as before; :meth:`RedisConnector.health` is a ``PING`` round trip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

from memspine.clients._common.secrets import auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import StorageError

__all__ = ["RedisConnector", "RedisSpec"]


@dataclass(frozen=True, slots=True)
class RedisSpec:
    """A ``redis://`` / ``rediss://`` / ``valkey://`` URL; it may carry a password, so it is
    excluded from ``repr`` and enters the identity only as a digest."""

    url: str = field(repr=False)


class RedisConnector:
    """One ``redis.asyncio.Redis`` client per URL."""

    kind: ClassVar[str] = "redis"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: RedisSpec) -> None:
        self._spec = spec
        self._client: Any | None = None

    @classmethod
    def identity(cls, spec: RedisSpec) -> ConnectorIdentity:
        parsed = urlsplit(spec.url)
        target = {
            "scheme": parsed.scheme.lower(),
            "host": (parsed.hostname or "").lower(),
            "port": parsed.port,
            "db": parsed.path.lstrip("/") or "0",
        }
        secret = f"{parsed.username or ''}:{parsed.password or ''}"
        return ConnectorIdentity(cls.kind, target, auth_digest(secret) if secret != ":" else None)

    @classmethod
    def locality(cls, spec: RedisSpec) -> Locality:
        return "cluster"

    @property
    def spec(self) -> RedisSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._client is None:
            raise StorageError("Redis connector is not open")
        return self._client

    async def open(self) -> None:
        if self._client is not None:
            return
        from redis.asyncio import Redis

        self._client = Redis.from_url(self._spec.url)

    async def health(self, deadline_s: float) -> bool:
        if self._client is None:
            return False
        try:
            async with asyncio.timeout(deadline_s):
                return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        client, self._client = self._client, None
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            await aclose()
