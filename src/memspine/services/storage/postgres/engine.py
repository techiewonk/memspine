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

from memspine.clients.postgres import PostgresClient
from memspine.services.base import CapabilityManifest
from memspine.services.storage.postgres.migrations import ensure_schema
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.schema import metadata
from memspine.store.events import EventLogMode

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
        async with self._pg.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        self._started = True
