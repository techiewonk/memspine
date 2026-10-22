"""Neo4j connector (RM-113): the native async driver; credentials never leave the connector.

The connector owns the driver (open, connectivity check, health, close). The graph adapter
builds its operation executor over :attr:`Neo4jClient.handle`
(:func:`memspine.services.graph.neo4j.connector_executor`) and never closes the driver."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

from memspine.clients._common.deadlines import probe
from memspine.clients._common.endpoints import is_loopback
from memspine.clients._common.secrets import auth_digest
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

__all__ = ["NativeNeo4jDriver", "Neo4jClient", "Neo4jSpec"]


class NativeNeo4jDriver(Protocol):
    """Small connector-side view of the optional native driver object."""

    async def verify_connectivity(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Neo4jSpec:
    """Transport spec: environment variable names (never values), database and deadlines.

    ``max_connections`` / ``keepalive_s`` (``runtime.shared_resources.http``): the driver's
    connection pool size and liveness-check idle time; None keeps the driver default."""

    connect_timeout_s: float
    request_timeout_s: float
    url_env: str = "MEMSPINE_NEO4J_URL"
    user_env: str = "MEMSPINE_NEO4J_USER"
    password_env: str = "MEMSPINE_NEO4J_PASSWORD"
    database: str = "neo4j"
    max_connections: int | None = None
    keepalive_s: float | None = None


def _url(spec: Neo4jSpec) -> str:
    raw_url = os.environ.get(spec.url_env)
    if not raw_url:
        raise ConfigError(f"Neo4j target needs environment variable {spec.url_env}")
    return raw_url


class Neo4jClient:
    """One native async driver per target; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "neo4j"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: Neo4jSpec) -> None:
        self._spec = spec
        self._driver: Any = None

    @classmethod
    def identity(cls, spec: Neo4jSpec) -> ConnectorIdentity:
        """Endpoint identity without credentials or the raw URL (both enter as digests)."""
        raw_url = _url(spec)
        try:
            parsed = urlsplit(raw_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ConfigError("Neo4j target URL is invalid") from None
        if not parsed.scheme or not hostname:
            raise ConfigError("Neo4j target URL is invalid")
        username = os.environ.get(spec.user_env) or ""
        password = os.environ.get(spec.password_env) or ""
        return ConnectorIdentity(
            cls.kind,
            {
                "scheme": parsed.scheme.lower(),
                "host": hostname.lower(),
                "port": port,
                "database": spec.database,
                "url_digest": sha256(raw_url.encode()).hexdigest(),
            },
            auth_digest(username, password, parsed.username or "", parsed.password or ""),
            {
                "connect_timeout_s": spec.connect_timeout_s,
                "request_timeout_s": spec.request_timeout_s,
                "max_connections": spec.max_connections,
                "keepalive_s": spec.keepalive_s,
            },
            # Physical target identity excludes credentials and transport settings: two roles
            # with different keys must not mirror into one target.
            {
                "kind": "neo4j",
                "host": hostname.lower(),
                "port": port or 7687,
                "database": spec.database,
            },
        )

    @classmethod
    def locality(cls, spec: Neo4jSpec) -> Locality:
        host = urlsplit(_url(spec)).hostname or ""
        return "host" if is_loopback(host) else "cluster"

    @property
    def spec(self) -> Neo4jSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        if self._driver is None:
            raise StorageError("Neo4j connector is not open — call open() first")
        return self._driver

    async def open(self) -> None:
        if self._driver is not None:
            return
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError:
            raise MissingServiceError("graph:neo4j", extra="neo4j") from None
        spec = self._spec
        url = _url(spec)
        user, password = os.environ.get(spec.user_env), os.environ.get(spec.password_env)
        pool: dict[str, Any] = {}
        if spec.max_connections is not None:
            pool["max_connection_pool_size"] = spec.max_connections
        if spec.keepalive_s is not None:
            pool["liveness_check_timeout"] = spec.keepalive_s
        driver: NativeNeo4jDriver = AsyncGraphDatabase.driver(
            url,
            auth=(user, password or "") if user else None,
            connection_timeout=spec.connect_timeout_s,
            **pool,
        )
        self._driver = driver
        try:
            async with asyncio.timeout(spec.connect_timeout_s):
                await driver.verify_connectivity()
        except BaseException:
            await self.close()
            raise

    async def health(self, deadline_s: float) -> bool:
        """``RETURN 1`` in a read session on the configured database."""
        driver = self._driver
        if driver is None:
            return False

        async def check() -> bool:
            async with driver.session(database=self._spec.database) as session:
                result = await session.run("RETURN 1 AS ok")
                record = await result.single()
                return record is not None and record["ok"] == 1

        return await probe(deadline_s, check)

    async def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            await driver.close()
