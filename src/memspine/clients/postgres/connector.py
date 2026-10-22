"""PostgreSQL connector (D-24, RM-113), ``[postgres]``: async SQLAlchemy engine over
**psycopg3** (``postgresql+psycopg://``).

psycopg3 is chosen over asyncpg because it exposes both a sync driver (Alembic
has no async API) and an async driver from one package â€” a uniform driver for
migrations and runtime. Import is deferred to ``open()`` so a core install
never imports it and a missing ``[postgres]`` extra fails with the D-10 error.
Sync engines and the Alembic migration connection are lent by
:meth:`PostgresClient.sync_engine` / :meth:`PostgresClient.migration_connection`
(``clients/postgres/sync.py``).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from memspine.clients._common.deadlines import probe
from memspine.clients._common.endpoints import is_loopback
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import MissingServiceError, StorageError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "PG_DRIVER_MIN_CONNECT_TIMEOUT_S",
    "PostgresClient",
    "PostgresSpec",
    "driver_connect_timeout",
    "normalize_pg_url",
]

#: RM-40 (docs/PROVIDER_PERFORMANCE_FOLLOWUP.md, PA-054): the smallest per-attempt
#: ``connect_timeout`` psycopg/libpq honour, in whole seconds. psycopg's
#: ``conninfo.timeout_from_conninfo`` (after libpq ``connectDBComplete``) raises
#: any smaller positive value to 2 and applies it to *each* host/address attempt,
#: so ``localhost`` (::1 then 127.0.0.1) can wait twice as long. The async
#: client enforces ``connect_timeout_s`` as one overall deadline on top of this;
#: a sync (Alembic) connection is bounded only per attempt.
PG_DRIVER_MIN_CONNECT_TIMEOUT_S = 2


def normalize_pg_url(url: str) -> str:
    """Normalize any Postgres DSN to the psycopg3 driver SQLAlchemy wants."""
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def driver_connect_timeout(connect_timeout_s: float) -> int:
    """The libpq ``connect_timeout`` psycopg actually applies, per attempt.

    Whole seconds, rounded up, and at least :data:`PG_DRIVER_MIN_CONNECT_TIMEOUT_S` (psycopg
    raises a smaller value to that floor itself; passing it explicitly keeps the
    effective value visible). The driver repeats it for every host and resolved
    address it tries (RM-40)."""
    return max(PG_DRIVER_MIN_CONNECT_TIMEOUT_S, math.ceil(connect_timeout_s))


@dataclass(frozen=True, slots=True)
class PostgresSpec:
    """Transport spec of one Postgres database. ``url`` carries credentials: it is excluded
    from ``repr`` and enters the identity only as a digest.

    ``connect_timeout_s``: the overall deadline for :meth:`PostgresClient.open`, across every
    host/address attempt (``TimeoutError`` when it expires); each driver attempt is separately
    bounded by :func:`driver_connect_timeout`, so a pooled reconnect can take up to that value
    per attempt (RM-40). ``None`` leaves both to the driver default. ``pool_options``
    (``runtime.shared_resources.sql``): SQLAlchemy queue-pool keyword arguments."""

    url: str = field(repr=False)
    echo: bool = False
    connect_timeout_s: float | None = None
    statement_timeout_s: float | None = None
    pool_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", normalize_pg_url(str(self.url)))
        object.__setattr__(self, "pool_options", dict(self.pool_options))


def _connect_args(spec: PostgresSpec, *, statement_timeout: bool) -> dict[str, object]:
    args: dict[str, object] = {}
    if spec.connect_timeout_s is not None:
        args["connect_timeout"] = driver_connect_timeout(spec.connect_timeout_s)
    if statement_timeout and spec.statement_timeout_s is not None:
        args["options"] = f"-c statement_timeout={max(1, int(spec.statement_timeout_s * 1000))}"
    return args


class PostgresClient:
    """One async engine (pool) per database; a :class:`~memspine.clients.base.Connector`."""

    kind: ClassVar[str] = "sql.postgres"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: PostgresSpec) -> None:
        self._spec = spec
        self._engine: AsyncEngine | None = None

    # -- connector protocol ----------------------------------------------------------------
    @classmethod
    def identity(cls, spec: PostgresSpec) -> ConnectorIdentity:
        """``target`` = the SHA-256 of the canonical full URL (credentials included, never
        returned), the same value the storage descriptor fingerprints."""
        url = make_url(spec.url)
        canonical_url = url.set(
            drivername="postgresql",
            port=url.port or 5432,
            query=dict(sorted(url.query.items())),
        ).render_as_string(hide_password=False)
        return ConnectorIdentity(
            cls.kind,
            {
                "driver": "postgresql",
                "url_sha256": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
            },
            None,
            {
                "connect_timeout_s": spec.connect_timeout_s,
                "statement_timeout_s": spec.statement_timeout_s,
                "pool": dict(spec.pool_options),
            },
            {
                "kind": "postgres",
                "host": (url.host or "").lower(),
                "port": url.port or 5432,
                "database": url.database,
            },
        )

    @classmethod
    def locality(cls, spec: PostgresSpec) -> Locality:
        host = make_url(spec.url).host
        return "host" if not host or host.startswith("/") or is_loopback(host) else "cluster"

    @property
    def spec(self) -> PostgresSpec:
        return self._spec

    @property
    def handle(self) -> AsyncEngine:
        return self.engine

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageError("PostgresClient is not open â€” call open() first")
        return self._engine

    async def open(self) -> None:
        if self._engine is not None:
            return
        try:
            import psycopg  # noqa: F401  # the psycopg3 driver SQLAlchemy binds
        except ImportError as exc:
            raise MissingServiceError("storage:postgres", extra="postgres") from exc
        import asyncio

        spec = self._spec
        engine = create_async_engine(
            spec.url,
            echo=spec.echo,
            connect_args=_connect_args(spec, statement_timeout=True),
            **dict(spec.pool_options),
        )
        # Verify reachability at start (D-10): a bad DSN / down server must fail
        # loudly here, not on the first write mid-request.
        try:
            async with asyncio.timeout(spec.connect_timeout_s):
                async with engine.connect():
                    pass
        except BaseException:
            await engine.dispose()
            raise
        self._engine = engine

    async def health(self, deadline_s: float) -> bool:
        """``SELECT 1`` over a pooled connection."""
        engine = self._engine
        if engine is None:
            return False

        async def check() -> bool:
            async with engine.connect() as conn:
                return bool((await conn.scalar(text("SELECT 1"))) == 1)

        return await probe(deadline_s, check)

    async def close(self) -> None:
        if self._engine is not None:
            engine, self._engine = self._engine, None
            await engine.dispose()

    # -- lent sync access (clients/postgres/sync.py) -----------------------------------------
    def sync_engine(
        self,
        pool_options: Mapping[str, Any] | None = None,
        *,
        statement_timeout: bool = True,
    ) -> Engine:
        """A new sync engine on this database (per-attempt connect timeout; the statement
        timeout unless ``statement_timeout=False``). The caller owns and disposes it."""
        from memspine.clients.postgres.sync import postgres_sync_engine

        return postgres_sync_engine(
            self._spec.url,
            _connect_args(self._spec, statement_timeout=statement_timeout),
            pool_options,
        )

    @contextmanager
    def migration_connection(self) -> Iterator[Connection]:
        """One unpooled sync connection for an Alembic run (no statement timeout: a migration
        may legitimately run long); closed on exit."""
        from memspine.clients.postgres.sync import migration_connection

        with migration_connection(
            self._spec.url, _connect_args(self._spec, statement_timeout=False)
        ) as connection:
            yield connection
