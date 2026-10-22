"""Lexical index factory; analyzer policy is validated before construction."""

from memspine.config.schema.providers import TantivySettings
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import LexicalBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import generation_component, host_base_path
from memspine.providers.registry import LEXICAL_INDEXES, ProviderDescriptor, placed_locality
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
            # Single-writer index (ADR-079): one connector (index, writer, writer lock and the
            # uncommitted flag) per path per process, shared through the registry; each Engine
            # gets its own adapter, whose RM-72 commit hold is per adapter token. The writer
            # lock is a threading lock, so Engines on different event loops share it safely.
            # ``date_tokens`` changes the path suffix, so the path already separates it. The
            # index still opens lazily (RM-74 warm-up or first use), off the event loop.
            connector, ref = await acquire(
                ctx.shared,
                TantivyConnector,
                tantivy_spec(path, analyzer=settings.analyzer),
                resource_id="lexical",
                lazy=True,
            )
            service = TantivyLexical(connector, date_tokens=settings.date_tokens)

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
                resolve_locality=placed_locality,
                single_writer=True,
                needs_placement=True,
            ),
        )
