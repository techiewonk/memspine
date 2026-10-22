"""OpenSearch lexical provider descriptor and engine-local builder."""

from __future__ import annotations

import hashlib
from typing import cast

from pydantic import BaseModel, JsonValue

from memspine.clients.opensearch import OpenSearchClient, OpenSearchSpec
from memspine.config.schema.remote_providers import OpenSearchSettings
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import LexicalBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.registry import LEXICAL_INDEXES, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.services.lexical.base import LexicalStore


def _spec(settings: OpenSearchSettings, max_connections: int | None = None) -> OpenSearchSpec:
    return OpenSearchSpec(
        connect_timeout_s=settings.connect_timeout_s,
        request_timeout_s=settings.request_timeout_s,
        url_env=settings.url_env,
        username_env=settings.username_env,
        password_env=settings.password_env,
        verify_certs=settings.verify_certs,
        max_connections=max_connections,
    )


def _target_identity(model: BaseModel) -> dict[str, JsonValue]:
    """The connector's identity (the one parser), plus the index prefix in the physical
    target: two deployments' prefixes on one cluster are different targets."""
    if not isinstance(model, OpenSearchSettings):
        raise ConfigError("OpenSearch target identity received incompatible settings")
    target = OpenSearchClient.identity(_spec(model)).descriptor_target()
    target["canonical_target"] = {**target["canonical_target"], "index_prefix": model.index_prefix}
    return cast(dict[str, JsonValue], target)


class OpenSearchBuilder:
    async def build(
        self, settings: OpenSearchSettings, ctx: LexicalBuildContext
    ) -> BuildResult[LexicalStore]:
        from memspine.services.lexical.opensearch import OpenSearchLexical

        if not ctx.shared.owner_id:
            raise ConfigError("OpenSearch requires a stable deployment.id")
        scope = hashlib.sha256(ctx.shared.owner_id.encode()).hexdigest()[:20]
        suffix = (
            "" if ctx.generation_id == "active" else f"-g{generation_component(ctx.generation_id)}"
        )
        index = f"{settings.index_prefix}-{scope}{suffix}"
        # One connected client per target and transport settings (ADR-079); the index
        # stays per service. The key holds only the target identity and an auth digest.
        http = ctx.shared.http_pool
        spec = _spec(settings, http.max_connections if http is not None else None)
        client, ref = await acquire(ctx.shared, OpenSearchClient, spec, resource_id="lexical")
        service = OpenSearchLexical(
            client,
            index=index,
            owner_id=ctx.shared.owner_id,
            request_timeout_s=settings.request_timeout_s,
        )
        await service.ensure_index()

        async def probe() -> bool:
            if not await client.health(settings.request_timeout_s):
                return False
            try:
                await service.search("__memspine_health_probe__", "memspine health", top_k=1)
            except Exception:
                return False
            return True

        return BuildResult(
            cast(LexicalStore, service),
            ProviderRef("lexical", "opensearch"),
            (ref,),
            frozenset({"lexical", "namespace_filter", "erase"}),
            health_probe=probe,
        )


def register() -> None:
    if "opensearch" not in LEXICAL_INDEXES:
        LEXICAL_INDEXES.register(
            "opensearch",
            ProviderDescriptor(
                ProviderRef("lexical", "opensearch"),
                OpenSearchSettings,
                frozenset({"lexical", "namespace_filter", "erase"}),
                (DependencyRequirement("package", "opensearchpy"),),
                OpenSearchBuilder(),
                service_manifest=CapabilityManifest("opensearch", "lexical", extra="opensearch"),
                target_identity=_target_identity,
                requires_deployment_id=True,
                locality="cluster",
            ),
        )
