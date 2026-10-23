"""One-time import of a pre-RM-113 asset directory (``registry.sqlite`` plus ``blobs/``).

Before RM-113 item 18 ``ingest.asset_dir`` held the registry as a raw-``sqlite3`` sidecar
(table ``assets``) and the downloaded bytes under ``blobs/<aa>/<sha256>``. The import copies
the rows into the asset registry (the records database) and the bytes into the
``storage.documents`` slot, then renames ``registry.sqlite`` to ``registry.sqlite.imported``
and ``blobs`` to ``blobs.imported`` (never overwriting: ``.imported.1`` and so on).

Idempotent like the RM-111 WP5 side-file import: a digest of the rows is recorded in
``side_file_imports`` (kind ``assets``) in the same transaction as the rows, and content
already recorded is not copied again (a crash between the commit and the rename leaves only
the rename to do). The old registry had no namespace: a row takes the namespace of the record
that carried it (``record_id``); a row whose record is unknown takes ``namespace`` when given
and is skipped otherwise.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import structlog

from memspine.clients.sqlite.files import legacy_file_connection
from memspine.services.assets.models import AssetEntry
from memspine.services.assets.registry import AssetImportResult, AssetRegistry
from memspine.services.documents.base import DocumentStore

__all__ = ["LEGACY_BLOBS", "LEGACY_REGISTRY", "import_asset_dir", "read_legacy_registry"]

_log = structlog.get_logger("memspine.assets")

#: The legacy sidecar and blob directory under the old ``ingest.asset_dir``.
LEGACY_REGISTRY = "registry.sqlite"
LEGACY_BLOBS = "blobs"

_LEGACY_COLUMNS = (
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


def read_legacy_registry(path: Path) -> list[dict[str, Any]]:
    """Every row of the legacy ``assets`` table (only the columns the old schema had)."""
    with legacy_file_connection(path) as con:
        have = {str(r[1]) for r in con.execute("PRAGMA table_info(assets)")}
        if not have:
            return []
        cols = [c for c in _LEGACY_COLUMNS if c in have]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM assets ORDER BY asset_id").fetchall()
    return [dict(zip(cols, row, strict=True)) for row in rows]


def _digest(rows: Sequence[Mapping[str, Any]]) -> str:
    blob = json.dumps(list(rows), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(f"assets\x00{blob}".encode()).hexdigest()


def _free_name(path: Path) -> Path:
    target = path.with_name(path.name + ".imported")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.imported.{n}")
        n += 1
    return target


def _rename(path: Path) -> str | None:
    if not path.exists():
        return None
    target = _free_name(path)
    try:
        path.rename(target)
    except FileNotFoundError:  # another process renamed it first
        return None
    for extra in ("-wal", "-shm", "-journal"):
        leftover = Path(f"{path}{extra}")
        if leftover.exists():
            with contextlib.suppress(OSError):
                leftover.rename(Path(f"{target}{extra}"))
    return str(target)


async def import_asset_dir(
    directory: str | Path,
    registry: AssetRegistry,
    documents: DocumentStore | None,
    namespace_of: Callable[[Sequence[str]], Awaitable[Mapping[str, str]]],
    *,
    namespace: str | None = None,
) -> AssetImportResult | None:
    """Import ``directory`` once. None when it holds no legacy registry.

    ``namespace_of`` maps record ids to their namespaces (the storage lookup). Without a
    documents slot the rows are imported, ``blobs/`` is left in place and a warning names
    ``storage.documents``."""
    root = Path(directory)
    path = root / LEGACY_REGISTRY
    if not path.is_file():
        return None
    try:
        raw = read_legacy_registry(path)
    except sqlite3.Error as exc:
        _log.warning("asset.import_skipped", path=str(path), error=type(exc).__name__)
        return None
    owners = await namespace_of([str(r["record_id"]) for r in raw if r.get("record_id")])
    entries: list[AssetEntry] = []
    skipped = 0
    for row in raw:
        ns = owners.get(str(row.get("record_id") or "")) or namespace
        if ns is None:
            skipped += 1
            continue
        entries.append(replace(AssetEntry(**row), namespace=ns))
    blobs = root / LEGACY_BLOBS
    copied = 0
    if documents is not None and blobs.is_dir():
        # Bytes first: a re-run after a crash puts the same content again (idempotent by digest).
        for entry in entries:
            digest = entry.content_hash
            if not digest:
                continue
            blob = blobs / digest[:2] / digest
            if not blob.is_file():
                continue
            await documents.put(
                entry.namespace,
                blob.read_bytes(),
                media_type=entry.mime or "application/octet-stream",
                metadata={"kind": "asset"},
                record_ids=(entry.record_id,) if entry.record_id else (),
                expected_digest=digest,
            )
            copied += 1
    elif blobs.is_dir():
        _log.warning(
            "asset.import_blobs_kept",
            path=str(blobs),
            detail="no storage.documents slot: the downloaded bytes stay in place",
        )
    digest = _digest(raw)
    rows = await registry.import_rows(entries, digest=digest, source=str(path))
    renamed = [_rename(path)]
    if documents is not None:
        renamed.append(_rename(blobs))
    result = AssetImportResult(
        source=str(path),
        rows=rows or 0,
        skipped=skipped,
        documents=copied,
        already_imported=rows is None,
        renamed_to=tuple(r for r in renamed if r is not None),
    )
    _log.info(
        "asset.imported",
        path=str(path),
        rows=result.rows,
        skipped=skipped,
        documents=copied,
        already_imported=result.already_imported,
    )
    return result
