"""Application-owned SQLite file connections for local journals."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["file_connection", "open_file_connection"]


def open_file_connection(
    path: str | Path,
    *,
    timeout_s: float = 5.0,
    autocommit: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open an application-owned SQLite file connection.

    Long-lived clients use this factory directly and own the returned connection. Short-lived
    callers should prefer :func:`file_connection`.
    """
    connection = (
        sqlite3.connect(
            str(path),
            timeout=timeout_s,
            isolation_level=None,
            check_same_thread=check_same_thread,
        )
        if autocommit
        else sqlite3.connect(str(path), timeout=timeout_s, check_same_thread=check_same_thread)
    )
    connection.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
    return connection


@contextmanager
def file_connection(
    path: str | Path, *, timeout_s: float, autocommit: bool = False
) -> Iterator[sqlite3.Connection]:
    """One connection to a local SQLite file (created when missing), closed on exit. The
    busy timeout is also set as ``PRAGMA busy_timeout``; ``autocommit`` opens it with
    ``isolation_level=None`` (the caller issues ``BEGIN`` itself)."""
    connection = open_file_connection(path, timeout_s=timeout_s, autocommit=autocommit)
    try:
        yield connection
    finally:
        connection.close()
