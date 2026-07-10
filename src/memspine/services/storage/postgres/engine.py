"""PostgreSQL storage adapter (D-36, Phase 6): a thin :class:`SqlStorage`
subclass over an injected :class:`~memspine.clients.postgres.PostgresClient`.

All event-log + read-model logic (and the dialect-dispatched upsert) is shared
in :mod:`memspine.services.storage.sql_base`; this adapter supplies only the
schema bring-up and the ``postgres`` capability name. The schema is materialized
with ``create_all`` from the SAME dialect-neutral metadata SQLite uses — the
``memory_events.seq`` INTEGER primary key becomes a Postgres identity column,
and ``sqlite_autoincrement`` is a SQLite-only table option Postgres ignores. A
Postgres-side Alembic migration tree is a documented follow-up (ADR-025); for
pre-alpha, ``create_all`` gives the same schema deterministically.
"""

from __future__ import annotations

from memspine.clients.postgres import PostgresClient
from memspine.core.events import EventLogMode
from memspine.services.base import CapabilityManifest
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.schema import metadata

__all__ = ["PostgresStorage"]


class PostgresStorage(SqlStorage):
    """Event log + relational read model in a PostgreSQL database."""

    def __init__(
        self,
        client: PostgresClient,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
    ) -> None:
        super().__init__(client, mode, compress)
        self._pg = client

    @property
    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="postgres",
            capability="storage",
            provides=("event_log", "records", "projector_offsets"),
        )

    async def start(self) -> None:
        async with self._pg.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        self._started = True
