"""``derive.FailureLedger``: the persisted dead-letter list of the envelope-v2 write path (W2-06a, W-14).

One row per item (or whole call) that could not be turned into an envelope even after the one repair re-ask:
the namespace, the window, the position in the model's list, a reason code, the number of attempts, whether
a retry is allowed, and a hash of the item. The item itself is NOT kept unless the tap content opt-in is on
(``tap.content_enabled()``); then a masked, truncated copy rides along for review.

RM-111 WP5 (ADR-081): the ``dead_letters`` table is in the storage database (pass its sync engine), so a
dead letter survives a restart, is encrypted with the database on SQLite/SQLCipher and is shared on
Postgres. Writes to it run on a background thread (the event loop never waits on SQLite's write lock);
``list`` and ``count`` wait for the queued ones first. A path (``:memory:`` for an in-memory store, or a
plain SQLite file for tests and tools) keeps a private table.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text

from memspine.observability import tap
from memspine.store.side_tables import (
    BackgroundWriter,
    ensure_side_tables,
    local_engine,
    write_tx,
)

__all__ = ["DeadLetter", "FailureLedger"]

_INSERT = text(
    "INSERT INTO dead_letters (at, ns, window_id, kind, item_index, code, detail, attempts, "
    "retry_allowed, item_hash, content) VALUES (:at, :ns, :window_id, :kind, :item_index, :code, "
    ":detail, :attempts, :retry_allowed, :item_hash, :content)"
)


@dataclass(frozen=True)
class DeadLetter:
    id: int
    at: float
    ns: str
    window_id: str
    kind: str  # "item" or "window"
    item_index: int
    code: str
    detail: str
    attempts: int
    retry_allowed: bool
    item_hash: str | None
    content: str | None


def item_hash(item: Any) -> str:
    blob = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class FailureLedger:
    """Append-only dead letters; thread-safe. ``target``: the storage database's sync engine, or a
    path (``":memory:"`` for a store with no file)."""

    def __init__(
        self, target: str | Engine = ":memory:", *, clock: Callable[[], float] = time.time
    ) -> None:
        if isinstance(target, Engine):
            self.path = target.url.render_as_string(hide_password=True)
            self._engine, self._owned = target, False
            self._writer: BackgroundWriter | None = BackgroundWriter("dead-letters")
        else:
            self.path = target
            self._engine, self._owned = local_engine(target), True
            self._writer = None
        self._clock = clock
        self._lock = threading.Lock()  # one statement at a time on a private (single) connection
        self._closed = False
        ensure_side_tables(self._engine, ("dead_letters",))

    def _guard(self) -> contextlib.AbstractContextManager[Any]:
        return self._lock if self._owned else contextlib.nullcontext()

    def close(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._owned:
                    self._engine.dispose()

    def _insert(self, row: dict[str, Any]) -> None:
        with self._guard(), write_tx(self._engine) as con:
            con.execute(_INSERT, row)

    def record(
        self,
        ns: str,
        window_id: str,
        index: int,
        code: str,
        *,
        detail: str = "",
        attempts: int = 1,
        retry_allowed: bool = False,
        item: Any = None,
        kind: str = "item",
    ) -> None:
        """Append one dead letter. ``detail`` must already be free of item text (codes and paths)."""
        content: str | None = None
        digest: str | None = None
        if item is not None:
            digest = item_hash(item)
            if tap.content_enabled():  # opt-in only; masked and truncated by tap.content
                kept = tap.content(json.dumps(item, ensure_ascii=False, default=str))
                content = str(kept) if getattr(kept, "kept", False) else None
        if self._closed:
            return
        row = {
            "at": self._clock(),
            "ns": ns,
            "window_id": window_id,
            "kind": kind,
            "item_index": index,
            "code": code,
            "detail": detail[:300],
            "attempts": attempts,
            "retry_allowed": int(retry_allowed),
            "item_hash": digest,
            "content": content,
        }
        if self._writer is not None:
            self._writer.submit(self._insert, row)
        else:
            self._insert(row)

    def _settle(self) -> bool:
        """Wait for the queued writes; False once closed."""
        if self._closed:
            return False
        if self._writer is not None:
            self._writer.drain()
        return True

    def list(self, ns: str | None = None, *, limit: int = 1000) -> list[DeadLetter]:
        if not self._settle():
            return []
        sql = (
            "SELECT id, at, ns, window_id, kind, item_index, code, detail, attempts, "
            "retry_allowed, item_hash, content FROM dead_letters"
        )
        args: dict[str, Any] = {"limit": limit}
        if ns is not None:
            sql += " WHERE ns = :ns"
            args["ns"] = ns
        with self._guard(), self._engine.connect() as con:
            rows = con.execute(text(sql + " ORDER BY id LIMIT :limit"), args).fetchall()
        return [
            DeadLetter(
                int(r[0]),
                float(r[1]),
                r[2],
                r[3],
                r[4],
                int(r[5]),
                r[6],
                r[7],
                int(r[8]),
                bool(r[9]),
                r[10],
                r[11],
            )
            for r in rows
        ]

    def count(self, ns: str | None = None) -> int:
        if not self._settle():
            return 0
        with self._guard(), self._engine.connect() as con:
            if ns is None:
                row = con.execute(text("SELECT COUNT(*) FROM dead_letters")).fetchone()
            else:
                row = con.execute(
                    text("SELECT COUNT(*) FROM dead_letters WHERE ns = :ns"), {"ns": ns}
                ).fetchone()
        return int(row[0]) if row is not None else 0

    def by_code(self, ns: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for letter in self.list(ns, limit=1_000_000):
            out[letter.code] = out.get(letter.code, 0) + 1
        return out
