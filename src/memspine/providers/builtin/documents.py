"""``storage.documents`` factories (ADR-081): settings -> spec -> shared connector -> adapter.

Filesystem (RM-111), S3-compatible and Weaviate (RM-112) ship here. MongoDB GridFS is RM-112;
its name is rejected by the config until it lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, JsonValue

from memspine.config.schema.providers import FilesystemDocumentsSettings
from memspine.config.schema.remote_providers import (
    S3DocumentsSettings,
    WeaviateDocumentsSettings,
)
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import DocumentsBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.placement import host_base_path
from memspine.providers.registry import DOCUMENT_STORES, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.services.documents.base import DocumentStore

if TYPE_CHECKING:
    from memspine.clients.s3 import S3Spec

_CAPABILITIES = frozenset({"documents", "namespace_filter", "erase", "stream"})


def filesystem_root(settings: FilesystemDocumentsSettings, ctx: DocumentsBuildContext) -> str:
    """The root directory: ``settings.root``, else ``<records placement base>.documents``."""
    if settings.root is not None:
        return settings.root
    if ctx.placement.in_memory:
        raise ConfigError(
            "storage.documents provider 'filesystem' needs settings.root when storage.records "
            "is in memory (there is no database file to place it beside)"
        )
    return f"{host_base_path(ctx.placement, 'documents', 'filesystem')}.documents"


def _needs_placement(settings: BaseModel) -> bool:
    return isinstance(settings, FilesystemDocumentsSettings) and settings.root is None


class FilesystemBuilder:
    async def build(
        self, settings: FilesystemDocumentsSettings, ctx: DocumentsBuildContext
    ) -> BuildResult[DocumentStore]:
        from memspine.clients.fs import FilesystemConnector, FilesystemSpec
        from memspine.services.documents.filesystem import FilesystemDocumentStore

        spec = FilesystemSpec(filesystem_root(settings, ctx), fsync=settings.fsync)
        # Engines on one root share the connector (and its exclusive section), ADR-079.
        connector, ref = await acquire(
            ctx.shared, FilesystemConnector, spec, resource_id="documents"
        )
        service = FilesystemDocumentStore(connector, max_bytes=settings.max_bytes)
        deadline = ctx.shared.health_timeout_s

        async def probe() -> bool:
            return await connector.health(deadline)

        return BuildResult(
            service,
            ProviderRef("documents", "filesystem"),
            (ref,),
            _CAPABILITIES,
            health_probe=probe,
        )


# -- s3 (RM-112) -----------------------------------------------------------------------------
def s3_spec(settings: S3DocumentsSettings, max_connections: int | None = None) -> S3Spec:
    """Map the slot settings to the connector spec, resolving the named environment variables
    (a missing variable is a ``ConfigError`` naming it, never a value)."""
    from memspine.clients._common.secrets import Secret, required_env
    from memspine.clients.s3 import S3Credentials, S3Spec

    what = "storage.documents provider 's3'"
    endpoint = required_env(settings.endpoint_url_env, what) if settings.endpoint_url_env else None
    credentials = None
    if settings.access_key_id_env is not None and settings.secret_access_key_env is not None:
        credentials = S3Credentials(
            Secret(required_env(settings.access_key_id_env, what)),
            Secret(required_env(settings.secret_access_key_env, what)),
            (
                Secret(required_env(settings.session_token_env, what))
                if settings.session_token_env
                else None
            ),
        )
    return S3Spec(
        bucket=settings.bucket,
        connect_timeout_s=settings.connect_timeout_s,
        request_timeout_s=settings.request_timeout_s,
        prefix=settings.prefix,
        endpoint_url=endpoint,
        region=settings.region,
        profile=settings.profile,
        credentials=credentials,
        addressing_style=settings.addressing_style,
        checksum_mode=settings.checksum_mode,
        max_pool_connections=max_connections,
        create_bucket=settings.create_bucket,
    )


def _s3_target_identity(model: BaseModel) -> dict[str, JsonValue]:
    """The connector's credential-free identity (endpoint, region, bucket, prefix, digest)."""
    if not isinstance(model, S3DocumentsSettings):
        raise ConfigError("S3 target identity received incompatible settings")
    from memspine.clients.s3 import S3Connector

    return cast(dict[str, JsonValue], S3Connector.identity(s3_spec(model)).descriptor_target())


