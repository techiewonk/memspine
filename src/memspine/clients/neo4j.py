"""Owned Neo4j transport; credentials and native driver stay inside the client."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from memspine.clients.base import Client
from memspine.exceptions import ConfigError, MissingServiceError, StorageError
from memspine.providers.settings import Neo4jSettings


class Neo4jOperationExecutor(Protocol):
    """Service-owned operation adapter over a connected native driver."""

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...


class NativeNeo4jDriver(Protocol):
    """Small client-side view of the optional native driver object."""

    async def verify_connectivity(self) -> None: ...

    async def close(self) -> None: ...


ExecutorFactory = Callable[[NativeNeo4jDriver], Neo4jOperationExecutor]


class _BorrowedExecutor:
    def __init__(self, owner: Neo4jClient) -> None:
        self._owner = owner

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        executor = self._owner._executor
        if executor is None:
            raise StorageError("Neo4j transport is not connected")
        async with asyncio.timeout(self._owner._settings.request_timeout_s):
            return await executor.run(op, params)

    async def close(self) -> None:
        # A graph adapter borrows this handle; only the lifecycle owner closes it.
        return None


class Neo4jClient(Client):
    def __init__(
        self,
        settings: Neo4jSettings,
        *,
        executor_factory: ExecutorFactory,
        max_connections: int | None = None,
        keepalive_s: float | None = None,
    ) -> None:
        """``max_connections`` / ``keepalive_s`` (``runtime.shared_resources.http``): the
        driver's connection pool size and liveness-check idle time; None keeps the driver
        default."""
        self._settings = settings
        self._pool: dict[str, Any] = {}
        if max_connections is not None:
            self._pool["max_connection_pool_size"] = max_connections
        if keepalive_s is not None:
            self._pool["liveness_check_timeout"] = keepalive_s
        self._executor_factory = executor_factory
        self._driver: NativeNeo4jDriver | None = None
        self._executor: Neo4jOperationExecutor | None = None
        self.executor = _BorrowedExecutor(self)

    async def connect(self) -> None:
        if self._driver is not None:
            return
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError:
            raise MissingServiceError("graph:neo4j", extra="neo4j") from None
        settings = self._settings
        url = os.environ.get(settings.url_env)
        if not url:
            raise ConfigError(f"Neo4j target needs environment variable {settings.url_env}")
        user, password = os.environ.get(settings.user_env), os.environ.get(settings.password_env)
        driver: NativeNeo4jDriver = AsyncGraphDatabase.driver(
            url,
            auth=(user, password or "") if user else None,
            connection_timeout=settings.connect_timeout_s,
            **self._pool,
        )
        self._driver = driver
        try:
            async with asyncio.timeout(settings.connect_timeout_s):
                await driver.verify_connectivity()
            self._executor = self._executor_factory(driver)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        driver, self._driver = self._driver, None
        self._executor = None
        if driver is not None:
            await driver.close()

    async def health(self) -> bool:
        if self._driver is None:
            return False
        try:
            async with asyncio.timeout(self._settings.connect_timeout_s):
                await self._driver.verify_connectivity()
        except Exception:
            return False
        return True
