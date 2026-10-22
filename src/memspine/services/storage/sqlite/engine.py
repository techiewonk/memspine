"""SQLite storage adapter (D-36/D-44): a thin :class:`SqlStorage` subclass.

All event-log + read-model logic is dialect-neutral and lives in
:mod:`memspine.services.storage.sql_base`; this adapter supplies only the
SQLite-specific schema bring-up (Alembic for file-backed databases, the real
migration path; ``create_all`` for ``:memory:`` engines a separate migration
connection cannot reach) and the ``sqlite`` capability name. It consumes an
injected :class:`~memspine.clients.sqlite.SQLiteClient` (D-24).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from memspine.clients.sqlite import SQLiteClient
from memspine.services.base import CapabilityManifest
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.migrations import ensure_schema
from memspine.services.storage.sqlite.schema import metadata
from memspine.store.events import EventLogMode

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from memspine.derive.job import LeaseStore
    from memspine.services.storage.records_port import LeasePort

__all__ = ["SQLiteStorage"]


class SQLiteStorage(SqlStorage):
    """Event log + relational read model in one SQLite database."""

    def __init__(
        self,
        client: SQLiteClient,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
    ) -> None:
        super().__init__(client, mode, compress)
        self._sqlite = client  # typed handle for is_memory/path in start()
        self._leases: LeaseStore | None = None
        self._lease_engine: Engine | None = None

    @property
    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="sqlite",
            capability="storage",
            provides=("event_log", "records", "projector_offsets"),
        )

    async def start(self) -> None:
        """Bring the schema to head: Alembic for file-backed databases (the real
        migration path), ``create_all`` only for ``:memory:`` engines, which a
        separate migration connection cannot reach."""
        if self._sqlite.is_memory:
            from memspine.runtime.outbox import outbox_metadata

            async with self._sqlite.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
                # ADR-081 records port: the outbox item table commits with the log (the file
                # path gets it from Alembic revision 0008).
                await conn.run_sync(outbox_metadata.create_all)
        else:
            # #52: an encrypted client supplies a keyed connection for Alembic.
            await asyncio.to_thread(ensure_schema, self._sqlite.path, self._sqlite.sync_creator)
        self._started = True

    def leases(self) -> LeasePort:
        """ADR-081 records port: the lease tables in this database (a private in-process
        table for ``:memory:``). Opened on first use, closed by :meth:`stop`."""
        from memspine.derive.job import LeaseStore
        from memspine.runtime.outbox import sqlite_engine

        if self._leases is None:
            if self._sqlite.is_memory:
                self._leases = LeaseStore(":memory:")
            else:
                # The WP5 side tables of this database, over a sync engine (keyed under
                # SQLCipher, like the outbox's).
                self._lease_engine = sqlite_engine(
                    self._sqlite.path,
                    self._sqlite.sync_creator,
                    busy_timeout_s=self._sqlite.busy_timeout_s,
                )
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
