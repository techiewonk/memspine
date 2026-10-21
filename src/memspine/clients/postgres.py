"""PostgreSQL connection client (D-24), ``[postgres]``: async SQLAlchemy engine
over **psycopg3** (``postgresql+psycopg://``).

psycopg3 is chosen over asyncpg because it exposes both a sync driver (Alembic
has no async API) and an async driver from one package — a uniform driver for
migrations and runtime. Import is deferred to ``connect()`` so a core install
never imports it and a missing ``[postgres]`` extra fails with the D-10 error.
"""

from __future__ import annotations

import asyncio
import math

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from memspine.clients.base import Client
from memspine.config import constants
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["PostgresClient", "driver_connect_timeout", "normalize_pg_url"]


def normalize_pg_url(url: str) -> str:
    """Normalize any Postgres DSN to the psycopg3 async driver SQLAlchemy wants."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def driver_connect_timeout(connect_timeout_s: float) -> int:
    """The libpq ``connect_timeout`` psycopg actually applies, per attempt.

    Whole seconds, rounded up, and at least
    :data:`~memspine.config.constants.PG_DRIVER_MIN_CONNECT_TIMEOUT_S` (psycopg
    raises a smaller value to that floor itself; passing it explicitly keeps the
    effective value visible). The driver repeats it for every host and resolved
    address it tries (RM-40)."""
    return max(constants.PG_DRIVER_MIN_CONNECT_TIMEOUT_S, math.ceil(connect_timeout_s))


class PostgresClient(Client):
    def __init__(
        self,
        url: str,
        echo: bool = False,
        *,
        connect_timeout_s: float | None = None,
        statement_timeout_s: float | None = None,
    ) -> None:
        """``connect_timeout_s``: the overall deadline for :meth:`connect`, across
        every host/address attempt; it raises ``TimeoutError`` when it expires.
        Each driver attempt is separately bounded by
        :func:`driver_connect_timeout`, so a pooled reconnect after ``connect``
        can take up to that value per attempt (RM-40). ``None`` leaves both to
        the driver default."""
        self._url = normalize_pg_url(url)
        self._echo = echo
        self._connect_timeout_s = connect_timeout_s
        self._connect_args: dict[str, object] = {}
        if connect_timeout_s is not None:
            self._connect_args["connect_timeout"] = driver_connect_timeout(connect_timeout_s)
        if statement_timeout_s is not None:
            self._connect_args["options"] = (
                f"-c statement_timeout={max(1, int(statement_timeout_s * 1000))}"
            )
        self._engine: AsyncEngine | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageError("PostgresClient is not connected — call connect() first")
        return self._engine

    async def connect(self) -> None:
        if self._engine is not None:
            return
        try:
            import psycopg  # noqa: F401  # the psycopg3 driver SQLAlchemy binds
        except ImportError as exc:
            raise MissingServiceError("storage:postgres", extra="postgres") from exc
        engine = create_async_engine(self._url, echo=self._echo, connect_args=self._connect_args)
        # Verify reachability at start (D-10): a bad DSN / down server must fail
        # loudly here, not on the first write mid-request.
        try:
            async with asyncio.timeout(self._connect_timeout_s):
                async with engine.connect():
                    pass
        except BaseException:
            await engine.dispose()
            raise
        self._engine = engine

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def health(self) -> bool:
        return self._engine is not None
