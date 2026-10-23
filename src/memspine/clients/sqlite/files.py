"""Plain SQLite file connections (RM-113): the only ``sqlite3.connect`` outside the storage
connector.

These open a file that is NOT the storage database: a side file or sidecar left by an older
version, read once by a one-time import (RM-111 WP5 side files, the RM-113 item 18 asset
registry). Nothing here is keyed: such files were always plaintext.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["LEGACY_FILE_TIMEOUT_S", "legacy_file_connection"]

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
