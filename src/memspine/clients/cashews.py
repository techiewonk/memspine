"""cashews cache connector (D-24, RM-113), ``[cache]``.

Owns one set-up ``cashews.Cache`` for a single backend URL (``mem://`` /
``disk://`` / ``redis://``); the cache service receives this connector injected and
never sets cashews up itself. Import of cashews is deferred to ``open()`` so a
core install never imports it and a missing ``[cache]`` extra fails with the
D-10 error. Replaces the LMDB + hand-rolled Redis clients (ADR-022 amendment).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

from memspine.clients._common.deadlines import probe
from memspine.clients._common.endpoints import is_loopback
from memspine.clients._common.secrets import auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

__all__ = ["CashewsClient", "CashewsSpec"]

_PING_KEY = "__memspine_ping__"


@dataclass(frozen=True, slots=True)
class CashewsSpec:
    """A cashews backend URL. It may carry a Redis password: excluded from ``repr`` and
    entering the identity only as a digest."""

    url: str = field(repr=False)

    def __post_init__(self) -> None:
        if not urlsplit(self.url).scheme:
            raise ConfigError("cache backend URL needs a scheme (mem://, disk://, redis://)")


class CashewsClient:
    """One set-up cashews cache per backend URL; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "cache.cashews"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: CashewsSpec) -> None:
        self._spec = spec
        self._cache: Any = None

    @classmethod
    def identity(cls, spec: CashewsSpec) -> ConnectorIdentity:
        """Remote backends by scheme, host, port and database (password as a digest); a
        ``mem://`` cache is private (never equal); ``disk://`` by its directory."""
        parsed = urlsplit(spec.url)
        scheme = parsed.scheme.lower()
        if scheme == "mem":
            return ConnectorIdentity(cls.kind, {"scheme": scheme, "private": uuid.uuid4().hex})
        if scheme == "disk":
            directory = dict(parse_qsl(parsed.query)).get("directory", "")
            return ConnectorIdentity(cls.kind, {"scheme": scheme, "directory": directory})
        host = (parsed.hostname or "").lower()
        target = {"scheme": scheme, "host": host, "port": parsed.port, "db": parsed.path}
        return ConnectorIdentity(
            cls.kind,
            target,
            auth_digest(parsed.username or "", parsed.password or ""),
            {"query": sorted(parse_qsl(parsed.query))},
            {"kind": "cache", "host": host, "port": parsed.port, "db": parsed.path},
        )

    @classmethod
    def locality(cls, spec: CashewsSpec) -> Locality:
        parsed = urlsplit(spec.url)
        scheme = parsed.scheme.lower()
        if scheme == "mem":
            return "process"
        if scheme == "disk" or is_loopback(parsed.hostname or ""):
            return "host"
        return "cluster"

    @property
    def spec(self) -> CashewsSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        return self.cache

    @property
    def cache(self) -> Any:
        if self._cache is None:
            raise StorageError("CashewsClient is not open — call open() first")
        return self._cache

    async def open(self) -> None:
        if self._cache is not None:
            return
        try:
            from cashews import Cache
        except ImportError as exc:
            raise MissingServiceError("cache:cashews", extra="cache") from exc
        cache = Cache()
        cache.setup(self._spec.url)
        # Verify reachability at start (D-10): for redis this forces a connection
        # so a bad DSN / down server fails loudly here, not mid-request. For
        # mem/disk it is a cheap no-op miss.
        self._cache = cache
        try:
            await cache.get(_PING_KEY)
        except BaseException:
            await self.close()
            raise

    async def health(self, deadline_s: float) -> bool:
        """One ``get`` of a reserved key (a miss) against the backend."""
        cache = self._cache
        if cache is None:
            return False

        async def check() -> bool:
            await cache.get(_PING_KEY)
            return True

        return await probe(deadline_s, check)

    async def close(self) -> None:
        if self._cache is not None:
            cache, self._cache = self._cache, None
            await cache.close()
