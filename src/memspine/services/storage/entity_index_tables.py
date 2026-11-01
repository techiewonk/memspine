"""RM-130 (ADR-089): the entity name index, a rebuildable projection kept in storage.records.

One generic row table holds every kind of index row, so the SQL, MongoDB and in-memory stores
share one tiny contract (``put`` / ``select`` / ``delete`` / ``clear``). The rows are written only
by the ``entity_index`` projector (:mod:`memspine.memories.associative.entity_index`) from the
entity ids, links, aliases and merges recorded in the event log; they are never a second source
of truth: ``clear`` + replay reproduces them.

Row kinds (``kind`` / ``key`` / ``entity_id`` / ``ref``):

* ``entity``: ``key`` = ``entity_id`` = the opaque id; ``data`` = name, type, basis, candidates;
* ``name``:   ``key`` = canonical name or alias, ``ref`` = ``name`` or ``alias:<id>``;
  ``match`` = the name's match key (the read path's seed lookup);
* ``link``:   ``key`` = record id, ``ref`` = the mention's name fingerprint; ``data`` = basis,
  status and candidates of that mention's link;
* ``merge``:  ``key`` = merge id, ``entity_id`` = kept entity, ``ref`` = absorbed entity;
* ``cite``:   ``key`` = an evidence record id, ``entity_id`` = the merge / alias id it supports,
  ``ref`` = ``merge`` or ``alias``.

DS-12: ``select_after`` reads one kind's rows in ``(seq, key, entity_id, ref)`` order after a
keyset cursor, one bounded page at a time (the KNOWN refresh's incremental read).
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import orjson
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    or_,
    select,
    tuple_,
)
from sqlalchemy.ext.asyncio import AsyncEngine

#: A ``select_after`` keyset cursor: ``(seq, key, entity_id, ref)`` of the last row read.
RowCursor = tuple[int, str, str, str]

__all__ = [
    "EntityIndexRow",
    "EntityIndexStore",
    "MemoryEntityIndexStore",
    "SqlEntityIndexStore",
    "entity_index_metadata",
    "entity_index_store_of",
    "entity_index_table",
]

entity_index_metadata = MetaData()

entity_index_table = Table(
    "entity_index",
    entity_index_metadata,
    Column("namespace", String, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("entity_id", String, primary_key=True),
    Column("ref", String, primary_key=True),
    Column("match", String, nullable=False, default=""),
    Column("data", Text, nullable=False, default="{}"),
    Column("seq", Integer, nullable=False, default=0),
    Index("ix_entity_index_match", "namespace", "kind", "match"),
    Index("ix_entity_index_entity", "namespace", "kind", "entity_id"),
    # DS-12: the incremental (keyset) read of the KNOWN refresh
    Index("ix_entity_index_seq", "namespace", "kind", "seq"),
)


@dataclass(frozen=True)
class EntityIndexRow:
    namespace: str
    kind: str
    key: str
    entity_id: str
    ref: str = ""
    match: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    @property
    def pk(self) -> tuple[str, str, str, str, str]:
        return (self.namespace, self.kind, self.key, self.entity_id, self.ref)


@runtime_checkable
class EntityIndexStore(Protocol):
    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        """Insert or replace rows by primary key."""
        ...

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]:
        """Rows matching every given column, ordered by primary key."""
        ...

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None: ...

    async def clear(self) -> None: ...

    async def select_after(
        self, namespace: str, kind: str, *, after: RowCursor | None, limit: int
    ) -> list[EntityIndexRow]:
        """At most ``limit`` rows of ``kind`` past the cursor ``after`` (None: from the start),
        ordered by ``(seq, key, entity_id, ref)``."""
        ...


def _matches(row: EntityIndexRow, wanted: dict[str, str | None]) -> bool:
    return all(v is None or getattr(row, k) == v for k, v in wanted.items())


def row_cursor(row: EntityIndexRow) -> RowCursor:
    """The ``select_after`` cursor just past ``row``."""
    return (row.seq, row.key, row.entity_id, row.ref)


class MemoryEntityIndexStore:
    """The in-process store (fakes and storage without the table)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, str, str], EntityIndexRow] = {}

    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        for row in rows:
            self._rows[row.pk] = row

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]:
        wanted = {"key": key, "entity_id": entity_id, "ref": ref, "match": match}
        return [
            row
            for pk, row in sorted(self._rows.items())
            if pk[0] == namespace and pk[1] == kind and _matches(row, wanted)
        ]

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        wanted = {"key": key, "entity_id": entity_id, "ref": ref}
        for pk in [
            pk
            for pk, row in self._rows.items()
            if pk[0] == namespace and pk[1] == kind and _matches(row, wanted)
        ]:
            del self._rows[pk]

    async def clear(self) -> None:
        self._rows.clear()

    async def select_after(
        self, namespace: str, kind: str, *, after: RowCursor | None, limit: int
    ) -> list[EntityIndexRow]:
        rows = sorted(
            (
                row
                for pk, row in self._rows.items()
                if pk[0] == namespace
                and pk[1] == kind
                and (after is None or row_cursor(row) > after)
            ),
            key=row_cursor,
        )
        return rows[: max(limit, 0)]


