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

ADR-083 (RM-119): ``settings.launch.mode: managed`` makes the slot start (or adopt) a local
``mongod`` through :class:`~memspine.clients.mongodb.launcher.MongodLauncher`, acquired from the
process registry before the connector (which depends on it, so it closes first). A managed slot
is ``host`` locality: ``multi_node`` rejects it and under ``multi_process`` only the writer may
manage it. A documents slot that inherits the records connection also inherits its launcher; the
registry key (binary, port, data_dir) makes both slots share one server.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, JsonValue

from memspine.clients.mongodb import MongoConnector, MongodLauncher, MongodSpec, MongoSpec
from memspine.config import constants
from memspine.config.schema.providers import (
    MongoDocumentsSettings,
    MongoLaunchSettings,
    MongoRecordsSettings,
)
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import (
    DocumentsBuildContext,
    SharedBuildContext,
    StorageBuildContext,
)
from memspine.providers.contracts import (
    BuildResult,
    DependencyRequirement,
    ProjectionPlacement,
    ProviderRef,
    ResourceRef,
    StorageBuildResult,
)
from memspine.providers.registry import (
    DOCUMENT_STORES,
    STORAGE_BACKENDS,
    Locality,
    ProviderDescriptor,
)
from memspine.services.base import CapabilityManifest
from memspine.store.events import EventLogMode

if TYPE_CHECKING:
    from memspine.services.documents.base import DocumentStore
    from memspine.services.storage.records_port import RecordsStore

