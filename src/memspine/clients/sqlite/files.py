"""Plain SQLite file connections (RM-113): the only ``sqlite3.connect`` outside the storage
connector.

These open a file that is NOT the storage database, so nothing here is keyed:

* :func:`legacy_file_connection` reads a side file or sidecar left by an older version, once,
  for a one-time import (RM-111 WP5 side files, the RM-113 item 18 asset registry);
* :func:`file_connection` opens an application-owned local file that lives outside the
  storage database by design (the Resonate step journal).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["LEGACY_FILE_TIMEOUT_S", "file_connection", "legacy_file_connection"]

#: Busy timeout of a legacy-file read (another process may still hold the old file).
LEGACY_FILE_TIMEOUT_S = 10.0


@contextmanager
def legacy_file_connection(
    path: str | Path, *, timeout_s: float = LEGACY_FILE_TIMEOUT_S
) -> Iterator[sqlite3.Connection]:
    """One connection to an existing plaintext SQLite file, closed on exit. A missing file
    raises ``sqlite3.OperationalError`` instead of being created (the caller only reads; a
    hot journal or WAL left by a crash is still recovered, which a ``mode=ro`` open cannot)."""
    if not Path(path).is_file():
        raise sqlite3.OperationalError(f"no such SQLite file: {path}")
    connection = sqlite3.connect(str(path), timeout=timeout_s)
    try:
        yield connection
    finally:
        connection.close()


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