class SqlEntityIndexStore:
    """The ``entity_index`` table on the records database (SQLite / Postgres)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def _insert(self) -> Any:
        if self._engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            return pg_insert(entity_index_table)
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(entity_index_table)

    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        if not rows:
            return
        t = entity_index_table
        async with self._engine.begin() as conn:
            for row in rows:
                stmt = self._insert().values(
                    namespace=row.namespace,
                    kind=row.kind,
                    key=row.key,
                    entity_id=row.entity_id,
                    ref=row.ref,
                    match=row.match,
                    data=orjson.dumps(row.data, option=orjson.OPT_SORT_KEYS).decode(),
                    seq=row.seq,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[t.c.namespace, t.c.kind, t.c.key, t.c.entity_id, t.c.ref],
                    set_={
                        "match": stmt.excluded.match,
                        "data": stmt.excluded.data,
                        "seq": stmt.excluded.seq,
                    },
                )
                await conn.execute(stmt)

    @staticmethod
    def _where(namespace: str, kind: str, **cols: str | None) -> Any:
        t = entity_index_table
        clauses = [t.c.namespace == namespace, t.c.kind == kind]
        clauses.extend(t.c[name] == value for name, value in cols.items() if value is not None)
        return and_(*clauses)

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]:
        t = entity_index_table
        stmt = (
            select(t)
            .where(self._where(namespace, kind, key=key, entity_id=entity_id, ref=ref, match=match))
            .order_by(t.c.key, t.c.entity_id, t.c.ref)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            EntityIndexRow(
                namespace=r.namespace,
                kind=r.kind,
                key=r.key,
                entity_id=r.entity_id,
                ref=r.ref,
                match=r.match,
                data=dict(orjson.loads(r.data)),
                seq=int(r.seq),
            )
            for r in rows
        ]

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        stmt = delete(entity_index_table).where(
            self._where(namespace, kind, key=key, entity_id=entity_id, ref=ref)
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def clear(self) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(entity_index_table))

    async def select_after(
        self, namespace: str, kind: str, *, after: RowCursor | None, limit: int
    ) -> list[EntityIndexRow]:
        if limit <= 0:
            return []
        t = entity_index_table
        stmt = select(t).where(t.c.namespace == namespace, t.c.kind == kind)
        if after is not None:
            seq, key, entity_id, ref = after
            stmt = stmt.where(
                or_(
                    t.c.seq > seq,
                    and_(
                        t.c.seq == seq,
                        tuple_(t.c.key, t.c.entity_id, t.c.ref) > tuple_(key, entity_id, ref),
                    ),
                )
            )
        stmt = stmt.order_by(t.c.seq, t.c.key, t.c.entity_id, t.c.ref).limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            EntityIndexRow(
                namespace=r.namespace,
                kind=r.kind,
                key=r.key,
                entity_id=r.entity_id,
                ref=r.ref,
                match=r.match,
                data=dict(orjson.loads(r.data)),
                seq=int(r.seq),
            )
            for r in rows
        ]


_FALLBACK: weakref.WeakKeyDictionary[Any, MemoryEntityIndexStore] = weakref.WeakKeyDictionary()
_FALLBACK_BY_ID: dict[int, tuple[Any, MemoryEntityIndexStore]] = {}


def entity_index_store_of(storage: Any) -> EntityIndexStore:
    """The entity index store of a records store: its own (``entity_index_store()``), else one
    in-process store per storage object (fakes, stores without the table)."""
    own = getattr(storage, "entity_index_store", None)
    if callable(own):
        found = own()
        if isinstance(found, EntityIndexStore):
            return found
    try:
        store = _FALLBACK.get(storage)
        if store is None:
            store = _FALLBACK[storage] = MemoryEntityIndexStore()
        return store
    except TypeError:  # not weak-referenceable
        held = _FALLBACK_BY_ID.get(id(storage))
        if held is None or held[0] is not storage:
            held = _FALLBACK_BY_ID[id(storage)] = (storage, MemoryEntityIndexStore())
        return held[1]
