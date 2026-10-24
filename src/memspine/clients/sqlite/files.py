"""Application-owned SQLite file connections for local journals."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["file_connection"]


@contextmanager
def file_connection(
    path: str | Path, *, timeout_s: float, autocommit: bool = False
) -> Iterator[sqlite3.Connection]:
    """One connection to a local SQLite file (created when missing), closed on exit. The
    busy timeout is also set as ``PRAGMA busy_timeout``; ``autocommit`` opens it with
    ``isolation_level=None`` (the caller issues ``BEGIN`` itself)."""
    connection = (
        sqlite3.connect(str(path), timeout=timeout_s, isolation_level=None)
        if autocommit
        else sqlite3.connect(str(path), timeout=timeout_s)
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
        yield connection
    finally:
        connection.close()
