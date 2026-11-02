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
import contextlib
import os
import shutil
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
                current = self._conn  # read under the lock: compact() may have swapped it
                if current is None:
                    return False
                result = current.execute("RETURN 1")
                return bool(result.has_next() and result.get_next()[0] == 1)

        async def check() -> bool:
            return await asyncio.to_thread(round_trip)

        return await probe(deadline_s, check)

    async def compact(self) -> bool:
        """ADR-095 (graph and mention projections): rewrite the database into a fresh file.

        LadybugDB 0.22 frees a deleted node's or row's slot but leaves its strings in the
        database file pages; ``CHECKPOINT`` does not compact them and there is no ``VACUUM``.
        Under the statement lock (no other statement runs meanwhile): ``EXPORT DATABASE`` the
        live content (schema, data and indexes, so nothing deleted) next to the database,
        ``IMPORT DATABASE`` it into a fresh file, close the old database, replace its file with
        the fresh one (``os.replace``, atomic on one volume) and reopen at the same path. The
        path, and so the pinned graph binding (ADR-081) and every projector's high-water mark,
        is unchanged; only the bytes are new. Extensions the export loads (FTS) are loaded
        again on the reopened connection. Any failure before the swap keeps the old database
        open and untouched. A ``:memory:`` database has no file: nothing to do. Returns False
        when the database could not be rewritten."""
        if self._spec.is_memory or self._conn is None:
            return True
        path = Path(str(self._spec.path))
        if "'" in path.as_posix():  # EXPORT/IMPORT take the directory as a string literal
            return False
        import ladybug

        def rewrite() -> bool:
            with self.statement_lock:
                work = path.with_name(f"{path.name}.compact-{uuid.uuid4().hex[:12]}")
                export, fresh = work / "export", work / path.name
                try:
                    work.mkdir()
                    self._conn.execute(f"EXPORT DATABASE '{export.as_posix()}'")
                    schema = export / "schema.cypher"
                    fts = schema.exists() and "FTS" in schema.read_text(encoding="utf-8")
                    db = ladybug.Database(str(fresh))
                    try:
                        conn = ladybug.Connection(db)
                        try:
                            conn.execute(f"IMPORT DATABASE '{export.as_posix()}'")
                            conn.execute("CHECKPOINT")
                        finally:
                            conn.close()
                    finally:
                        db.close()
                except Exception:
                    shutil.rmtree(work, ignore_errors=True)
                    raise
                old_conn, old_db = self._conn, self._db
                self._conn = self._db = None
                try:
                    old_conn.close()
                    old_db.close()
                    # The closed database checkpointed its WAL; a stale one would be replayed
                    # onto the fresh file.
                    path.with_name(path.name + ".wal").unlink(missing_ok=True)
                    os.replace(fresh, path)
                finally:
                    self._db = ladybug.Database(str(path))
                    self._conn = ladybug.Connection(self._db)
                    shutil.rmtree(work, ignore_errors=True)
                if fts:  # on failure the store degrades to vector-only entity search
                    with contextlib.suppress(Exception):
                        self._conn.execute("LOAD FTS")
                return True

        try:
            return await asyncio.to_thread(rewrite)
        except Exception:
            return False

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
