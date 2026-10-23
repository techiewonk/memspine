"""MongoDB factories (RM-112, ``[mongodb]``): settings -> spec -> shared connector -> adapter.

Two descriptors share one connector type:

* ``storage.records: {provider: mongodb}`` - :class:`~memspine.services.storage.mongodb.
  MongoRecordsStore`, the whole ``RecordsStore`` port without SQL (contract:
  ``docs/RECORDS_STORE_CONTRACT.md``). It exposes no SQL access, so the SQL-only features are
  refused before anything opens (:mod:`memspine.providers.records_features`).
* ``storage.documents: {provider: mongodb}`` - :class:`~memspine.services.documents.mongodb.
  MongoDocumentStore`, a GridFS bucket. Unset connection settings come from the records slot
  when it is MongoDB, so both slots resolve to the same spec and share one connector through
  the process registry (ADR-079).

Both are ``cluster`` locality and need a replica set or a sharded cluster (the connector refuses
a standalone ``mongod`` at open). pymongo is imported only inside ``build``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, JsonValue

from memspine.clients.mongodb import MongoConnector, MongoSpec
from memspine.config.schema.providers import MongoDocumentsSettings, MongoRecordsSettings
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import DocumentsBuildContext, StorageBuildContext
from memspine.providers.contracts import (
    BuildResult,
    DependencyRequirement,
    ProjectionPlacement,
    ProviderRef,
    StorageBuildResult,
)
from memspine.providers.registry import DOCUMENT_STORES, STORAGE_BACKENDS, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.store.events import EventLogMode

if TYPE_CHECKING:
    from memspine.services.documents.base import DocumentStore
    from memspine.services.storage.records_port import RecordsStore

__all__ = [
    "DOCUMENTS_CAPABILITIES",
    "RECORDS_CAPABILITIES",
    "documents_spec",
    "mongo_spec",
    "register",
]

#: What a MongoDB records store provides. No ``sql``: SQL-only features are refused for it.
RECORDS_CAPABILITIES = frozenset(
    {
        "records",
        "transactions",
        "event_log",
        "outbox",
        "projector_offsets",
        "leases",
        "lineage",
        "erasure",
        "provider_metadata",
    }
)
DOCUMENTS_CAPABILITIES = frozenset({"documents", "namespace_filter", "erase", "stream"})

#: Connection defaults for a documents slot whose records store is not MongoDB.
_DEFAULTS = MongoRecordsSettings(url="mongodb://unused")


def mongo_spec(settings: MongoRecordsSettings) -> MongoSpec:
    """The connector spec of a ``storage.records`` MongoDB slot."""
    return MongoSpec(
        url=settings.url,
        database=settings.database,
        connect_timeout_s=settings.connect_timeout_s,
        server_selection_timeout_s=settings.server_selection_timeout_s,
        operation_timeout_s=settings.operation_timeout_s,
    )


def documents_spec(
    settings: MongoDocumentsSettings, records: tuple[str, Mapping[str, Any]] | None
) -> MongoSpec:
    """The connector spec of a ``storage.documents`` MongoDB slot: its own fields, else the
    records slot's MongoDB settings (so both slots share one connector), else the defaults."""
    base: MongoRecordsSettings | None = None
    if records is not None and records[0] == "mongodb":
        base = MongoRecordsSettings.model_validate(dict(records[1]))
    url = settings.url or (base.url if base is not None else None)
    if url is None:
        raise ConfigError(
            "storage.documents provider 'mongodb' needs settings.url: storage.records is not "
            "MongoDB, so there is no records deployment to share"
        )
    inherit = base if base is not None and url == base.url else _DEFAULTS
    return MongoSpec(
        url=url,
        database=settings.database or inherit.database,
        connect_timeout_s=settings.connect_timeout_s or inherit.connect_timeout_s,
        server_selection_timeout_s=(
            settings.server_selection_timeout_s or inherit.server_selection_timeout_s
        ),
        operation_timeout_s=(
            settings.operation_timeout_s
            if settings.operation_timeout_s is not None
            else inherit.operation_timeout_s
        ),
    )


def mongodb_placement(
    settings: BaseModel, owner_id: str, data_dir: str | None = None
) -> ProjectionPlacement:
    """A network store has no file to sit beside: host-local projections (LanceDB, Tantivy,
    LadybugDB) live under ``deployment.data_dir/memspine``, as for Postgres."""
    if not isinstance(settings, MongoRecordsSettings):
        raise TypeError("mongodb placement requires MongoRecordsSettings")
    return ProjectionPlacement(
        None if data_dir is None else Path(data_dir) / "memspine", False, owner_id
    )


