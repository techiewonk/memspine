"""Sync SQLAlchemy engines and the Alembic migration connection for SQLite (RM-113 P1 #8/#9).

The storage layer is async; the outbox, the lease store and Alembic are synchronous. Every
sync engine memspine opens on a SQLite database is built here, so URL, busy-timeout and
SQLCipher keying live in one place."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool, StaticPool

__all__ = ["memory_sync_engine", "migration_connection", "sqlite_sync_engine"]


def sqlite_sync_engine(
    path: str,
    creator: Callable[[], Any] | None = None,
    *,
    busy_timeout_s: float,
    pool_options: Mapping[str, Any] | None = None,
) -> Engine:
    """A sync engine over a file-backed SQLite database (the one the storage layer uses).

    ``creator`` opens a keyed connection for an encrypted database (#52; no pool, so
    ``pool_options`` - SQLAlchemy queue-pool keyword arguments - apply only without it)."""
    if isinstance(busy_timeout_s, bool) or not math.isfinite(busy_timeout_s) or busy_timeout_s <= 0:
        raise ValueError("busy_timeout_s must be finite and positive")
    if creator is not None:
        return create_engine("sqlite://", creator=creator, poolclass=NullPool)
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": busy_timeout_s, "check_same_thread": False},
        **dict(pool_options or {}),
    )


def memory_sync_engine() -> Engine:
    """A private in-memory engine (one shared connection). Used for ``:memory:`` storage, whose
    database a second connection cannot reach: the queue then lives as long as the process."""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@contextmanager
def migration_connection(
    path: str, creator: Callable[[], Any] | None = None
) -> Iterator[Connection]:
    """One unpooled sync connection to ``path`` (keyed by ``creator`` under SQLCipher), closed
    and disposed on exit. Alembic runs over it (``config.attributes['connection']``)."""
    engine = (
        create_engine("sqlite://", creator=creator, poolclass=NullPool)
        if creator is not None
        else create_engine(f"sqlite:///{path}", poolclass=NullPool)
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()
