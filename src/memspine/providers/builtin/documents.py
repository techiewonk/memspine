"""``storage.documents`` factories (ADR-081): settings -> spec -> shared connector -> adapter.

Filesystem ships now (RM-111). MongoDB GridFS, S3-compatible and Weaviate documents are RM-112;
their names are rejected by the config until they land.
"""

from __future__ import annotations

from pydantic import BaseModel

from memspine.config.schema.providers import FilesystemDocumentsSettings
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import DocumentsBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import host_base_path
from memspine.providers.registry import DOCUMENT_STORES, ProviderDescriptor
from memspine.services.documents.base import DocumentStore

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


def register() -> None:
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
