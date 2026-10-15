"""Asset registry: a small sqlite sidecar (``:memory:`` when no directory is configured).

It is a cache of identity and derived evidence, not a second source of truth: the record
carries the ``asset:<id>`` tag, the registry carries the URI and what resolving it gave.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from memspine.services.assets.models import AssetEntry

__all__ = ["AssetRegistry"]

_COLUMNS = (
    "asset_id",
    "source_turn_id",
    "uri",
    "kind",
    "caption",
    "search_hint",
    "record_id",
    "availability",
    "content_hash",
    "mime",
    "size",
    "evidence_status",
    "evidence",
    "evidence_backend",
    "error",
)
_SELECT = ", ".join(_COLUMNS)


def _row(found: tuple[object, ...]) -> AssetEntry:
    return AssetEntry(**dict(zip(_COLUMNS, found, strict=True)))  # type: ignore[arg-type]


class AssetRegistry:
    def __init__(self, directory: str | Path | None = None) -> None:
        if directory is None:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path / "registry.sqlite"), check_same_thread=False)
        cols = ", ".join(f"{c} INTEGER" if c == "size" else f"{c} TEXT" for c in _COLUMNS[1:])
        self._db.execute(f"CREATE TABLE IF NOT EXISTS assets (asset_id TEXT PRIMARY KEY, {cols})")
        self._db.commit()

    def register(self, entry: AssetEntry) -> AssetEntry:
        """Idempotent: an asset already registered keeps its resolved state; only a missing
        ``record_id`` is filled in."""
        existing = self.get(entry.asset_id)
        if existing is not None:
            if existing.record_id is None and entry.record_id is not None:
                existing.record_id = entry.record_id
                self.update(existing)
            return existing
        self.update(entry)
        return entry

    def update(self, entry: AssetEntry) -> None:
        row = entry.as_dict()
        marks = ", ".join("?" for _ in _COLUMNS)
        self._db.execute(
            f"INSERT OR REPLACE INTO assets ({_SELECT}) VALUES ({marks})",
            [row[c] for c in _COLUMNS],
        )
        self._db.commit()

    def get(self, asset_id: str) -> AssetEntry | None:
        found = self._db.execute(
            f"SELECT {_SELECT} FROM assets WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        return None if found is None else _row(found)

    def all(self) -> list[AssetEntry]:
        rows = self._db.execute(f"SELECT {_SELECT} FROM assets ORDER BY asset_id").fetchall()
        return [_row(r) for r in rows]

    def with_hash(self, content_hash: str) -> AssetEntry | None:
        """Another asset whose bytes hashed the same and already has evidence (reuse it)."""
        found = self._db.execute(
            f"SELECT {_SELECT} FROM assets WHERE content_hash = ? "
            "AND evidence_status = 'ok' LIMIT 1",
            (content_hash,),
        ).fetchone()
        return None if found is None else _row(found)

    def close(self) -> None:
        self._db.close()
