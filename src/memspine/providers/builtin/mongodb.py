"""MongoDB ``records`` provider factory (RM-112): settings -> spec -> connector -> adapter.

NOT REGISTERED YET. The ADR-081 storage-slot rewrite (RM-111: ``storage.<slot>`` settings,
the slot-aware descriptor and container wiring) is landing concurrently, so this module only
has the pieces that registration will use and touches neither ``providers.registry`` nor the
container:

* :class:`MongoRecordsSettings` - the slot settings (moves into the config schema with the
  rewrite);
* :func:`mongo_spec` - settings to the connector's frozen transport spec;
* :func:`build_mongodb_records` - opens the connector and starts the adapter, closing the
  connector again when start fails;
* :data:`RECORDS_MONGODB` - the descriptor facts (slot, name, extra, locality, capabilities)
  with ``wirable=False`` until the slot registry can hold a non-SQL ``records`` provider.

The current ``storage`` category expects a SQL engine (``StorageBuildResult`` and
``ManagedSqlAccess``), which a MongoDB store does not have; registering here would either
lie about that or force a change in the files being rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.clients.mongodb import MongoConnector, MongoSpec
from memspine.store.events import EventLogMode

if TYPE_CHECKING:
    from memspine.services.storage.mongodb import MongoRecordsStore

__all__ = [
    "RECORDS_MONGODB",
    "MongoRecordsSettings",
    "PendingRecordsDescriptor",
    "build_mongodb_records",
    "mongo_spec",
]


class MongoRecordsSettings(BaseModel):
    """``storage.records`` settings for ``provider: mongodb``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, description="mongodb:// or mongodb+srv:// replica-set URL")
    database: str = Field(default="memspine", min_length=1)
    collection_prefix: str = ""
    connect_timeout_s: float = Field(default=10.0, gt=0)
    server_selection_timeout_s: float = Field(default=10.0, gt=0)
    operation_timeout_s: float | None = Field(default=30.0, gt=0)


@dataclass(frozen=True, slots=True)
class PendingRecordsDescriptor:
    """Descriptor facts of a records provider that is built but not yet registered."""

    slot: str
    name: str
    extra: str
    locality: Literal["process", "host", "cluster"]
    capabilities: frozenset[str]
    wirable: bool


RECORDS_MONGODB = PendingRecordsDescriptor(
    slot="records",
    name="mongodb",
    extra="mongodb",
    locality="cluster",
    capabilities=frozenset(
        {"event_log", "outbox", "projector_offsets", "leases", "records", "lineage", "erasure"}
    ),
    wirable=False,  # becomes registrable when the RM-111 slot registry lands
)


def mongo_spec(settings: MongoRecordsSettings) -> MongoSpec:
    return MongoSpec(
        url=settings.url,
        database=settings.database,
        connect_timeout_s=settings.connect_timeout_s,
        server_selection_timeout_s=settings.server_selection_timeout_s,
        operation_timeout_s=settings.operation_timeout_s,
    )


async def build_mongodb_records(
    settings: MongoRecordsSettings,
    *,
    mode: EventLogMode = EventLogMode.FULL,
    compress: bool = False,
    connector: MongoConnector | None = None,
) -> tuple[MongoConnector, MongoRecordsStore]:
    """Open (or reuse a shared, already open) connector and start the records store.

    Returns ``(connector, store)``. Without a ``connector`` argument the caller owns the new one.
    The adapter (and so pymongo) is imported here, so this module imports without the extra."""
    from memspine.services.storage.mongodb import MongoRecordsStore

    owned = connector is None
    conn = connector or MongoConnector(mongo_spec(settings))
    await conn.open()
    try:
        store = MongoRecordsStore(
            conn, mode=mode, compress=compress, collection_prefix=settings.collection_prefix
        )
        await store.start()
    except BaseException:
        if owned:
            await conn.close()
        raise
    return conn, store
