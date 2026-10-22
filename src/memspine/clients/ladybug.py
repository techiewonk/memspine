"""LadybugDB connector (D-26/D-24, RM-113), ``[graph]``.

LadybugDB (PyPI: ``ladybug``, import ``ladybug``, pinned ``>=0.21,<0.22``) is
the maintained, MIT-licensed fork of Kùzu; Kùzu itself was archived upstream on
2025-10-10. The fork kept Kùzu's embedded-Cypher Python API (``Database``/
``Connection``, synchronous client, the same DDL dialect), so this connector
is the one embedded-graph connector (the Kùzu client was removed, ADR-034).

Owns the embedded ``ladybug.Database`` + ``ladybug.Connection``; the graph
service receives this connector injected and never connects itself (D-22).
Import of the ``ladybug`` package is deferred to ``open()`` so constructing
the connector without the extra fails with the actionable D-10 error, and a core
install never imports it at all.
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

__all__ = ["LadybugClient", "LadybugSpec"]

MEMORY = ":memory:"


@dataclass(frozen=True, slots=True)
class LadybugSpec:
    """A database path, or ``":memory:"`` for a private throwaway database."""

    path: str | os.PathLike[str] = MEMORY

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", str(self.path))

    @property
    def is_memory(self) -> bool:
        return str(self.path) == MEMORY


class LadybugClient:
    """One embedded ladybug database per path (``":memory:"`` for throwaway)."""

    kind: ClassVar[str] = "graph.ladybug"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: LadybugSpec) -> None:
        self._spec = spec
        self._db: Any = None
        self._conn: Any = None
        #: One embedded connection is not a pool: every statement unit (the adapter's and the
        #: health probe's) runs under this lock on a worker thread. A threading lock lets
        #: Engines on different event loops share one database per path (ADR-079).
        self.statement_lock = threading.Lock()

    @classmethod
    def identity(cls, spec: LadybugSpec) -> ConnectorIdentity:
        """A single-writer database by canonical path; ``:memory:`` is private (never equal)."""
        if spec.is_memory:
            return ConnectorIdentity(cls.kind, {"path": MEMORY, "private": uuid.uuid4().hex})
        path = os.path.normcase(str(Path(spec.path).expanduser().resolve(strict=False)))
        return ConnectorIdentity(
            cls.kind, {"path": path}, canonical={"kind": "ladybug", "path": path}
        )

    @classmethod
    def locality(cls, spec: LadybugSpec) -> Locality:
        return "process" if spec.is_memory else "host"

    @property
    def spec(self) -> LadybugSpec:
        return self._spec

    @property
    def handle(self) -> Any:
        return self.connection

    @property
    def connection(self) -> Any:
        if self._conn is None:
            raise StorageError("LadybugClient is not open — call open() first")
        return self._conn

    async def open(self) -> None:
        if self._conn is not None:
            return
        try:
            import ladybug
        except ImportError as exc:
            raise MissingServiceError("graph:ladybug", extra="graph") from exc
        path = str(self._spec.path)

        def _open() -> tuple[Any, Any]:
            db = ladybug.Database(path)
            try:
                return db, ladybug.Connection(db)
            except BaseException:
                db.close()
                raise

        self._db, self._conn = await asyncio.to_thread(_open)

    async def health(self, deadline_s: float) -> bool:
        """``RETURN 1`` on the embedded connection."""
        conn = self._conn
        if conn is None:
            return False

        def round_trip() -> bool:
            with self.statement_lock:
                result = conn.execute("RETURN 1")
                return bool(result.has_next() and result.get_next()[0] == 1)

        async def check() -> bool:
            return await asyncio.to_thread(round_trip)

        return await probe(deadline_s, check)

    async def close(self) -> None:
        conn, db = self._conn, self._db
        self._conn = self._db = None
        if conn is None and db is None:
            return

        def _close() -> None:
            if conn is not None:
                conn.close()
            if db is not None:
                db.close()

        await asyncio.to_thread(_close)
