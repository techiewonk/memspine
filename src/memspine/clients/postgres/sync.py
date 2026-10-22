"""Sync SQLAlchemy engines, the Alembic migration connection and the migration advisory lock
for Postgres (RM-113 P1 #8/#9, ADR-081)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from memspine.clients.postgres.connector import normalize_pg_url

__all__ = [
    "MIGRATION_LOCK_KEY",
    "advisory_lock",
    "migration_connection",
    "postgres_sync_engine",
]

#: ADR-081: the session advisory lock every memspine migration run takes, so two processes
#: starting on one database never run Alembic at once (a signed 64-bit key).
MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"memspine.storage.migrations").digest()[:8], "big", signed=True
)


def postgres_sync_engine(
    url: str,
    connect_args: Mapping[str, object] | None = None,
    pool_options: Mapping[str, Any] | None = None,
) -> Engine:
    """A pooled sync engine on ``url`` (normalized to psycopg3)."""
    return create_engine(
        normalize_pg_url(url),
        connect_args=dict(connect_args or {}),
        **dict(pool_options or {}),
    )


@contextmanager
def migration_connection(
    url: str, connect_args: Mapping[str, object] | None = None
) -> Iterator[Connection]:
    """One unpooled sync connection on ``url``, closed and disposed on exit."""
    engine = create_engine(
        normalize_pg_url(url), connect_args=dict(connect_args or {}), poolclass=NullPool
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


@contextmanager
def advisory_lock(connection: Connection, key: int = MIGRATION_LOCK_KEY) -> Iterator[None]:
    """Hold the session advisory lock ``key`` on ``connection`` for the block (Postgres only;
    a no-op on other dialects). Session-level, so Alembic may commit inside the block."""
    if connection.dialect.name != "postgresql":
        yield
        return
    connection.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
    connection.commit()
    try:
        yield
    finally:
        if connection.in_transaction():
            connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        connection.commit()
