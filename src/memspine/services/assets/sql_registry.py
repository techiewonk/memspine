"""Asset registry stored in the authoritative records database."""

from __future__ import annotations

import asyncio
import builtins
import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    delete,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from memspine.services.assets.models import AssetEntry

__all__ = [
    "SqlAssetRegistry",
    "asset_metadata",
    "asset_registry_table",
]

#: Advisory-lock key of the table creation on Postgres (the side-table key, RM-111 WP5).
_ADVISORY_KEY = 0x4D535053
_LOCK_SQL = f"SELECT pg_advisory_xact_lock({_ADVISORY_KEY})"

asset_metadata = MetaData()

asset_registry_table = Table(
    "asset_registry",
    asset_metadata,
    Column("namespace", Text, primary_key=True),
    Column("asset_id", Text, primary_key=True),
    Column("source_turn_id", Text, nullable=False),
    Column("uri", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("caption", Text, nullable=True),
    Column("search_hint", Text, nullable=True),
    Column("record_id", Text, nullable=True),
    Column("availability", Text, nullable=False),
    Column("content_hash", Text, nullable=True),
    Column("mime", Text, nullable=True),
    Column("size", Integer, nullable=True),
    Column("evidence_status", Text, nullable=False),
    Column("evidence", Text, nullable=True),
    Column("evidence_backend", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("updated_at", Float, nullable=False),
    Index("asset_registry_hash", "namespace", "content_hash"),
    Index("asset_registry_record", "namespace", "record_id"),
)
_FIELDS = (
    "asset_id",
    "source_turn_id",
    "uri",
    "namespace",
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
_SELECT = [asset_registry_table.c[name] for name in _FIELDS]
_PK = ("namespace", "asset_id")


def _entry(row: Any) -> AssetEntry:
    return AssetEntry(**{name: row[i] for i, name in enumerate(_FIELDS)})


class SqlAssetRegistry:
    """Asset rows in the records database, over the storage's (borrowed) async engine."""

    def __init__(self, engine: AsyncEngine, *, clock: Any = time.time) -> None:
        self._engine = engine
        self._clock = clock
        self._ready = False
        self._ready_lock = asyncio.Lock()

    @property
    def _pg(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    async def ensure(self) -> None:
        """Create the tables that do not exist yet (Alembic normally made them)."""
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            async with self._engine.begin() as con:
                if self._pg:
                    await con.exec_driver_sql(_LOCK_SQL)
                await con.run_sync(asset_metadata.create_all, checkfirst=True)
            self._ready = True

    def _row(self, entry: AssetEntry) -> dict[str, Any]:
        row = {name: getattr(entry, name) for name in _FIELDS}
        row["updated_at"] = float(self._clock())
        return row

    def _insert(self, rows: dict[str, Any] | list[dict[str, Any]]) -> Any:
        dialect = postgresql if self._pg else sqlite
        return dialect.insert(asset_registry_table).values(rows)

    async def _upsert(self, con: AsyncConnection, entry: AssetEntry) -> None:
        row = self._row(entry)
        stmt = self._insert(row)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_PK),
            set_={k: stmt.excluded[k] for k in row if k not in _PK},
        )
        await con.execute(stmt)

    @staticmethod
    def _key(namespace: str, asset_id: str) -> Any:
        t = asset_registry_table.c
        return and_(t.namespace == namespace, t.asset_id == asset_id)

    # -- the port --------------------------------------------------------------------------
    async def register(self, entry: AssetEntry) -> AssetEntry:
        await self.ensure()
        t = asset_registry_table.c
        async with self._engine.begin() as con:
            await con.execute(
                self._insert(self._row(entry)).on_conflict_do_nothing(index_elements=list(_PK))
            )
            if entry.record_id is not None:
                await con.execute(
                    update(asset_registry_table)
                    .where(self._key(entry.namespace, entry.asset_id), t.record_id.is_(None))
                    .values(record_id=entry.record_id)
                )
            found = (
                await con.execute(
                    select(*_SELECT).where(self._key(entry.namespace, entry.asset_id))
                )
            ).first()
        assert found is not None
        return _entry(found)

    async def update(self, entry: AssetEntry) -> None:
        await self.ensure()
        async with self._engine.begin() as con:
            await self._upsert(con, entry)

    async def get(self, namespace: str, asset_id: str) -> AssetEntry | None:
        await self.ensure()
        async with self._engine.connect() as con:
            found = (
                await con.execute(select(*_SELECT).where(self._key(namespace, asset_id)))
            ).first()
        return None if found is None else _entry(found)

    async def all(self, namespace: str) -> builtins.list[AssetEntry]:
        await self.ensure()
        t = asset_registry_table.c
        async with self._engine.connect() as con:
            rows = (
                await con.execute(
                    select(*_SELECT).where(t.namespace == namespace).order_by(t.asset_id)
                )
            ).all()
        return [_entry(r) for r in rows]

    async def with_hash(self, namespace: str, content_hash: str) -> AssetEntry | None:
        await self.ensure()
        t = asset_registry_table.c
        async with self._engine.connect() as con:
            found = (
                await con.execute(
                    select(*_SELECT)
                    .where(
                        t.namespace == namespace,
                        t.content_hash == content_hash,
                        t.evidence_status == "ok",
                    )
                    .order_by(t.asset_id)
                    .limit(1)
                )
            ).first()
        return None if found is None else _entry(found)

    async def hash_in_use(self, namespace: str, content_hash: str) -> bool:
        await self.ensure()
        t = asset_registry_table.c
        async with self._engine.connect() as con:
            found = (
                await con.execute(
                    select(t.asset_id)
                    .where(t.namespace == namespace, t.content_hash == content_hash)
                    .limit(1)
                )
            ).first()
        return found is not None

    async def erase_records(
        self, namespace: str, record_ids: Sequence[str]
    ) -> builtins.list[AssetEntry]:
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return []
        await self.ensure()
        t = asset_registry_table.c
        where = and_(t.namespace == namespace, t.record_id.in_(ids))
        async with self._engine.begin() as con:
            gone = [_entry(r) for r in (await con.execute(select(*_SELECT).where(where))).all()]
            if gone:
                await con.execute(delete(asset_registry_table).where(where))
        return gone

    async def erase_namespace(self, namespace: str) -> builtins.list[AssetEntry]:
        await self.ensure()
        t = asset_registry_table.c
        async with self._engine.begin() as con:
            gone = [
                _entry(r)
                for r in (await con.execute(select(*_SELECT).where(t.namespace == namespace))).all()
            ]
            if gone:
                await con.execute(delete(asset_registry_table).where(t.namespace == namespace))
        return gone

    async def close(self) -> None:
        return None  # the engine is borrowed: the storage owns it
