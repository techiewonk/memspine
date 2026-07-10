"""PostgreSQL connection client (D-24), ``[postgres]``: async SQLAlchemy engine
over **psycopg3** (``postgresql+psycopg://``).

psycopg3 is chosen over asyncpg because it exposes both a sync driver (Alembic
has no async API) and an async driver from one package — a uniform driver for
migrations and runtime. Import is deferred to ``connect()`` so a core install
never imports it and a missing ``[postgres]`` extra fails with the D-10 error.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["PostgresClient", "normalize_pg_url"]


def normalize_pg_url(url: str) -> str:
    """Normalize any Postgres DSN to the psycopg3 async driver SQLAlchemy wants."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


class PostgresClient(Client):
    def __init__(self, url: str, echo: bool = False) -> None:
        self._url = normalize_pg_url(url)
        self._echo = echo
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
        engine = create_async_engine(self._url, echo=self._echo)
        # Verify reachability at start (D-10): a bad DSN / down server must fail
        # loudly here, not on the first write mid-request.
        try:
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
