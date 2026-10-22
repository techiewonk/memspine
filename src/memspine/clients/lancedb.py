"""LanceDB connector (D-24, core dep — ADR-021, RM-113).

Owns the ``lancedb.connect`` handle; the vector service receives this connector
injected and never connects itself.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from memspine.clients._common.deadlines import probe
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["LANCE_MEMORY_METADATA_CACHE_BYTES", "LanceDBClient", "LanceDBSpec"]

#: Metadata cache of a ``memory://`` Lance connection (ADR-053). A cached entry can keep a
#: whole in-memory object alive (each holds a 5 MB upload buffer) while the cache counts only
#: the entry's own bytes, so the default cache pins gigabytes of dropped table versions; at
#: this size it pins a handful.
LANCE_MEMORY_METADATA_CACHE_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class LanceDBSpec:
    """A database directory, or ``memory://<name>`` for a private in-memory database."""

    path: str | os.PathLike[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", str(self.path))

    @property
    def is_memory(self) -> bool:
        return str(self.path).startswith("memory://")


class LanceDBClient:
    """One connection per database path per process (RM-77: shared by every Engine on the path).

    LanceDB table handles stay on the version they last read or wrote, so a handle opened by
    one store does not see rows another store committed through its own handle. Each store
    reports its commits here (:meth:`note_write`); a store whose handle is behind the table's
    write epoch checks out the latest version before it reads (one integer compare otherwise).
    """

    kind: ClassVar[str] = "vector.lancedb"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: LanceDBSpec) -> None:
        self._spec = spec
        self._db: Any = None
        self._epochs: dict[str, int] = {}
        self._epoch_lock = threading.Lock()

    @classmethod
    def identity(cls, spec: LanceDBSpec) -> ConnectorIdentity:
        """A directory by canonical path; an in-memory database is private (never equal)."""
        if spec.is_memory:
            return ConnectorIdentity(
                cls.kind, {"path": str(spec.path), "private": uuid.uuid4().hex}
            )
        path = os.path.normcase(str(Path(spec.path).expanduser().resolve(strict=False)))
        return ConnectorIdentity(
            cls.kind, {"path": path}, canonical={"kind": "lancedb", "path": path}
        )

    @classmethod
    def locality(cls, spec: LanceDBSpec) -> Locality:
        return "process" if spec.is_memory else "host"

    @property
    def spec(self) -> LanceDBSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        return self.db

    def write_epoch(self, table: str) -> int:
        """How many commits the stores of this client made to ``table``."""
        return self._epochs.get(table, 0)

    def note_write(self, table: str) -> int:
        """Record one commit to ``table``; returns the new epoch."""
        with self._epoch_lock:
            epoch = self._epochs[table] = self._epochs.get(table, 0) + 1
            return epoch

    @property
    def db(self) -> Any:
        if self._db is None:
            raise StorageError("LanceDBClient is not open — call open() first")
        return self._db

    async def open(self) -> None:
        if self._db is not None:
            return
        try:
            import lancedb
        except ImportError as exc:
            raise MissingServiceError("vector:lancedb") from exc
        path = str(self._spec.path)
        if self._spec.is_memory:
            # ADR-053: a small metadata cache, so dropped table versions are
            # released instead of pinned by cache entries.
            session = lancedb.Session(metadata_cache_size_bytes=LANCE_MEMORY_METADATA_CACHE_BYTES)
            self._db = await asyncio.to_thread(lancedb.connect, path, session=session)
        else:
            self._db = await asyncio.to_thread(lancedb.connect, path)

    async def health(self, deadline_s: float) -> bool:
        """``list_tables()`` on the open connection (a directory listing, no table scan)."""
        db = self._db
        if db is None:
            return False

        async def check() -> bool:
            await asyncio.to_thread(db.list_tables)
            return True

        return await probe(deadline_s, check)

    async def close(self) -> None:
        # lancedb connections hold no server socket; drop the handle so table
        # references are released and a fresh open() reopens cleanly.
        self._db = None