def _records_target(settings: BaseModel) -> dict[str, JsonValue]:
    if not isinstance(settings, MongoRecordsSettings):
        raise TypeError("mongodb target identity requires MongoRecordsSettings")
    return cast(
        dict[str, JsonValue], MongoConnector.identity(mongo_spec(settings)).descriptor_target()
    )


def _documents_target(settings: BaseModel) -> dict[str, JsonValue]:
    if not isinstance(settings, MongoDocumentsSettings):
        raise TypeError("mongodb documents target identity requires MongoDocumentsSettings")
    if settings.url is None:  # the records slot's deployment, whose identity it records
        return {
            "inherits": "storage.records",
            "database": settings.database,
            "bucket": settings.bucket,
        }
    target = dict(MongoConnector.identity(documents_spec(settings, None)).descriptor_target())
    target["bucket"] = settings.bucket
    return cast(dict[str, JsonValue], target)


class MongoRecordsBuilder:
    async def build(
        self, settings: MongoRecordsSettings, ctx: StorageBuildContext
    ) -> StorageBuildResult[RecordsStore]:
        if ctx.event_log.mode is EventLogMode.EPHEMERAL:
            raise ConfigError(
                "storage.records provider 'mongodb' persists the event log: event_log.mode "
                "ephemeral is refused (use full or rolling)"
            )
        try:
            from memspine.services.storage.mongodb import MongoRecordsStore
        except ImportError as exc:
            raise MissingServiceError("storage:mongodb", extra="mongodb") from exc
        # Engines on one deployment share the client (ADR-079); the registry lease owns its close.
        connector, ref = await acquire(
            ctx.shared, MongoConnector, mongo_spec(settings), resource_id="records"
        )
        service = MongoRecordsStore(
            connector,
            mode=ctx.event_log.mode,
            compress=ctx.event_log.compress,
            collection_prefix=settings.collection_prefix,
        )
        deadline = ctx.shared.health_timeout_s

        async def probe() -> bool:
            return await connector.health(deadline)

        return StorageBuildResult(
            service,
            ProviderRef("storage", "mongodb"),
            (ref,),
            RECORDS_CAPABILITIES,
            None,  # no SQL access: the service is the whole records port
            health_probe=probe,
        )


class MongoDocumentsBuilder:
    async def build(
        self, settings: MongoDocumentsSettings, ctx: DocumentsBuildContext
    ) -> BuildResult[DocumentStore]:
        try:
            from memspine.services.documents.mongodb import MongoDocumentStore
        except ImportError as exc:
            raise MissingServiceError("documents:mongodb", extra="mongodb") from exc
        spec = documents_spec(settings, ctx.records)
        # Same spec as the records slot -> the same shared connector (one client per deployment).
        connector, ref = await acquire(ctx.shared, MongoConnector, spec, resource_id="documents")
        service = MongoDocumentStore(
            connector, bucket=settings.bucket, max_bytes=settings.max_bytes
        )
        await service.start()
        deadline = ctx.shared.health_timeout_s

        async def probe() -> bool:
            return await service.health(deadline)

        return BuildResult(
            service,
            ProviderRef("documents", "mongodb"),
            (ref,),
            DOCUMENTS_CAPABILITIES,
            health_probe=probe,
        )


def register() -> None:
    requirement = (DependencyRequirement("package", "pymongo"),)
    if "mongodb" not in STORAGE_BACKENDS:
        STORAGE_BACKENDS.register(
            "mongodb",
            ProviderDescriptor(
                ProviderRef("storage", "mongodb"),
                MongoRecordsSettings,
                RECORDS_CAPABILITIES,
                requirement,
                MongoRecordsBuilder(),
                service_manifest=CapabilityManifest("mongodb", "records", extra="mongodb"),
                placement=mongodb_placement,
                requires_deployment_id=True,
                target_identity=_records_target,
                locality="cluster",
            ),
        )
    if "mongodb" not in DOCUMENT_STORES:
        DOCUMENT_STORES.register(
            "mongodb",
            ProviderDescriptor(
                ProviderRef("documents", "mongodb"),
                MongoDocumentsSettings,
                DOCUMENTS_CAPABILITIES,
                requirement,
                MongoDocumentsBuilder(),
                service_manifest=CapabilityManifest("mongodb", "documents", extra="mongodb"),
                target_identity=_documents_target,
                locality="cluster",
            ),
        )
