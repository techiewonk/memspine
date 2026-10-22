"""PostgreSQL storage adapter (D-36, Phase 6): a thin :class:`SqlStorage`
subclass over an injected :class:`~memspine.clients.postgres.PostgresClient`.

All event-log + read-model logic (and the dialect-dispatched upsert) is shared
in :mod:`memspine.services.storage.sql_base`; this adapter supplies only the
schema bring-up and the ``postgres`` capability name. The schema is materialized
with ``create_all`` from the SAME dialect-neutral metadata SQLite uses — the
``memory_events.seq`` INTEGER primary key becomes a Postgres identity column,
and ``sqlite_autoincrement`` is a SQLite-only table option Postgres ignores. The
``server`` storage profile (F-16d) instead runs the Postgres Alembic tree (``postgres/alembic``,
kept in parity with the SQLite tree by a test).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from memspine.clients.postgres import PostgresClient
from memspine.services.base import CapabilityManifest
from memspine.services.storage.postgres.migrations import ensure_schema
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.schema import metadata
from memspine.store.events import EventLogMode

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from memspine.derive.job import LeaseStore
    from memspine.services.storage.records_port import LeasePort

__all__ = ["PostgresStorage"]


class PostgresStorage(SqlStorage):
    """Event log + relational read model in a PostgreSQL database."""

    def __init__(
        self,
        client: PostgresClient,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
        use_alembic: bool = False,
    ) -> None:
        super().__init__(client, mode, compress)
        self._pg = client
        #: F-16d: the ``server`` profile brings the schema up with the Postgres Alembic tree.
        self._use_alembic = use_alembic
        self._leases: LeaseStore | None = None
        self._lease_engine: Engine | None = None

    @property
    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="postgres",
            capability="storage",
            provides=("event_log", "records", "projector_offsets"),
        )

    async def start(self) -> None:
        if self._use_alembic:
            await asyncio.to_thread(ensure_schema, self._pg._url)
            self._started = True
            return
        from memspine.runtime.outbox import outbox_metadata

        async with self._pg.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            # ADR-081 records port: the outbox tables commit with the log (the job store
            # creates the same tables with ``checkfirst``; Alembic revision pg0008 too).
            await conn.run_sync(outbox_metadata.create_all)
        self._started = True

    def leases(self) -> LeasePort:
        """ADR-081 records port: the lease tables in this database, shared by every
        process on it (database clock, advisory-locked transactions). Opened on first use
        over a private sync engine, closed by :meth:`stop`."""
        from sqlalchemy import create_engine

        from memspine.derive.job import LeaseStore

        if self._leases is None:
            self._lease_engine = create_engine(self._pg._url)
            self._leases = LeaseStore(self._lease_engine)
        return self._leases

    async def stop(self) -> None:
        if self._leases is not None:
            self._leases.close()
            self._leases = None
        if self._lease_engine is not None:
            await asyncio.to_thread(self._lease_engine.dispose)
            self._lease_engine = None
        await super().stop()
