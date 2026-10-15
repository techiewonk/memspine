# ruff: noqa: E501
"""``derive.FailureLedger``: the persisted dead-letter list of the envelope-v2 write path (W2-06a, W-14).

One row per item (or whole call) that could not be turned into an envelope even after the one repair re-ask:
the namespace, the window, the position in the model's list, a reason code, the number of attempts, whether
a retry is allowed, and a hash of the item. The item itself is NOT kept unless the tap content opt-in is on
(``tap.content_enabled()``); then a masked, truncated copy rides along for review. A SQLite file next to the
store (``:memory:`` for an in-memory store), so a dead letter survives a restart.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from memspine.observability import tap

__all__ = ["DeadLetter", "FailureLedger"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, ns TEXT NOT NULL, window_id TEXT NOT NULL,
  kind TEXT NOT NULL, item_index INTEGER NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL,
  attempts INTEGER NOT NULL, retry_allowed INTEGER NOT NULL, item_hash TEXT, content TEXT);
CREATE INDEX IF NOT EXISTS dead_letters_ns ON dead_letters (ns, id);
"""


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
    """Append-only dead letters; thread-safe; ``path=":memory:"`` for a store with no file."""

    def __init__(self, path: str = ":memory:", *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._db.close()
                self._db = None

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
        with self._lock:
            if self._db is None:
                return
            self._db.execute(
                "INSERT INTO dead_letters (at, ns, window_id, kind, item_index, code, detail, "
                "attempts, retry_allowed, item_hash, content) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._clock(),
                    ns,
                    window_id,
                    kind,
                    index,
                    code,
                    detail[:300],
                    attempts,
                    int(retry_allowed),
                    digest,
                    content,
                ),
            )

    def list(self, ns: str | None = None, *, limit: int = 1000) -> list[DeadLetter]:
        with self._lock:
            if self._db is None:
                return []
            sql = (
                "SELECT id, at, ns, window_id, kind, item_index, code, detail, attempts, "
                "retry_allowed, item_hash, content FROM dead_letters"
            )
            args: tuple[Any, ...] = ()
            if ns is not None:
                sql += " WHERE ns = ?"
                args = (ns,)
            rows = self._db.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
        return [
            DeadLetter(
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], bool(r[9]), r[10], r[11]
            )
            for r in rows
        ]

    def count(self, ns: str | None = None) -> int:
        with self._lock:
            if self._db is None:
                return 0
            if ns is None:
                return int(self._db.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
            return int(
                self._db.execute(
                    "SELECT COUNT(*) FROM dead_letters WHERE ns = ?", (ns,)
                ).fetchone()[0]
            )

    def by_code(self, ns: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for letter in self.list(ns, limit=1_000_000):
            out[letter.code] = out.get(letter.code, 0) + 1
        return out
