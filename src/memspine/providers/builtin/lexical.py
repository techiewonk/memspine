"""Lexical index factory; analyzer policy is validated before construction."""

from memspine.config.schema.providers import TantivySettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import LexicalBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.registry import LEXICAL_INDEXES, ProviderDescriptor
from memspine.services.lexical.base import LexicalStore


class TantivyBuilder:
    async def build(
        self, settings: TantivySettings, ctx: LexicalBuildContext
    ) -> BuildResult[LexicalStore]:
        from memspine.services.lexical.tantivy import TantivyLexical

        parts = ([] if settings.analyzer == "default" else [settings.analyzer]) + (
            ["dates"] if settings.date_tokens else []
        )
        suffix = f".tantivy-{'-'.join(parts)}" if parts else ".tantivy"
        if ctx.generation_id != "active":
            if ctx.placement.in_memory:
                raise ConfigError("controlled lexical generations require durable storage placement")
            suffix += f"-generation-{generation_component(ctx.generation_id)}"
        path = None if ctx.placement.in_memory else f"{ctx.placement.base_path}{suffix}"
        service = TantivyLexical(path, analyzer=settings.analyzer, date_tokens=settings.date_tokens)
        ref = ctx.shared.resources.own("lexical", service.close)

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
            ),
        )
