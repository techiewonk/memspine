"""Lexical index factory; analyzer policy is validated before construction."""

from memspine.config.schema.providers import TantivySettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import LexicalBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import generation_component, host_base_path
from memspine.providers.registry import LEXICAL_INDEXES, ProviderDescriptor
from memspine.providers.shared import own_shared, path_identity, resource_key
from memspine.services.lexical.base import LexicalStore


class TantivyBuilder:
    async def build(
        self, settings: TantivySettings, ctx: LexicalBuildContext
    ) -> BuildResult[LexicalStore]:
        from memspine.clients.tantivy import TantivyConnector
        from memspine.services.lexical.tantivy import TantivyLexical, tantivy_spec

        parts = ([] if settings.analyzer == "default" else [settings.analyzer]) + (
            ["dates"] if settings.date_tokens else []
        )
        suffix = f".tantivy-{'-'.join(parts)}" if parts else ".tantivy"
        if ctx.generation_id != "active":
            if ctx.placement.in_memory:
                raise ConfigError(
                    "controlled lexical generations require durable storage placement"
                )
            suffix += f"-generation-{generation_component(ctx.generation_id)}"
        service: TantivyLexical
        if ctx.placement.in_memory:
            # An in-RAM index is private to this Engine, like its ``:memory:`` event log.
            connector = TantivyConnector(tantivy_spec(None, analyzer=settings.analyzer))
            service = TantivyLexical(connector, date_tokens=settings.date_tokens)
            ref = ctx.shared.resources.own("lexical", connector.close)
        else:
            path = f"{host_base_path(ctx.placement, 'lexical', 'tantivy')}{suffix}"

            async def open_index() -> TantivyLexical:
                connector = TantivyConnector(tantivy_spec(path, analyzer=settings.analyzer))
                return TantivyLexical(connector, date_tokens=settings.date_tokens)

            async def close_index(index: TantivyLexical) -> None:
                await index.connector.close()

            # Single-writer store (ADR-079): one handle per path per process. Its writer lock
            # is a threading lock, so Engines on different event loops share it safely.
            service, ref = await own_shared(
                ctx.shared.resources,
                "lexical",
                resource_key(
                    "lexical.tantivy",
                    {"path": path_identity(path)},
                    {"analyzer": settings.analyzer, "date_tokens": settings.date_tokens},
                    scope="thread",
                ),
                open_index,
                close=close_index,
                registry=ctx.shared.resource_registry(),
            )

        async def probe() -> bool:
            await service.search("__memspine_health_probe__", "", top_k=1)
            return True

        return BuildResult(
            service,
            ProviderRef("lexical", "tantivy"),
            (ref,),
            frozenset({"lexical", "namespace_filter", "erase"}),
            health_probe=probe,
        )


def register() -> None:
    if "tantivy" not in LEXICAL_INDEXES:
        LEXICAL_INDEXES.register(
            "tantivy",
            ProviderDescriptor(
                ProviderRef("lexical", "tantivy"),
                TantivySettings,
                frozenset({"lexical", "namespace_filter", "erase"}),
                (),
                TantivyBuilder(),
                locality="host",
            ),
        )
