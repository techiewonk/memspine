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

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventLogMode
from memspine.services.base import CapabilityManifest
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.migrations import ensure_schema
from memspine.services.storage.sqlite.schema import metadata

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
            async with self._sqlite.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(ensure_schema, self._sqlite.path)
        self._started = True