class S3Builder:
    async def build(
        self, settings: S3DocumentsSettings, ctx: DocumentsBuildContext
    ) -> BuildResult[DocumentStore]:
        from memspine.clients.s3 import S3Connector
        from memspine.services.documents.s3 import S3DocumentStore

        pool = ctx.shared.http_pool.max_connections if ctx.shared.http_pool is not None else None
        # Engines on one bucket, prefix and principal share the client (ADR-079).
        connector, ref = await acquire(
            ctx.shared, S3Connector, s3_spec(settings, pool), resource_id="documents"
        )
        service = S3DocumentStore(
            connector,
            max_bytes=settings.max_bytes,
            part_size=settings.part_size_bytes,
            conditional_writes=settings.conditional_writes,
        )
        deadline = ctx.shared.health_timeout_s

        async def probe() -> bool:
            return await connector.health(deadline)

        return BuildResult(
            service,
            ProviderRef("documents", "s3"),
            (ref,),
            _CAPABILITIES,
            health_probe=probe,
        )


# -- weaviate (RM-112) -----------------------------------------------------------------------
def _weaviate_target_identity(model: BaseModel) -> dict[str, JsonValue]:
    from memspine.providers.builtin.weaviate import _target_identity

    return _target_identity(model)


class WeaviateDocumentsBuilder:
    async def build(
        self, settings: WeaviateDocumentsSettings, ctx: DocumentsBuildContext
    ) -> BuildResult[DocumentStore]:
        from memspine.providers.builtin.weaviate import _shared_client
        from memspine.services._weaviate.documents import (
            WeaviateDocumentsBinding,
            documents_collection_name,
        )
        from memspine.services.documents.weaviate import WeaviateDocumentStore

        if not ctx.shared.owner_id:
            raise ConfigError("Weaviate requires a stable deployment.id")
        name = documents_collection_name(settings.collection_prefix, ctx.shared.owner_id)
        # The same client as the vector/lexical slots on this deployment (one identity).
        client, ref = await _shared_client(settings, ctx.shared, "documents")
        service = WeaviateDocumentStore(
            client,
            WeaviateDocumentsBinding(name),
            max_bytes=settings.max_bytes,
            chunk_bytes=settings.chunk_bytes,
        )
        await service.start()

        async def probe() -> bool:
            return await service.probe(settings.request_timeout_s)

        return BuildResult(
            cast(DocumentStore, service),
            ProviderRef("documents", "weaviate"),
            (ref,),
            _CAPABILITIES,
            health_probe=probe,
        )


def _register_remote() -> None:
    if "s3" not in DOCUMENT_STORES:
        DOCUMENT_STORES.register(
            "s3",
            ProviderDescriptor(
                ProviderRef("documents", "s3"),
                S3DocumentsSettings,
                _CAPABILITIES,
                (DependencyRequirement("package", "boto3"),),
                S3Builder(),
                service_manifest=CapabilityManifest("s3", "documents", extra="aws"),
                target_identity=_s3_target_identity,
                locality="cluster",
            ),
        )
    if "weaviate" not in DOCUMENT_STORES:
        DOCUMENT_STORES.register(
            "weaviate",
            ProviderDescriptor(
                ProviderRef("documents", "weaviate"),
                WeaviateDocumentsSettings,
                _CAPABILITIES,
                (DependencyRequirement("package", "weaviate"),),
                WeaviateDocumentsBuilder(),
                service_manifest=CapabilityManifest("weaviate", "documents", extra="weaviate"),
                target_identity=_weaviate_target_identity,
                requires_deployment_id=True,
                locality="cluster",
            ),
        )


def register() -> None:
    _register_filesystem()
    _register_remote()


def _register_filesystem() -> None:
    if "filesystem" not in DOCUMENT_STORES:
        DOCUMENT_STORES.register(
            "filesystem",
            ProviderDescriptor(
                ProviderRef("documents", "filesystem"),
                FilesystemDocumentsSettings,
                _CAPABILITIES,
                (),
                FilesystemBuilder(),
                locality="host",
                single_writer=True,
                needs_placement=_needs_placement,
            ),
        )