__all__ = [
    "DOCUMENTS_CAPABILITIES",
    "RECORDS_CAPABILITIES",
    "documents_launch",
    "documents_spec",
    "mongo_spec",
    "mongod_spec",
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
    """The connector spec of a ``storage.records`` MongoDB slot (a managed slot connects to
    its loopback server)."""
    return MongoSpec(
        url=settings.effective_url,
        database=settings.database,
        connect_timeout_s=settings.connect_timeout_s,
        server_selection_timeout_s=settings.server_selection_timeout_s,
        operation_timeout_s=settings.operation_timeout_s,
    )


def mongod_spec(
    launch: MongoLaunchSettings, data_dir: str | None, *, slot: str = "storage.records"
) -> MongodSpec:
    """The launcher spec of a managed slot (ADR-083 section 2): ``launch.data_dir``, else
    ``<deployment.data_dir>/mongod/<port>``."""
    if not launch.managed or launch.version is None or launch.port is None:
        raise ConfigError(f"{slot}.settings.launch.mode is not managed")
    if launch.data_dir is not None:
        dbpath = launch.data_dir
    elif data_dir is not None:
        dbpath = str(Path(data_dir) / "mongod" / str(launch.port))
    else:
        raise ConfigError(
            f"{slot}.settings.launch.mode: managed needs a dbpath: set "
            f"{slot}.settings.launch.data_dir or deployment.data_dir (the default is "
            "<deployment.data_dir>/mongod/<port>)"
        )
    return MongodSpec(
        version=launch.version,
        port=launch.port,
        data_dir=dbpath,
        binary=launch.binary,
        download=launch.download,
        cache_dir=launch.cache_dir,
        replica_set=launch.replica_set,
        start_timeout_s=launch.start_timeout_s,
        cache_size_gb=launch.cache_size_gb,
        shutdown_timeout_s=constants.MONGOD_SHUTDOWN_TIMEOUT_S,
    )


def _records_base(records: tuple[str, Mapping[str, Any]] | None) -> MongoRecordsSettings | None:
    if records is not None and records[0] == "mongodb":
        return MongoRecordsSettings.model_validate(dict(records[1]))
    return None


def documents_launch(
    settings: MongoDocumentsSettings, records: tuple[str, Mapping[str, Any]] | None
) -> tuple[MongoLaunchSettings, str] | None:
    """The managed launch a documents slot needs and the slot that configures it: its own
    ``launch``, else (no ``url``, no ``launch``) the records slot's managed launch; None when
    the server is external."""
    if settings.launch is not None:
        return (settings.launch, "storage.documents") if settings.launch.managed else None
    base = _records_base(records)
    if settings.url is None and base is not None and base.launch.managed:
        return base.launch, "storage.records"
    return None


def documents_spec(
    settings: MongoDocumentsSettings, records: tuple[str, Mapping[str, Any]] | None
) -> MongoSpec:
    """The connector spec of a ``storage.documents`` MongoDB slot: its own fields, else the
    records slot's MongoDB settings (so both slots share one connector), else the defaults."""
    base = _records_base(records)
    url: str | None
    if settings.launch is not None and settings.launch.managed:
        url = settings.launch.managed_url()
    else:
        url = settings.url or (base.effective_url if base is not None else None)
    if url is None:
        raise ConfigError(
            "storage.documents provider 'mongodb' needs settings.url: storage.records is not "
            "MongoDB, so there is no records deployment to share"
        )
    inherit = base if base is not None and url == base.effective_url else _DEFAULTS
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


def _records_locality(settings: BaseModel, records: Locality) -> Locality:
    """ADR-083 section 5: a managed server is host-local; an external one is a cluster."""
    if isinstance(settings, MongoRecordsSettings) and settings.launch.managed:
        return "host"
    return "cluster"


def _documents_locality(settings: BaseModel, records: Locality) -> Locality:
    if not isinstance(settings, MongoDocumentsSettings):
        return "cluster"
    if settings.launch is not None:
        return "host" if settings.launch.managed else "cluster"
    if settings.url is None:  # the records slot's server (managed: host, external: cluster)
        return records
    return "cluster"


def _managed_fix(launch: MongoLaunchSettings | None, slot: str, mode: str) -> str | None:
    if launch is None or not launch.managed:
        return None
    if mode == "multi_node":
        return (
            f"a managed mongod binds 127.0.0.1 on one host; use {slot}.settings.launch.mode: "
            f"external with {slot}.settings.url naming a replica set every node reaches"
        )
    return (
        f"only the process with deployment.writer: true may use {slot}.settings.launch.mode: "
        f"managed; the other processes use {slot}.settings.launch.mode: external with "
        f"{slot}.settings.url {launch.managed_url()} (the writer's port)"
    )


def _records_fix(settings: BaseModel, mode: str) -> str | None:
    if not isinstance(settings, MongoRecordsSettings):
        return None
    return _managed_fix(settings.launch, "storage.records", mode)


def _documents_fix(settings: BaseModel, mode: str) -> str | None:
    if not isinstance(settings, MongoDocumentsSettings):
        return None
    return _managed_fix(settings.launch, "storage.documents", mode)


async def _acquire_launcher(
    ctx: SharedBuildContext, spec: MongodSpec, resource_id: str
) -> ResourceRef:
    """Start (or adopt) the managed server, shared through the registry (ADR-079)."""
    _launcher, ref = await acquire(ctx, MongodLauncher, spec, resource_id=resource_id)
    return ref


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
    if settings.url is None and settings.launch is None:  # the records slot's deployment
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
        refs: tuple[ResourceRef, ...] = ()
        if settings.launch.managed:  # ADR-083: the local server first; the connector needs it
            refs = (
                await _acquire_launcher(
                    ctx.shared, mongod_spec(settings.launch, ctx.data_dir), "records.mongod"
                ),
            )
        # Engines on one deployment share the client (ADR-079); the registry lease owns its close.
        connector, ref = await acquire(
            ctx.shared,
            MongoConnector,
            mongo_spec(settings),
            resource_id="records",
            depends_on=tuple(r.resource_id for r in refs),
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
            (*refs, ref),
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
        refs: tuple[ResourceRef, ...] = ()
        managed = documents_launch(settings, ctx.records)
        if managed is not None:  # ADR-083: the same (binary, port, data_dir) shares one server
            launch, slot = managed
            refs = (
                await _acquire_launcher(
                    ctx.shared, mongod_spec(launch, ctx.data_dir, slot=slot), "documents.mongod"
                ),
            )
        # Same spec as the records slot -> the same shared connector (one client per deployment).
        connector, ref = await acquire(
            ctx.shared,
            MongoConnector,
            spec,
            resource_id="documents",
            depends_on=tuple(r.resource_id for r in refs),
        )
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
            (*refs, ref),
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
                resolve_locality=_records_locality,
                host_local_fix=_records_fix,
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
                resolve_locality=_documents_locality,
                host_local_fix=_documents_fix,
            ),
        )
