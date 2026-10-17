"""OpenSearch lexical provider descriptor and engine-local builder."""

from __future__ import annotations

import hashlib
import os
from typing import cast

from pydantic import BaseModel, JsonValue

from memspine.config.remote import endpoint_from_env
from memspine.config.schema.remote_providers import OpenSearchSettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import LexicalBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.registry import LEXICAL_INDEXES, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.services.lexical.base import LexicalStore


def _target_identity(model: BaseModel) -> dict[str, JsonValue]:
    if not isinstance(model, OpenSearchSettings):
        raise ConfigError("OpenSearch target identity received incompatible settings")
    host, scheme, port, _ = endpoint_from_env(model.url_env, allowed_schemes=("http", "https"))
    username = os.environ.get(model.username_env, "") if model.username_env else ""
    password = os.environ.get(model.password_env, "") if model.password_env else ""
    if model.username_env and model.username_env not in os.environ:
        raise ConfigError(f"OpenSearch authentication needs environment variable {model.username_env}")
    if model.password_env and model.password_env not in os.environ:
        raise ConfigError(f"OpenSearch authentication needs environment variable {model.password_env}")
    return {
        "kind": "opensearch",
        "host": host.lower(),
        "scheme": scheme,
        "port": port,
        "auth_digest": hashlib.sha256(f"{username}\0{password}".encode()).hexdigest(),
        "canonical_target": {"kind": "opensearch", "host": host.lower(), "port": port, "index_prefix": model.index_prefix},
    }


class OpenSearchBuilder:
    async def build(self, settings: OpenSearchSettings, ctx: LexicalBuildContext) -> BuildResult[LexicalStore]:
        from memspine.clients.opensearch import OpenSearchClient
        from memspine.services.lexical.opensearch import OpenSearchLexical

        if not ctx.shared.owner_id:
            raise ConfigError("OpenSearch requires a stable runtime.deployment_id")
        scope = hashlib.sha256(ctx.shared.owner_id.encode()).hexdigest()[:20]
        suffix = "" if ctx.generation_id == "active" else f"-g{generation_component(ctx.generation_id)}"
        index = f"{settings.index_prefix}-{scope}{suffix}"
        client = OpenSearchClient(settings)
        ref = ctx.shared.resources.own("lexical", client.close)
        await client.connect()
        service = OpenSearchLexical(client, index=index, owner_id=ctx.shared.owner_id, request_timeout_s=settings.request_timeout_s)
        await service.ensure_index()

        async def probe() -> bool:
            if not await client.health():
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
            ),
        )
