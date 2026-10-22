"""The dependency-injection container (F-08; RESTRUCTURE section 1, ARCHITECTURE section 8).

The engine used to wire its own services in ``Engine._start_inner``. The builders now live here
as :mod:`dishka` providers, in three scopes:

``APP``      long-lived services, built once per engine start and finalised at ``stop``: the
             resolved config, secrets, storage, the KV cache, the embedder, the vector / lexical /
             graph stores, the LLM router, the prompt registry, the task runner.
``JOB``      one background job: a fresh :class:`PipelineContext` (the derivation context a
             pipeline run receives).
``REQUEST``  one read or write call: a fresh :class:`RequestState` for the call's scope / viewer.

Infrastructure services resolve through central provider descriptors. Each descriptor validates
strict settings, declares dependencies and capabilities, and constructs a typed service with a
category-specific context. :class:`Wiring` owns SQL access, lifecycle resources and build results;
engine callbacks arrive through :class:`WiringHooks`. The engine requests services in dependency
order, and optional providers are acquired only when their features are enabled.

``make_container`` passes ``lock_factory=None``: the container takes no lock of its own (a
resolution never runs a model call, and RESTRUCTURE section 4 keeps model calls out of locks).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
import threading
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from dishka import AsyncContainer, BaseScope, Provider, make_async_container, new_scope

from memspine.config.loader import ResolvedConfig, default_template, load_config
from memspine.config.schema import MemspineConfig
from memspine.config.schema.base import ConfigInputError
from memspine.config.schema.root import freeze_effective_config
from memspine.config.schema.storage import SlotConfig
from memspine.core.registry import SERVICE_EXTRAS, dependency_closure, missing_services
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.ingest import windows as _ingest_windows
from memspine.memories.semantic.entities import EntityExtractor, LLMEntityExtractor
from memspine.memories.semantic.write_pipeline import (
    ExtractEdges,
    GraphWritePipeline,
    WritePipeline,
    cardinality_map,
    extraction_rounds,
)
from memspine.models.profiles import bound_llm_roles, resolve_llm_profile
from memspine.observability.logging import get_logger
from memspine.plugins import enable as enable_plugins
from memspine.prompts.registry import PromptRegistry
from memspine.providers.contexts import (
    CacheBuildContext,
    ExtractionBuildContext,
    GraphBuildContext,
    LexicalBuildContext,
    LLMBuildContext,
    SharedBuildContext,
    StorageBuildContext,
    VectorBuildContext,
)
from memspine.providers.contracts import (
    BuildResult,
    ProviderRef,
    ResourceRef,
    SqlAccess,
    StorageBuildResult,
)
from memspine.providers.lifecycle import (
    ProviderLifecycleManager,
    ProviderResourceCleanupError,
    ResourceOwnershipError,
)
from memspine.providers.placement import (
    generation_component,
    host_base_path,
    storage_placement,
)
from memspine.providers.registry import (
    CACHE_BACKENDS as PROVIDER_CACHE_BACKENDS,
)
from memspine.providers.registry import (
    EMBEDDERS as PROVIDER_EMBEDDERS,
)
from memspine.providers.registry import (
    EXTRACTORS as PROVIDER_EXTRACTORS,
)
from memspine.providers.registry import (
    GRAPH_STORES as PROVIDER_GRAPH_STORES,
)
from memspine.providers.registry import (
    LEXICAL_INDEXES as PROVIDER_LEXICAL_INDEXES,
)
from memspine.providers.registry import (
    LLM_PROVIDERS as PROVIDER_LLM_PROVIDERS,
)
from memspine.providers.registry import (
    RENAMED_PROVIDERS,
    ProviderDescriptor,
    get_registry,
    validate_settings,
)
from memspine.providers.registry import (
    STORAGE_BACKENDS as PROVIDER_STORAGE_BACKENDS,
)
from memspine.providers.registry import (
    VECTOR_STORES as PROVIDER_VECTOR_STORES,
)
from memspine.providers.selection import ProviderSelectionError, select_provider
from memspine.providers.shared import ResourceRegistry, RetentionPolicy, shared_registry
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.runner import InlineRunner as OutboxRunner
from memspine.services.cache.base import KVCache
from memspine.services.cache.semantic import CachedEmbedding, CachedExtractor
from memspine.services.embedding.base import EmbeddingService
from memspine.services.extraction.base import ExtractionModels
from memspine.services.graph.base import GraphStore
from memspine.services.lexical.base import LexicalStore
from memspine.services.llm import structured as _structured
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.services.llm.escalation import (
    EscalatingLLM,
    ProfiledLLM,
    profiled_provider_identity,
)
from memspine.services.llm.tier_gate import TierGatedLLM, is_local_binding
from memspine.services.rerank.base import Reranker
from memspine.services.secrets.base import SecretsService
from memspine.services.secrets.chained import ChainedSecrets
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.vector.base import VectorStore
from memspine.workers.inline import InlineRunner
from memspine.workers.runner import TaskRunner

__all__ = [
    "Capabilities",
    "DecisionState",
    "EngineSpec",
    "JobInfo",
    "JobState",
    "MemScope",
    "OutboxHandle",
    "RequestInfo",
    "RequestState",
    "SemanticExtractor",
    "SemanticWritePipeline",
    "Wiring",
    "WiringHooks",
    "client_is_memory",
    "derived_base",
    "job_scope",
    "lexical_projector_name",
    "lexical_variant",
    "make_container",
    "make_neo4j_projection",
    "make_outbox_runner",
    "memory_policy",
    "neo4j_outbox_on",
    "preflight_projection_bindings",
    "prewarm_provider_imports",
    "request_scope",
    "resolve_graph_pin",
]

_log = get_logger(__name__)


def _descriptor(registry: Any, name: str, path: str) -> ProviderDescriptor:
    try:
        registry = get_registry(registry.kind)
        descriptor = registry.get(name)
    except Exception as exc:
        raise ConfigError(f"unknown provider at {path}: {name!r}") from exc
    if not isinstance(descriptor, ProviderDescriptor):
        raise ConfigError(f"provider {path}={name!r} is not registered as a typed provider")
    return descriptor


def _settings(descriptor: ProviderDescriptor, raw: Any) -> Any:
    return validate_settings(descriptor, raw if isinstance(raw, Mapping) else {})


def prewarm_provider_imports(config: MemspineConfig) -> threading.Thread | None:
    """PA-046: import the selected providers' declared ``prewarm_modules`` on a daemon
    thread, so a slow third-party import overlaps the rest of startup.

    Best effort only: an unknown provider, a missing package or an import error is left
    for the provider build to report through its normal (Missing)ServiceError path.
    Returns the thread (None when there is nothing to import) for tests.
    """
    selected = (
        (PROVIDER_VECTOR_STORES, config.storage.vector.provider, "storage.vector.provider"),
    )
    modules: list[str] = []
    for registry, name, path in selected:
        try:
            descriptor = _descriptor(registry, name, path)
        except ConfigError:
            continue
        modules.extend(
            module
            for module in descriptor.prewarm_modules
            if module not in sys.modules and importlib.util.find_spec(module) is not None
        )
    if not modules:
        return None

    def _import_all() -> None:
        for module in modules:
            try:
                importlib.import_module(module)
            except Exception:
                # Best effort: the provider build reports the real import error.
                return

    thread = threading.Thread(target=_import_all, name="memspine-prewarm", daemon=True)
    thread.start()
    return thread


def _owner_id(config: MemspineConfig, wiring: Wiring | None = None) -> str:
    return (
        config.deployment.id or (wiring.owner_id if wiring is not None else None) or "pending-owner"
    )


def _storage_placement(config: MemspineConfig, wiring: Wiring | None) -> Any:
    return storage_placement(config, _owner_id(config, wiring))


async def _build_provider(
    wiring: Wiring,
    descriptor: ProviderDescriptor,
    settings: Any,
    context: Any,
    *,
    result_key: str | None = None,
) -> BuildResult[Any]:
    """Run a typed builder under the configured startup deadline."""
    if descriptor.requires_deployment_id and not wiring.explicit_owner_id:
        raise ConfigError(
            f"deployment.id is required by shared provider "
            f"{descriptor.ref.category}.{descriptor.ref.name}"
        )
    startup_deadline = None
    try:
        if wiring.startup_timeout_s is None:
            result = await descriptor.builder.build(settings, context)
        else:
            startup_deadline = asyncio.timeout(wiring.startup_timeout_s)
            async with startup_deadline:
                result = await descriptor.builder.build(settings, context)
    except TimeoutError:
        provider = f"{descriptor.ref.category}.{descriptor.ref.name}"
        if startup_deadline is not None and startup_deadline.expired():
            raise ConfigError(
                f"provider {provider} exceeded runtime.provider_services.startup_timeout_s"
            ) from None
        raise ConfigError(f"provider {provider} exceeded a configured operation timeout") from None
    except (ConfigError, MissingServiceError):
        raise
    except Exception as exc:
        raise ConfigError(
            f"provider {descriptor.ref.category}.{descriptor.ref.name} failed during startup "
            f"({type(exc).__name__})"
        ) from None
    if not isinstance(result, BuildResult):
        raise ConfigError(
            f"provider builder {descriptor.ref.category}.{descriptor.ref.name} "
            "did not return a BuildResult"
        )
    if not isinstance(result.provider, ProviderRef):
        raise ConfigError(
            f"provider builder {descriptor.ref.category}.{descriptor.ref.name} "
            "returned an invalid provider identity"
        )
    if result.provider != descriptor.ref:
        raise ConfigError(
            f"provider builder identity mismatch for {descriptor.ref.category}.{descriptor.ref.name}"
        )
    if not isinstance(result.effective_capabilities, frozenset) or not all(
        isinstance(capability, str) and capability for capability in result.effective_capabilities
    ):
        raise ConfigError(
            f"provider builder {descriptor.ref.category}.{descriptor.ref.name} "
            "returned invalid effective capabilities"
        )
    if not isinstance(result.resources, tuple) or not all(
        isinstance(resource, ResourceRef) for resource in result.resources
    ):
        raise ConfigError(
            f"provider builder {descriptor.ref.category}.{descriptor.ref.name} "
            "returned invalid resource ownership references"
        )
    try:
        wiring.resources.validate_references(result.resources)
    except ResourceOwnershipError:
        raise ConfigError(
            f"provider builder {descriptor.ref.category}.{descriptor.ref.name} "
            "returned an unregistered resource reference"
        ) from None
    _validate_provider_result(descriptor, result, context)
    identity = result_key or ".".join(
        (
            descriptor.ref.category,
            descriptor.ref.name,
            *(ref.resource_id for ref in result.resources),
        )
    )
    wiring.provider_results[identity] = result
    return result


_PROVIDER_BASE_CAPABILITIES: dict[str, frozenset[str]] = {
    "storage": frozenset({"sql"}),
    "cache": frozenset({"kv", "ttl"}),
    "vector": frozenset({"vector", "namespace_filter", "erase"}),
    "graph": frozenset({"graph", "namespace_filter", "erase"}),
    "lexical": frozenset({"lexical", "namespace_filter", "erase"}),
    "embedding": frozenset({"embedding"}),
    "llm": frozenset({"llm"}),
    "rerank": frozenset({"rerank"}),
    "extraction": frozenset({"extraction"}),
    "decider": frozenset({"decider"}),
}


def _validate_provider_result(
    descriptor: ProviderDescriptor, result: BuildResult[Any], context: Any
) -> None:
    """Check declared/effective capabilities and service ports before publishing a build.

    These are structural checks only; provider health probes and model calls run later.
    """
    category = descriptor.ref.category
    name = descriptor.ref.name
    identity = f"{category}.{name}"
    mandatory = _PROVIDER_BASE_CAPABILITIES[category]
    if not mandatory <= descriptor.declared_capabilities:
        missing = ", ".join(sorted(mandatory - descriptor.declared_capabilities))
        raise ConfigError(f"provider {identity} descriptor omits required capabilities: {missing}")
    if not mandatory <= result.effective_capabilities:
        missing = ", ".join(sorted(mandatory - result.effective_capabilities))
        raise ConfigError(f"provider {identity} build omits required capabilities: {missing}")
    if not result.effective_capabilities <= descriptor.declared_capabilities:
        extra = ", ".join(sorted(result.effective_capabilities - descriptor.declared_capabilities))
        raise ConfigError(f"provider {identity} build claims undeclared capabilities: {extra}")

    if category == "storage":
        if not isinstance(result, StorageBuildResult):
            raise ConfigError(f"provider {identity} did not expose the SQL access contract")
        access = result.sql
        if (
            not callable(getattr(access, "checkpoint", None))
            or not callable(getattr(access, "acquire_sync", None))
            or not callable(getattr(access, "sync_lease", None))
            or not isinstance(getattr(access, "capabilities", None), frozenset)
            or "sql" not in access.capabilities
            or not access.capabilities <= result.effective_capabilities
            or not isinstance(getattr(access, "is_memory", None), bool)
        ):
            raise ConfigError(f"provider {identity} exposed an incomplete SQL access contract")
        from sqlalchemy.ext.asyncio import AsyncEngine

        if not isinstance(getattr(access, "engine", None), AsyncEngine):
            raise ConfigError(f"provider {identity} exposed an invalid SQL engine")
        return

    ports: dict[str, type[Any]] = {
        "cache": KVCache,
        "vector": VectorStore,
        "graph": GraphStore,
        "lexical": LexicalStore,
        "embedding": EmbeddingService,
        "llm": LLMService,
        "rerank": Reranker,
        "extraction": ExtractionModels,
        "decider": object,
    }
    if not isinstance(result.service, ports[category]):
        raise ConfigError(f"provider {identity} service does not satisfy its port contract")

    if category == "vector" and isinstance(context, VectorBuildContext):
        requested: set[str] = set()
        if context.versioned_spaces:
            requested.add("versioned_spaces")
        if context.quantization is not None:
            requested.add("quantization")
        missing_capabilities = requested - result.effective_capabilities
        if missing_capabilities:
            names = ", ".join(sorted(missing_capabilities))
            raise ConfigError(
                f"provider {identity} does not support requested capabilities: {names}"
            )


# ── scopes ───────────────────────────────────────────────────────────────────────────────


class MemScope(BaseScope):
    """App > job > request. ``JOB`` is entered explicitly (``job_scope``); a plain
    ``container()`` from ``APP`` goes straight to ``REQUEST``."""

    RUNTIME = new_scope("RUNTIME", skip=True)
    APP = new_scope("APP")
    JOB = new_scope("JOB", skip=True)
    REQUEST = new_scope("REQUEST")


# ── context and value types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineSpec:
    """What the engine was constructed with (the inputs of the two-phase bootstrap, D-22).

    ``config`` short-circuits the bootstrap: a ready :class:`MemspineConfig` is used as is.
    """

    template: str | None = None
    user_config: str | Path | dict[str, Any] | None = None
    dotenv_path: str | Path | None = ".env"
    overrides: dict[str, Any] = field(default_factory=dict)
    config: MemspineConfig | None = None


async def _no_withheld() -> list[str]:
    return []


@dataclass(frozen=True)
class WiringHooks:
    """The engine callbacks the builders need (so ``runtime`` never imports the engine)."""

    #: builds a fresh :class:`PipelineContext` (JOB scope; Temporal activity context factory)
    pipeline_ctx: Callable[[], PipelineContext] | None = None
    #: texts of records above the consent tier, for the tier-gated remote LLM (#50)
    withheld_texts: Callable[[], Awaitable[list[str]]] = _no_withheld
    #: ``max_rounds -> ExtractEdges``, for the graph write pipeline (C3)
    edge_extract_callable: Callable[[int], ExtractEdges] | None = None


@dataclass(frozen=True)
class Capabilities:
    """The validated memory combination (C1b): enabled types and the ones the closure added."""

    enabled: set[str]
    auto_enabled: list[str]


@dataclass
class Wiring:
    """Engine-local generic provider lifecycle and build results."""

    resources: ProviderLifecycleManager
    health_timeout_s: float
    startup_timeout_s: float | None
    owner_id: str | None = None
    explicit_owner_id: bool = False
    projection_routes: Mapping[str, Any] = field(default_factory=dict)
    sql: SqlAccess | None = None
    provider_results: dict[str, BuildResult[Any]] = field(default_factory=dict)
    #: #43: the extraction cache, kept so erasure can purge an erased text's entry
    cached_extractor: CachedExtractor | None = None
    #: Optional engine-owned local GLiNER model pool. Created only when a GLiNER feature is on.
    gliner_pool: ExtractionModels | None = None
    #: E4: whether the vector leg runs the two-stage quantized rescore
    rescore_active: bool = False
    #: RM-78: the registry this Engine's builders share resources through (the process one,
    #: or a private one under ``runtime.shared_resources.scope: engine``).
    registry: ResourceRegistry = field(default_factory=shared_registry)
    #: RM-78: this Engine's retention policy token on ``registry`` (None when not registered)
    retention_token: int | None = None
    #: RM-78: True when ``registry`` belongs to this Engine alone
    private_registry: bool = False
    #: ADR-081: the provider ``graph.provider: auto`` is pinned to (read from the graph
    #: projector binding, or the first resolution). None before the preflight ran.
    graph_pin: str | None = None
    #: ADR-081: category -> (pinned, now resolved) when ``auto`` would resolve differently
    #: today; reported, never applied (an explicit provider change is required).
    provider_drift: dict[str, tuple[str, str]] = field(default_factory=dict)

    async def release_registry(self) -> None:
        """Withdraw this Engine's retention policy; a private registry also closes the weights
        it still retains (nothing else can reuse them). Idempotent."""
        token, self.retention_token = self.retention_token, None
        if token is not None:
            self.registry.remove_retention(token)
        if self.private_registry:
            await self.registry.evict_idle()

    async def close_clients(self) -> None:
        report = await self.resources.close_all()
        if report.failures:
            _log.error(
                "provider.lifecycle_close_failed",
                resources=[
                    {
                        "resource_id": failure.resource_id,
                        "error_type": failure.error_type,
                        "timed_out": failure.timed_out,
                    }
                    for failure in report.failures
                ],
            )
            raise ProviderResourceCleanupError(report)


@dataclass(frozen=True)
class SemanticExtractor:
    """The entity extraction provider for the semantic door (D-28); ``value`` None when off."""

    value: EntityExtractor | None


@dataclass(frozen=True)
class SemanticWritePipeline:
    """The C3 graph write pipeline for the semantic door; ``value`` None when off."""

    value: WritePipeline | None


@dataclass(frozen=True)
class DecisionState:
    """H24: ``off`` is True when gliner2 was missing under ``strict_services: false``."""

    off: bool


@dataclass(frozen=True)
class RequestInfo:
    """REQUEST-scope context: who is calling and for which namespace."""

    namespace: str = "default"
    viewer: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class RequestState:
    """One read or write call's state; a fresh one per REQUEST scope."""

    request_id: str
    info: RequestInfo


@dataclass(frozen=True)
class JobInfo:
    """JOB-scope context: which background job runs."""

    name: str = ""


@dataclass(frozen=True)
class JobState:
    """One background job's state: its derivation context, fresh per JOB scope."""

    info: JobInfo
    context: PipelineContext


# ── shared helpers (moved from the engine; the engine keeps one-line delegates) ──────────


def memory_policy(config: MemspineConfig, memory_type: str) -> dict[str, Any]:
    mem = config.memories.get(memory_type)
    return dict(mem.policies) if mem is not None else {}


def client_is_memory(config: MemspineConfig) -> bool:
    return storage_placement(config, _owner_id(config)).in_memory


def derived_base(config: MemspineConfig) -> str:
    """Base path for the file-backed projections that live OUTSIDE the SQL
    database (LanceDB vectors, Tantivy lexical). For ``sqlite`` it is the db
    path (``<path>.lance`` etc.); for ``postgres`` it is ``deployment.data_dir/memspine``
    (ADR-081). ``:memory:`` for an in-memory records store; a ``ConfigError`` naming
    ``deployment.data_dir`` when the records store is not a file and none is set."""
    placement = storage_placement(config, _owner_id(config))
    if placement.in_memory:
        return ":memory:"
    return str(host_base_path(placement, "records", config.storage.records.provider))


def lexical_variant(config: MemspineConfig) -> str:
    """N58 / N30: the lexical index variant ("" = the default index): the analyzer
    when not ``default``, and ``dates`` with ``read.lexical_dates``."""
    settings = _settings(
        _descriptor(
            PROVIDER_LEXICAL_INDEXES, config.storage.lexical.provider, "storage.lexical.provider"
        ),
        config.storage.lexical.settings,
    )
    analyzer = getattr(settings, "analyzer", "default")
    date_tokens = getattr(settings, "date_tokens", False)
    parts: list[str] = [] if analyzer == "default" else [analyzer]
    if date_tokens:
        parts.append("dates")
    return "+".join(parts)


def lexical_projector_name(config: MemspineConfig) -> str:
    """N58: ``lexical`` for the default analyzer, ``lexical:<analyzer>`` otherwise,
    so a new analyzer's index starts from offset 0 and is rebuilt from the log."""
    variant = lexical_variant(config)
    return f"lexical:{variant}" if variant else "lexical"


def _as_options_dict(raw: Any) -> dict[str, object] | None:
    """Policy sub-blocks arrive as plain config dicts; anything else is a typo
    the policy's own extra='forbid' validation will surface."""
    return dict(raw) if isinstance(raw, dict) else None


def rescore_settings(
    config: MemspineConfig, embedder: EmbeddingService
) -> tuple[str | None, int | None]:
    """E4 (ADR-020): resolve the effective (quantization, matryoshka_dim)
    from the embedder manifest + the ``vector.quantization`` override.

    ``auto`` reads the manifest (default embedders declare none → the exact
    float32 path); ``none`` forces off; ``int8``/``binary`` force a scheme.
    Matryoshka truncation is manifest-only and uses the smallest declared
    prefix dim (max prefilter savings; the rescore restores full precision).
    """
    manifest = embedder.manifest
    override = config.storage.vector.settings.get("quantization", "auto")
    if override == "none":
        quantization = None
    elif override in ("int8", "binary"):
        quantization = override
    elif override == "auto":
        quantization = manifest.quantization
    else:
        raise ConfigError(
            f"unknown vector.quantization {override!r} (valid: auto, none, int8, binary)"
        )
    matryoshka_dim = min(manifest.matryoshka_dims) if manifest.matryoshka_dims else None
    return quantization, matryoshka_dim


# ── APP providers: bootstrap ─────────────────────────────────────────────────────────────


def build_secrets(spec: EngineSpec) -> SecretsService:
    """Bootstrap-phase secrets resolver (D-22). Selected by the env var
    ``MEMSPINE_SECRETS_BACKEND`` — NOT config, because secrets resolve
    *before* ``MemspineConfig`` exists (the chicken-and-egg the two-phase
    bootstrap avoids). ``env`` (default) is the zero-cloud ``EnvSecrets``;
    ``aws`` chains env/.env first, then AWS Secrets Manager, so local dev
    never needs credentials and a locally-set value always wins."""
    backend = os.environ.get("MEMSPINE_SECRETS_BACKEND", "env").strip().lower()
    env_secrets = EnvSecrets(dotenv_path=spec.dotenv_path)
    if backend == "env":
        return env_secrets
    if backend == "aws":
        from memspine.clients.aws import AwsConnector
        from memspine.services.secrets.aws import AwsSecrets, secrets_manager_spec

        # Bootstrap phase 1 (no event loop yet): open synchronously. Creating the client
        # opens no connection; it lives as long as the resolver (the process).
        connector = AwsConnector(secrets_manager_spec())
        connector.open_blocking()
        return ChainedSecrets(env_secrets, AwsSecrets(client=connector.handle))
    raise ConfigError(f"unknown MEMSPINE_SECRETS_BACKEND {backend!r} (valid: env, aws)")


def resolve_config(spec: EngineSpec, secrets: SecretsService) -> ResolvedConfig:
    if spec.config is not None:
        try:
            config = freeze_effective_config(spec.config)
        except ConfigInputError as exc:
            raise ConfigError(f"invalid configuration: {exc}") from None
        return ResolvedConfig(config=config)
    resolved = load_config(
        template=default_template(spec.template, spec.user_config, spec.overrides),
        user_config=spec.user_config,
        env=os.environ,
        overrides=spec.overrides,
        secret_resolver=secrets.get,
    )
    try:
        config = freeze_effective_config(resolved.config)
    except ConfigInputError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from None
    return ResolvedConfig(config=config, sources=dict(resolved.sources))


def config_of(resolved: ResolvedConfig) -> MemspineConfig:
    return resolved.config


#: Services the Phase-0 engine always constructs (core install, D-03).
_CORE_SERVICES = frozenset({"storage", "secrets"})


def check_capabilities(config: MemspineConfig) -> Capabilities:
    """Validate the combination (C1b) + required services (D-10), then enable the pinned
    plug-ins (MOD-02; empty ``plugins:`` = nothing happens)."""
    enabled, auto_enabled = dependency_closure(config.enabled_memories())
    gaps = missing_services(enabled, set(_CORE_SERVICES))
    if gaps:
        mem_type, services = next(iter(sorted(gaps.items())))
        service = sorted(services)[0]
        if config.strict_services:
            raise MissingServiceError(service, SERVICE_EXTRAS.get(service))
        _log.warning(
            "service.missing_ignored",
            detail="strict_services=false: engine starts degraded (D-10)",
            gaps={k: sorted(v) for k, v in gaps.items()},
            first_missing=f"{mem_type} needs {service}",
        )
    enable_plugins(config.plugins)
    return Capabilities(enabled=enabled, auto_enabled=auto_enabled)


def retention_policy(config: MemspineConfig) -> RetentionPolicy:
    """RM-78: the weight retention this config asks of its resource registry."""
    shared = config.runtime.shared_resources
    return RetentionPolicy(
        keep_warm=shared.keep_warm,
        idle_ttl_s=shared.idle_ttl_s,
        memory_budget_mb=shared.memory_budget_mb,
        max_models_per_category=shared.max_models_per_category,
    )


async def wiring_of(config: MemspineConfig, _caps: Capabilities) -> AsyncIterator[Wiring]:
    timeouts = config.runtime.provider_services
    private = config.runtime.shared_resources.scope == "engine"
    registry = ResourceRegistry() if private else shared_registry()
    wiring = Wiring(
        resources=ProviderLifecycleManager(close_timeout_s=timeouts.close_timeout_s),
        health_timeout_s=timeouts.health_timeout_s,
        startup_timeout_s=timeouts.startup_timeout_s,
        owner_id=config.deployment.id,
        explicit_owner_id=config.deployment.id is not None,
        registry=registry,
        retention_token=registry.add_retention(retention_policy(config)),
        private_registry=private,
    )
    gliner_active = (
        config.decision.provider == "gliner2"
        or memory_policy(config, "semantic").get("entity_extraction", "off") == "gliner"
        or config.write.prepass == "gliner"
        or "gliner_sensitivity" in config.models.bindings  # RM-99 opt-in
        or "gliner_topic" in config.models.bindings  # RM-106 opt-in
    )
    try:
        if gliner_active:
            # RM-113: the model.extraction descriptor builds the pool and registers its close.
            descriptor = _descriptor(PROVIDER_EXTRACTORS, "gliner2", "models.gliner_max_models")
            settings = _settings(descriptor, {"max_models": config.models.gliner_max_models})
            built = await _build_provider(
                wiring,
                descriptor,
                settings,
                ExtractionBuildContext(shared_build_context(config, wiring)),
                result_key="extraction.gliner2",
            )
            wiring.gliner_pool = cast(ExtractionModels, built.service)
        yield wiring
    finally:
        try:
            await wiring.close_clients()
        finally:
            await wiring.release_registry()


def shared_build_context(config: MemspineConfig, wiring: Wiring) -> SharedBuildContext:
    """The build context every provider builder of this Engine receives (RM-78: its resource
    registry and the ``runtime.shared_resources`` pool options)."""
    shared = config.runtime.shared_resources
    return SharedBuildContext(
        wiring.resources,
        _owner_id(config, wiring),
        wiring.health_timeout_s,
        registry=wiring.registry,
        sql_pool=shared.sql,
        http_pool=shared.http,
    )


# ── APP providers: services ──────────────────────────────────────────────────────────────


async def build_storage(config: MemspineConfig, wiring: Wiring) -> SqlStorage:
    descriptor = _descriptor(
        PROVIDER_STORAGE_BACKENDS, config.storage.records.provider, "storage.records.provider"
    )
    settings = _settings(descriptor, config.storage.records.settings)
    shared = shared_build_context(config, wiring)
    result = await _build_provider(
        wiring,
        descriptor,
        settings,
        StorageBuildContext(shared, config.event_log),
    )
    if not isinstance(result, StorageBuildResult):
        raise ConfigError(
            f"storage provider {config.storage.records.provider!r} did not expose SQL access"
        )
    wiring.sql = result.sql  # Engine receives only this capability, never the native client.
    return cast(SqlStorage, result.service)


async def build_cache(config: MemspineConfig, wiring: Wiring) -> KVCache:
    descriptor = _descriptor(
        PROVIDER_CACHE_BACKENDS, config.storage.cache.provider, "storage.cache.provider"
    )
    settings = _settings(descriptor, config.storage.cache.settings)
    placement = _storage_placement(config, wiring)
    shared = shared_build_context(config, wiring)
    result = await _build_provider(
        wiring,
        descriptor,
        settings,
        CacheBuildContext(shared, placement, data_dir=config.deployment.data_dir),
    )
    return cast(KVCache, result.service)


def resolved_embedding(config: MemspineConfig) -> Any:
    """MD-02: ``config.embedding`` with ``embedding.profile`` applied (None: unchanged)."""
    cfg = config.embedding
    if cfg.profile is None:
        return cfg
    from memspine.models.embedding_profiles import resolve_profile
    from memspine.observability import tap

    # Provider-native options moved into ``settings``.  In particular, a profile
    # can be selected while the configured provider is still the default
    # fastembed provider, so read an explicit device from the raw settings map
    # rather than looking for a legacy top-level attribute.
    configured_device = cfg.settings.get("device")
    profile = resolve_profile(
        cfg.profile,
        device=configured_device if isinstance(configured_device, str) else None,
    )
    tap.event(
        "embed.profile",
        profile=profile.name,
        model=profile.model_id,
        dim=profile.dim,
        dtype=profile.dtype,
        version_hash=profile.version_hash,
    )
    return profile.apply(cfg)


def _pin_embedding_profile(
    config: MemspineConfig,
    embedder: EmbeddingService,
    *,
    generation_id: str = "active",
) -> Any:
    """MD-02: pin the store's embedding profile at creation; refuse a different one on open."""
    cfg = config.embedding
    if not cfg.pin_profile:
        return None
    from memspine.models.embedding_profiles import (
        EmbeddingProfile,
        EmbeddingProfileMismatch,
        check_or_pin,
        resolve_profile,
    )
    from memspine.observability import tap

    if cfg.migrate_profile and not cfg.versioned_spaces:
        raise ConfigError(
            "embedding.migrate_profile needs embedding.versioned_spaces=true: the new profile's "
            "vectors go to a new space and the F-03 re-embed moves the old records"
        )
    effective = resolved_embedding(config)
    provider_settings = _settings(
        _descriptor(PROVIDER_EMBEDDERS, effective.provider, "embedding.provider"),
        effective.settings,
    )
    if cfg.profile is not None:
        profile = resolve_profile(cfg.profile, device=getattr(provider_settings, "device", None))
    else:
        eff = resolved_embedding(config)
        profile = EmbeddingProfile(
            name="config",
            provider=eff.provider,
            model_id=getattr(provider_settings, "model", embedder.embedder_id),
            dim=embedder.dim,
            dtype=getattr(provider_settings, "dtype", None) or "float32",
            query_instruction=getattr(provider_settings, "query_instruction", None),
        )
    generation_suffix = (
        "" if generation_id == "active" else f".generation-{generation_component(generation_id)}"
    )
    path = (
        None
        if client_is_memory(config)
        else f"{derived_base(config)}.embedding-profile{generation_suffix}.json"
    )
    try:
        status = check_or_pin(path, profile, migrate=cfg.migrate_profile)
    except EmbeddingProfileMismatch:
        tap.event("embed.profile_mismatch", profile=profile.name, version_hash=profile.version_hash)
        raise
    if status == "migrated":
        tap.event("embed.profile_mismatch", profile=profile.name, migrated=True)
    return profile


async def provide_embedder(
    config: MemspineConfig, cache: KVCache, _caps: Capabilities, wiring: Wiring
) -> EmbeddingService:
    """The E3-cached embedder (the cache is the one shared KV cache, Phase 2)."""
    effective = resolved_embedding(config)
    descriptor = _descriptor(PROVIDER_EMBEDDERS, effective.provider, "embedding.provider")
    settings = _settings(descriptor, effective.settings)
    shared = shared_build_context(config, wiring)
    result = await _build_provider(wiring, descriptor, settings, shared, result_key="embedding")
    return CachedEmbedding(cast(EmbeddingService, result.service), cache)


async def build_vector_store(
    config: MemspineConfig, embedder: EmbeddingService, wiring: Wiring
) -> VectorStore:
    route = wiring.projection_routes.get("vectors")
    generation_id = getattr(route, "physical_generation_id", "active")
    pinned = _pin_embedding_profile(
        config, embedder, generation_id=generation_id
    )  # MD-02 (flag off: returns at once)
    quantization, matryoshka_dim = rescore_settings(config, embedder)
    wiring.rescore_active = quantization is not None or matryoshka_dim is not None
    descriptor = _descriptor(
        PROVIDER_VECTOR_STORES, config.storage.vector.provider, "storage.vector.provider"
    )
    if route is not None and descriptor.ref.name != route.provider:
        raise ConfigError("storage.vector.provider does not match its verified switch target")
    settings = _settings(descriptor, config.storage.vector.settings)
    placement = _storage_placement(config, wiring)
    effective_embedding = resolved_embedding(config)
    effective_embedding_settings = _settings(
        _descriptor(PROVIDER_EMBEDDERS, effective_embedding.provider, "embedding.provider"),
        effective_embedding.settings,
    )
    context = VectorBuildContext(
        shared_build_context(config, wiring),
        placement,
        embedder,
        quantization,
        matryoshka_dim,
        None if pinned is None else pinned.to_dict(),
        config.embedding.versioned_spaces,
        getattr(effective_embedding_settings, "query_instruction", None),
        generation_id,
        sql=wiring.sql,
    )
    result = await _build_provider(wiring, descriptor, settings, context, result_key="vector")
    return cast(VectorStore, result.service)


async def build_lexical_store(
    config: MemspineConfig, _caps: Capabilities, wiring: Wiring
) -> LexicalStore:
    descriptor = _descriptor(
        PROVIDER_LEXICAL_INDEXES, config.storage.lexical.provider, "storage.lexical.provider"
    )
    route = wiring.projection_routes.get("lexical")
    if route is not None and descriptor.ref.name != route.provider:
        raise ConfigError("storage.lexical.provider does not match its verified switch target")
    settings = _settings(descriptor, config.storage.lexical.settings)
    context = LexicalBuildContext(
        shared_build_context(config, wiring),
        _storage_placement(config, wiring),
        getattr(route, "physical_generation_id", "active"),
    )
    result = await _build_provider(wiring, descriptor, settings, context, result_key="lexical")
    return cast(LexicalStore, result.service)


_GRAPH_REQUIRED = frozenset({"graph", "namespace_filter", "erase"})


def _graph_auto_descriptor(config: MemspineConfig, wiring: Wiring) -> ProviderDescriptor:
    """What ``graph.provider`` resolves to right now (``auto``: ladybug when installed,
    else ``sql_adjacency`` on a SQLite or Postgres records store)."""
    available_capabilities = wiring.sql.capabilities if wiring.sql is not None else frozenset()
    eligible_names = frozenset({"ladybug"})
    if {"sql.sqlite", "sql.postgres"} & available_capabilities:
        eligible_names |= {"sql_adjacency"}
    selection = select_provider(
        "graph",
        config.storage.graph.provider,
        required_capabilities=_GRAPH_REQUIRED,
        preferred_names=("ladybug", "sql_adjacency"),
        eligible_names=eligible_names,
        available_capabilities=available_capabilities,
    )
    return selection.descriptor


def _graph_descriptor(config: MemspineConfig, wiring: Wiring) -> ProviderDescriptor:
    """The graph provider to build. ``auto`` is resolved once and pinned (ADR-081): with a
    pin (``wiring.graph_pin``, from the graph projector binding) the pinned provider is
    selected explicitly, and a different resolution today is recorded in
    ``wiring.provider_drift`` and logged as a required provider change, never applied."""
    pinned = wiring.graph_pin if config.graph.provider == "auto" else None
    if pinned is None:
        return _graph_auto_descriptor(config, wiring)
    available_capabilities = wiring.sql.capabilities if wiring.sql is not None else frozenset()
    descriptor = select_provider(
        "graph",
        pinned,
        required_capabilities=_GRAPH_REQUIRED,
        available_capabilities=available_capabilities,
    ).descriptor
    try:
        resolved = _graph_auto_descriptor(config, wiring).ref.name
    except ProviderSelectionError:
        resolved = pinned
    if resolved != pinned and wiring.provider_drift.get("graph") != (pinned, resolved):
        wiring.provider_drift["graph"] = (pinned, resolved)
        _log.warning(
            "provider.auto_pin_drift",
            category="graph",
            pinned=pinned,
            resolved=resolved,
            action="graph.provider auto stays pinned; switching needs an explicit provider change",
        )
    return descriptor


async def resolve_graph_pin(config: MemspineConfig, wiring: Wiring) -> str | None:
    """ADR-081: pin ``graph.provider: auto`` to the provider recorded in the graph projector
    binding of this deployment, or to today's resolution when there is no binding yet (the
    binding preflight then records it). A binding written before the ``sql_adjacency``
    rename is adopted under the new name. Returns the pin (None when not ``auto``)."""
    from memspine.providers.projection_bindings import SqlProjectionBindingStore

    if config.graph.provider != "auto":
        return None
    if wiring.sql is None or wiring.owner_id is None:
        raise ConfigError("graph auto pinning requires started SQL and deployment identity")
    bound = await SqlProjectionBindingStore(wiring.sql.engine).bound_provider(
        wiring.owner_id, "graph"
    )
    if bound is None:
        wiring.graph_pin = _graph_auto_descriptor(config, wiring).ref.name
    else:
        wiring.graph_pin = RENAMED_PROVIDERS["graph_stores"].get(bound, bound)
    return wiring.graph_pin


async def preflight_projection_bindings(
    config: MemspineConfig,
    wiring: Wiring,
    enabled: Collection[str],
) -> None:
    """Reject active projection target drift before constructing its providers."""
    from memspine.providers.projection_bindings import (
        ProjectionTargetBinding,
        SqlProjectionBindingStore,
        projection_target_fingerprint,
    )

    if wiring.sql is None or wiring.owner_id is None:
        raise ConfigError(
            "projection target preflight requires started SQL and deployment identity"
        )

    candidates: list[tuple[str, str, ProviderDescriptor, Any]] = []
    vector_descriptor = _descriptor(
        PROVIDER_VECTOR_STORES, config.storage.vector.provider, "storage.vector.provider"
    )
    candidates.append(("vectors", "vector", vector_descriptor, config.storage.vector.settings))
    if config.read.hybrid:
        lexical_descriptor = _descriptor(
            PROVIDER_LEXICAL_INDEXES,
            config.storage.lexical.provider,
            "storage.lexical.provider",
        )
        candidates.append(
            (
                lexical_projector_name(config),
                "lexical",
                lexical_descriptor,
                config.storage.lexical.settings,
            )
        )
    if "associative" in enabled:
        await resolve_graph_pin(config, wiring)  # ADR-081: auto resolves once, then pinned
        graph_descriptor = _graph_descriptor(config, wiring)
        candidates.append(("graph", "graph", graph_descriptor, config.storage.graph.settings))
        mirror_slot = graph_mirror_slot(config)
        if mirror_slot is not None:
            mirror_descriptor = _descriptor(
                PROVIDER_GRAPH_STORES,
                mirror_slot.provider,
                "storage.graph_mirror.provider",
            )
            candidates.append(("graph_neo4j", "graph", mirror_descriptor, mirror_slot.settings))

    bindings: list[Any] = []
    for projector_name, category, descriptor, raw_settings in candidates:
        settings = _settings(descriptor, raw_settings)
        fingerprint = projection_target_fingerprint(
            config,
            category=category,
            provider=descriptor.ref,
            settings=settings,
            deployment_id=wiring.owner_id,
        )
        route = wiring.projection_routes.get(projector_name)
        if route is not None:
            if route.provider != descriptor.ref.name:
                raise ConfigError(
                    f"projector {projector_name!r} selected provider does not match its verified switch target"
                )
            if route.target_fingerprint != fingerprint:
                raise ConfigError(
                    f"projector {projector_name!r} settings do not match the verified switch target fingerprint"
                )
        bindings.append(
            ProjectionTargetBinding(
                deployment_id=wiring.owner_id,
                projector_name=projector_name,
                provider=(
                    f"{descriptor.ref.category}:{descriptor.ref.name}:v{descriptor.ref.contract_version}"
                ),
                target_fingerprint=fingerprint,
                generation_id="active" if route is None else route.generation_id,
                source_provider=None if route is None else route.source_provider,
                source_target_fingerprint=(
                    None if route is None else route.source_target_fingerprint
                ),
                source_generation_id=None if route is None else route.source_generation_id,
                verified_checkpoint_seq=None if route is None else route.target_checkpoint_seq,
            )
        )

    binding_store = SqlProjectionBindingStore(wiring.sql.engine)
    for binding in bindings:
        if binding.projector_name == "graph" and binding.provider.startswith(
            "graph:sql_adjacency:"
        ):
            # ADR-081: a binding recorded under the old name is the same target (the graph
            # tables of the records database); adopt it under the new name, once.
            await binding_store.adopt_renamed_provider(
                binding, "graph:sqlite_adjacency:" + binding.provider.rsplit(":", 1)[1]
            )
        await binding_store.ensure(binding)


async def build_graph_store(config: MemspineConfig, wiring: Wiring) -> GraphStore:
    descriptor = _graph_descriptor(config, wiring)
    mirror_slot = config.storage.graph_mirror
    if mirror_slot is not None:
        from memspine.providers.graph_targets import require_distinct_graph_targets

        mirror_descriptor = _descriptor(
            PROVIDER_GRAPH_STORES,
            mirror_slot.provider,
            "storage.graph_mirror.provider",
        )
        mirror_settings = _settings(mirror_descriptor, mirror_slot.settings)
        primary_settings = _settings(descriptor, config.storage.graph.settings)
        require_distinct_graph_targets(
            descriptor, primary_settings, mirror_descriptor, mirror_settings
        )
    route = wiring.projection_routes.get("graph")
    if route is not None and descriptor.ref.name != route.provider:
        raise ConfigError("storage.graph.provider does not match its verified switch target")
    if route is not None and descriptor.ref.name == "sql_adjacency":
        raise ConfigError(
            "controlled graph switching is unsupported for sql_adjacency because it shares the authoritative SQL database"
        )
    settings = _settings(descriptor, config.storage.graph.settings)
    context = GraphBuildContext(
        shared_build_context(config, wiring),
        _storage_placement(config, wiring),
        wiring.sql,
        getattr(route, "physical_generation_id", "active"),
    )
    result = await _build_provider(
        wiring, descriptor, settings, context, result_key="graph.primary"
    )
    return cast(GraphStore, result.service)


async def build_llm_router(config: MemspineConfig, hooks: WiringHooks, wiring: Wiring) -> LLMRouter:
    """Build inline, named-profile, tiered and escalation LLM bindings."""
    providers: dict[str, LLMService] = {}
    role_configs = bound_llm_roles(config)
    profile_services: dict[str, LLMService] = {}
    shared = shared_build_context(config, wiring)

    async def build_profile(profile_name: str, role: str) -> LLMService:
        existing = profile_services.get(profile_name)
        if existing is not None:
            return existing
        profile_config = resolve_llm_profile(config, profile_name)
        descriptor = _descriptor(
            PROVIDER_LLM_PROVIDERS,
            profile_config.provider,
            f"models.profiles.{profile_name}.provider",
        )
        typed = _settings(descriptor, profile_config.settings)
        built = await _build_provider(
            wiring,
            descriptor,
            typed,
            LLMBuildContext(shared, role, resource_key=f"llm.profile:{profile_name}"),
            result_key=f"llm.profile.{profile_name}",
        )
        raw_service = cast(LLMService, built.service)
        service = ProfiledLLM(
            raw_service,
            profiled_provider_identity(descriptor.ref, typed, raw_service),
        )
        profile_services[profile_name] = service
        return service

    def native_token_cap(role_config: Any) -> int | None:
        value = role_config.settings.get("max_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    for role, role_config in role_configs.items():
        profile_name = config.models.bindings.get(role)
        if profile_name is not None and config.models.profiles[profile_name].kind == "llm":
            providers[role] = await build_profile(profile_name, role)
            continue
        descriptor = _descriptor(
            PROVIDER_LLM_PROVIDERS, role_config.provider, f"llm.roles.{role}.provider"
        )
        typed = _settings(descriptor, role_config.settings)
        built = await _build_provider(
            wiring, descriptor, typed, LLMBuildContext(shared, role), result_key=f"llm.{role}"
        )
        providers[role] = cast(LLMService, built.service)
    resolved = None
    if config.models.tiers_v2:
        resolved = await _bind_tiered_roles(config, providers, role_configs, wiring)
    consent = config.consent
    if consent.remote_llm_max_tier is not None:
        # Gate each candidate independently: a local primary may target a remote profile.
        for role, role_config in role_configs.items():
            if not is_local_binding(
                role_config.provider, role_config.settings, consent.local_hosts
            ):
                providers[role] = TierGatedLLM(providers[role], hooks.withheld_texts)
        for role, policy in config.models.escalation.items():
            target_names = policy.targets[: max(0, policy.max_attempts - 1)]
            profile_names = (config.models.bindings[role], *target_names)
            candidates: list[LLMService] = [providers[role]]
            profile_caps = [native_token_cap(role_configs[role])]
            for profile_name in target_names:
                candidates.append(await build_profile(profile_name, role))
                profile_caps.append(native_token_cap(resolve_llm_profile(config, profile_name)))
            gated = []
            for candidate, profile_name in zip(candidates[1:], target_names, strict=True):
                target_config = resolve_llm_profile(config, profile_name)
                if not is_local_binding(
                    target_config.provider, target_config.settings, consent.local_hosts
                ):
                    candidate = TierGatedLLM(candidate, hooks.withheld_texts)
                gated.append(candidate)
            providers[role] = EscalatingLLM(
                role, profile_names, (candidates[0], *gated), policy, tuple(profile_caps)
            )
    else:
        for role, policy in config.models.escalation.items():
            target_names = policy.targets[: max(0, policy.max_attempts - 1)]
            profile_names = (config.models.bindings[role], *target_names)
            candidates = [providers[role]]
            profile_caps = [native_token_cap(role_configs[role])]
            for profile_name in target_names:
                candidates.append(await build_profile(profile_name, role))
                profile_caps.append(native_token_cap(resolve_llm_profile(config, profile_name)))
            providers[role] = EscalatingLLM(
                role, profile_names, tuple(candidates), policy, tuple(profile_caps)
            )
    router = LLMRouter(
        providers,
        structured_options=_structured.StructuredOptions(**config.llm.structured.model_dump()),
    )
    if resolved is not None:
        from memspine.models.resolve import report
        from memspine.prompts.roles import CATALOGUE_ROLE

        explicit_profiles = {
            role: profile_name
            for role, profile_name in config.models.bindings.items()
            if role in role_configs and config.models.profiles[profile_name].kind == "llm"
        }
        model_plan = report(resolved)
        profiles_by_catalogue: dict[str, dict[str, str]] = {}
        for role, profile_name in explicit_profiles.items():
            catalogue_role = CATALOGUE_ROLE.get(role)
            if catalogue_role is not None:
                profiles_by_catalogue.setdefault(catalogue_role, {})[role] = profile_name
        for role_plan in model_plan["roles"]:
            bindings = profiles_by_catalogue.get(role_plan["role"])
            if not bindings:
                continue
            profiles = set(bindings.values())
            role_plan.update(
                status="explicit_multiple" if len(profiles) > 1 else "explicit",
                model=(
                    "multiple profiles"
                    if len(profiles) > 1
                    else str(
                        config.models.profiles[next(iter(profiles))].settings.get(
                            "model", next(iter(profiles))
                        )
                    )
                ),
                device="",
                fallback="",
                profile_bindings=dict(sorted(bindings.items())),
            )
            if len(profiles) == 1:
                role_plan["profile"] = next(iter(profiles))
        router.model_plan = {
            **model_plan,
            "explicit_profiles": explicit_profiles,
        }
    return router


async def _bind_tiered_roles(
    config: MemspineConfig,
    providers: dict[str, LLMService],
    role_configs: dict[str, Any],
    wiring: Wiring,
) -> Any:
    """MD-01 (``models.tiers_v2``): bind every engine role no ``llm.roles`` entry covers through the
    tier plan. A role the plan cannot serve gets no provider, so its feature takes the role's
    fallback (L1/L3/summaries off, rules, calibrated gate) and makes zero model calls. Explicit
    ``llm.roles`` entries win and are left exactly as built above."""
    from memspine.models.resolve import (
        emit_taps,
        resolve_models,
        role_binding,
        role_config,
    )
    from memspine.models.tiers import ROLE_CATALOGUE
    from memspine.observability import tap
    from memspine.prompts.roles import CATALOGUE_ROLE
    from memspine.services.llm.base import ThinkingCapped

    explicit_roles = set(role_configs)
    explicit_catalogue_roles = {
        CATALOGUE_ROLE[role] for role in explicit_roles if role in CATALOGUE_ROLE
    }
    extra = ("extract",) if config.write.window_extract != "off" else ()
    models_config = config.models.model_copy(
        update={
            "required_roles": tuple(
                role
                for role in config.models.required_roles
                if role not in explicit_catalogue_roles
            )
        }
    )
    resolved = resolve_models(
        models_config,
        extra_required=tuple(role for role in extra if role not in explicit_catalogue_roles),
    )
    resolved.check()
    spec_tokens = {r.name: r.max_tokens for r in ROLE_CATALOGUE}
    for role in sorted(CATALOGUE_ROLE):
        if role in explicit_roles:
            source = "models.profiles" if role in config.models.bindings else "llm.roles"
            tap.event("models.resolve", role=role, status="explicit", source=source)
            continue
        rc = role_config(resolved, role)
        if rc is None:
            continue
        descriptor = _descriptor(
            PROVIDER_LLM_PROVIDERS, rc.provider, f"models.roles.{role}.provider"
        )
        typed = _settings(descriptor, rc.settings)
        built = await _build_provider(
            wiring,
            descriptor,
            typed,
            LLMBuildContext(
                shared_build_context(config, wiring),
                role,
            ),
            result_key=f"llm.{role}",
        )
        provider = cast(LLMService, built.service)
        b = role_binding(resolved, role)
        if b is not None and b.thinking.mode == "capped":
            provider = ThinkingCapped(
                provider,
                cap_tokens=b.thinking.cap_tokens,
                max_tokens=spec_tokens[CATALOGUE_ROLE[role]],
            )
        providers[role] = provider
        role_configs[role] = rc
    emit_taps(resolved, tuple(r for r in sorted(CATALOGUE_ROLE) if r not in explicit_roles))
    return resolved


async def provide_llm_router(
    config: MemspineConfig, hooks: WiringHooks, _caps: Capabilities, wiring: Wiring
) -> LLMRouter:
    return await build_llm_router(config, hooks, wiring)


def check_decision_provider(config: MemspineConfig) -> DecisionState:
    """H24, at ``start()``: a configured gliner2 provider must be importable (D-10).

    Missing gliner2 raises ``MissingServiceError(extra="ner")``; with
    ``strict_services: false`` it logs once and the provider stays off.
    """
    if config.decision.provider == "off":
        return DecisionState(off=False)
    from memspine.services.decider.gliner2 import auto_extractor_class

    try:
        auto_extractor_class()
    except MissingServiceError:
        if config.strict_services:
            raise
        _log.warning(
            "service.missing_ignored",
            detail="strict_services=false: decision provider off, read planner uses rules",
            service="gliner2 decision provider",
            extra="ner",
        )
        return DecisionState(off=True)
    return DecisionState(off=False)


def build_prompts(config: MemspineConfig) -> PromptRegistry:
    # Prompt pack resolution (§4 step 2, D-43): defaults + config overrides.
    return PromptRegistry(
        overrides=config.prompts.overrides,
        partials=config.prompts.partials,
        selection=config.prompts.selection,
        layout_v2=config.prompts.layout_v2,
        layout_partials=config.prompts.layout_partials,
    )


def build_extractor(
    config: MemspineConfig,
    llm: LLMRouter,
    prompts: PromptRegistry,
    cache: KVCache,
    wiring: Wiring,
) -> SemanticExtractor:
    """Entity extraction provider (D-28): off (default) | llm | gliner."""
    mode = str(memory_policy(config, "semantic").get("entity_extraction", "off"))
    if mode == "off":
        return SemanticExtractor(None)
    if mode == "llm":
        # E3 extraction cache: keyed by (prompt version x content hash), so
        # a prompt upgrade cleanly invalidates (N7).
        wiring.cached_extractor = CachedExtractor(
            LLMEntityExtractor(llm.for_role("extract"), prompts.for_role("extract")),
            cache,
        )
        return SemanticExtractor(wiring.cached_extractor)
    if mode == "gliner":
        from memspine.memories.semantic.entities import GlinerEntityExtractor
        from memspine.models.profiles import resolve_gliner_settings

        if wiring.gliner_pool is None:
            raise ConfigError("semantic GLiNER extraction needs the engine GLiNER pool")
        settings = resolve_gliner_settings(config, "gliner_semantic")
        return SemanticExtractor(GlinerEntityExtractor(wiring.gliner_pool, settings))
    raise ConfigError(
        f"unknown memories.semantic.policies.entity_extraction {mode!r} (valid: off, llm, gliner)"
    )


def build_write_pipeline(
    config: MemspineConfig, llm: LLMRouter, hooks: WiringHooks
) -> SemanticWritePipeline:
    """C3 synchronous graphiti write pipeline for the semantic door.

    Active only when ``memories.semantic.policies.write_pipeline == "graph"``
    AND an ``extract_edges`` role is bound — otherwise None (single-pass,
    byte-identical to v0.1). Edges are written through the M4/M5 ladder, so
    edge invalidation reuses the conflict machinery (ADR-026)."""
    sem = memory_policy(config, "semantic")
    if sem.get("write_pipeline", "single") != "graph":
        return SemanticWritePipeline(None)
    if "extract_edges" not in llm.roles:
        return SemanticWritePipeline(None)
    if hooks.edge_extract_callable is None:
        raise ConfigError("the graph write pipeline needs the edge_extract_callable wiring hook")
    return SemanticWritePipeline(
        GraphWritePipeline(
            hooks.edge_extract_callable(extraction_rounds(sem)),
            protected_keys=config.firewall.protected_keys,
            cardinality=cardinality_map(
                (_as_options_dict(sem.get("extract_graph")) or {}).get("cardinality")
            ),
        )
    )


def build_runner(
    config: MemspineConfig, hooks: WiringHooks, _caps: Capabilities, wiring: Wiring
) -> TaskRunner:
    if config.workers.runner == "inline":
        return InlineRunner()
    if config.workers.runner == "temporal":
        settings = config.workers.temporal
        if settings is None:
            raise ConfigError(
                "workers.temporal.deployment_id is required when workers.runner='temporal'"
            )
        if hooks.pipeline_ctx is None:
            raise ConfigError("workers.runner='temporal' needs the pipeline_ctx wiring hook")
        from memspine.workers.temporal_runner import TemporalRunner

        queue = settings.task_queue or f"memspine-pipelines-v1-{settings.deployment_id}"
        resonate = config.workers.resonate
        return TemporalRunner(
            deployment_id=settings.deployment_id,
            address=settings.address,
            namespace=settings.namespace,
            task_queue=queue,
            tls=settings.tls,
            api_key=settings.api_key.get_secret_value() if settings.api_key is not None else None,
            journal_path=resonate.journal_path,
            context_factory=hooks.pipeline_ctx,
            rpc_timeout_s=settings.rpc_timeout_s,
            activity_timeout_s=settings.activity_timeout_s,
            workflow_timeout_s=settings.workflow_timeout_s,
            max_concurrent_activities=settings.max_concurrent_activities,
            retry_max_attempts=settings.retry_max_attempts,
            max_result_bytes=settings.max_result_bytes,
            resonate_task_timeout_s=resonate.task_timeout_s,
            step_timeout_s=resonate.step_timeout_s,
            step_lease_s=resonate.step_lease_s,
            journal_busy_timeout_s=resonate.journal_busy_timeout_s,
            resonate_max_concurrent_tasks=resonate.max_concurrent_tasks,
        )
    if config.workers.runner == "taskiq":
        from memspine.workers.taskiq_runner import TaskiqRunner

        return TaskiqRunner(url=config.workers.broker_url)
    raise ConfigError(
        f"unknown workers.runner {config.workers.runner!r} (valid: inline, temporal, taskiq)"
    )


@dataclass(frozen=True)
class OutboxHandle:
    """F-04: the durable outbox runner, or None when ``workers.outbox`` is off."""

    runner: OutboxRunner | None


@dataclass
class _EngineOutbox:
    """The outbox pieces one Engine's runners share (ADR-079): one job store (one schema
    check) per outbox database and one taskiq broker, with a distinct task per runner."""

    store: SqlJobStore
    brokers: dict[tuple[str, str | None], Any] = field(default_factory=dict)
    task_names: set[str] = field(default_factory=set)

    def task_name(self, label: str) -> str:
        from memspine.runtime.transport import TASK_NAME

        base = TASK_NAME if label == "windows" else f"{TASK_NAME}.{label}"
        name, n = base, 1
        while name in self.task_names:
            n += 1
            name = f"{base}.{n}"
        self.task_names.add(name)
        return name


#: Per Engine (its lifecycle manager), per outbox sync engine. Weak keys: a stopped Engine's
#: manager is dropped with its entry.
_ENGINE_OUTBOXES: weakref.WeakKeyDictionary[ProviderLifecycleManager, dict[Any, _EngineOutbox]] = (
    weakref.WeakKeyDictionary()
)


def _engine_outbox(resources: ProviderLifecycleManager, engine: Any) -> _EngineOutbox:
    per_engine = _ENGINE_OUTBOXES.setdefault(resources, {})
    shared = per_engine.get(engine)
    if shared is None:
        shared = per_engine[engine] = _EngineOutbox(SqlJobStore(engine, close_engine=False))
    return shared


def _engine_broker(
    resources: ProviderLifecycleManager, shared: _EngineOutbox, kind: Any, url: str | None
) -> Any:
    from memspine.runtime.transport import make_broker, shutdown_broker

    broker = shared.brokers.get((kind, url))
    if broker is None:
        broker = shared.brokers[(kind, url)] = make_broker(kind, url)

        async def close() -> None:
            await shutdown_broker(broker)

        resources.own(f"outbox:broker:{id(broker):x}", close)
    return broker


def make_outbox_runner(
    config: MemspineConfig,
    sql: SqlAccess,
    resources: ProviderLifecycleManager,
    commit_lock: Callable[[str], Any] | None = None,
    *,
    label: str = "windows",
) -> OutboxRunner:
    """Build an inline runner over the Engine's outbox store on the storage database.

    The sync SQLAlchemy engine is an owned lease from the storage provider, so
    runtime does not inspect or retain a vendor client. Every runner of one Engine shares
    one job store and (under the taskiq transport) one broker; ``label`` names the
    runner's broker task (``windows`` keeps the task the worker entry point consumes).
    """
    lease = sql.sync_lease(resources, "outbox")
    parts = _engine_outbox(resources, lease.engine)
    runner: OutboxRunner
    if config.workers.transport == "taskiq":  # RT-01: the same outbox, ids sent over taskiq
        from memspine.runtime.transport import TransportRunner

        kind = config.workers.transport_broker
        runner = TransportRunner(
            parts.store,
            broker=_engine_broker(resources, parts, kind, config.workers.broker_url),
            local_drain=kind == "memory",
            commit_lock=commit_lock,
            task_name=parts.task_name(label),
        )
    else:
        runner = OutboxRunner(parts.store, commit_lock=commit_lock)
    if config.runtime.scheduler_v2:  # RT-02: backpressure + SLO ledger + background priority
        from memspine.observability import bindings as _bindings
        from memspine.runtime import scheduler as _sched

        # the starting engine bound the scheduler of its own settings; a bare runner gets a
        # private policy
        shared: _sched.Scheduler = _bindings.resolve("scheduler", None) or _sched.Scheduler(
            config.runtime
        )
        runner.configure_scheduler(shared.backpressure, shared.ledger)
    return runner


def build_outbox(config: MemspineConfig, wiring: Wiring, _storage: SqlStorage) -> OutboxHandle:
    """Provider: the runner when ``workers.outbox`` is on (after storage, so the schema exists)."""
    if not config.workers.outbox:
        return OutboxHandle(None)
    if wiring.sql is None:
        raise ConfigError("workers.outbox requires the storage SQL capability")
    return OutboxHandle(make_outbox_runner(config, wiring.sql, wiring.resources, label="outbox"))


# ── F-16e: Neo4j graph projection through the outbox ─────────────────────────────────────


def graph_mirror_slot(config: MemspineConfig) -> SlotConfig | None:
    """The ``storage.graph_mirror`` slot when one is configured (ADR-081).

    The mirror gate is a capability check: the selected provider must declare ``mirror``."""
    slot = config.storage.graph_mirror
    if slot is None:
        return None
    descriptor = _descriptor(PROVIDER_GRAPH_STORES, slot.provider, "storage.graph_mirror.provider")
    if "mirror" not in descriptor.declared_capabilities:
        raise ConfigError(
            f"storage.graph_mirror provider {slot.provider!r} lacks the 'mirror' capability"
        )
    return slot


def neo4j_outbox_on(config: MemspineConfig) -> bool:
    """Whether the separately configured graph mirror is enabled."""
    return graph_mirror_slot(config) is not None


@dataclass
class Neo4jProjection:
    """The pieces of the Neo4j mirror: the queueing projector, its runner, the store."""

    projector: Any
    runner: OutboxRunner
    store: Any

    async def close(self) -> None:
        failures: list[str] = []
        try:
            await self.runner.stop(1.0)
        except asyncio.CancelledError:
            failures.append("stop:CancelledError")
        except Exception as exc:
            failures.append(f"stop:{type(exc).__name__}")
        try:
            self.runner.close()
        except asyncio.CancelledError:
            failures.append("close:CancelledError")
        except Exception as exc:
            failures.append(f"close:{type(exc).__name__}")
        if failures:
            raise RuntimeError("Neo4j projection worker cleanup failed: " + ", ".join(failures))


async def make_neo4j_projection(
    config: MemspineConfig,
    sql: SqlAccess,
    resources: ProviderLifecycleManager,
    commit_lock: Callable[[str], Any] | None,
    entities: Any,
    compiled: str,
    provider_results: dict[str, BuildResult[Any]] | None = None,
    registry: ResourceRegistry | None = None,
) -> Neo4jProjection:
    """Build the outbox runner, the Neo4j store and the registered ``graph_projection`` handler."""
    from memspine.memories.associative.graph_outbox import Neo4jGraphHandler, Neo4jOutboxProjector

    mirror = graph_mirror_slot(config)
    if mirror is None:
        raise ConfigError("make_neo4j_projection needs storage.graph_mirror")
    descriptor = _descriptor(
        PROVIDER_GRAPH_STORES, mirror.provider, "storage.graph_mirror.provider"
    )
    settings = _settings(descriptor, mirror.settings)
    shared = SharedBuildContext(
        resources,
        _owner_id(config),
        config.runtime.provider_services.health_timeout_s,
        registry=registry,
        sql_pool=config.runtime.shared_resources.sql,
        http_pool=config.runtime.shared_resources.http,
    )
    graph_context = GraphBuildContext(
        shared,
        storage_placement(config, _owner_id(config)),  # mirror uses deployment-scoped ownership
        sql,
        generation_id=config.deployment.id or "localdev",
        resource_id="graph:mirror",
    )
    mirror_wiring = Wiring(
        resources,
        shared.health_timeout_s,
        config.runtime.provider_services.startup_timeout_s,
        owner_id=config.deployment.id,
        explicit_owner_id=config.deployment.id is not None,
    )
    built = await _build_provider(
        mirror_wiring, descriptor, settings, graph_context, result_key="graph.mirror"
    )
    if provider_results is not None:
        provider_results["graph.mirror"] = built
    store = built.service
    runner = make_outbox_runner(config, sql, resources, commit_lock, label="graph_mirror")
    Neo4jGraphHandler(store, entities, compiled=compiled).register(runner)
    return Neo4jProjection(Neo4jOutboxProjector(runner), runner, store)


def _window_outbox(eng: Any, config: MemspineConfig) -> OutboxRunner:
    """The factory ``ingest.windows`` calls for ``workers.outbox`` (it cannot import runtime)."""
    return make_outbox_runner(config, eng._sql, eng._provider_resources, eng._namespace_lock)


_ingest_windows.register_outbox_factory(_window_outbox)


# ── JOB and REQUEST providers ────────────────────────────────────────────────────────────


def build_job_state(info: JobInfo, hooks: WiringHooks) -> JobState:
    if hooks.pipeline_ctx is None:
        raise ConfigError("a job scope needs the pipeline_ctx wiring hook")
    return JobState(info=info, context=hooks.pipeline_ctx())


def build_request_state(info: RequestInfo) -> RequestState:
    return RequestState(request_id=uuid.uuid4().hex, info=info)


# ── the container ────────────────────────────────────────────────────────────────────────


def _provider() -> Provider:
    p = Provider(scope=MemScope.APP)
    p.provide(build_secrets, provides=SecretsService)
    p.provide(resolve_config, provides=ResolvedConfig)
    p.provide(config_of, provides=MemspineConfig)
    p.provide(check_capabilities, provides=Capabilities)
    p.provide(wiring_of, provides=Wiring)
    p.provide(build_storage, provides=SqlStorage)
    p.provide(build_cache, provides=KVCache)
    p.provide(provide_embedder, provides=EmbeddingService)
    p.provide(build_vector_store, provides=VectorStore)
    p.provide(build_lexical_store, provides=LexicalStore)
    p.provide(build_graph_store, provides=GraphStore)
    p.provide(provide_llm_router, provides=LLMRouter)
    p.provide(check_decision_provider, provides=DecisionState)
    p.provide(build_prompts, provides=PromptRegistry)
    p.provide(build_extractor, provides=SemanticExtractor)
    p.provide(build_write_pipeline, provides=SemanticWritePipeline)
    p.provide(build_runner, provides=TaskRunner)
    p.provide(build_outbox, provides=OutboxHandle)
    p.provide(build_job_state, scope=MemScope.JOB, provides=JobState)
    p.provide(build_request_state, scope=MemScope.REQUEST, provides=RequestState)
    return p


def _context_provider() -> Provider:
    p = Provider()
    p.from_context(provides=EngineSpec, scope=MemScope.APP)
    p.from_context(provides=WiringHooks, scope=MemScope.APP)
    p.from_context(provides=JobInfo, scope=MemScope.JOB)
    p.from_context(provides=RequestInfo, scope=MemScope.REQUEST)
    return p


def make_container(
    config: MemspineConfig | EngineSpec | None = None,
    *,
    hooks: WiringHooks | None = None,
) -> AsyncContainer:
    """The async container for one engine start.

    ``config`` is a ready :class:`MemspineConfig`, an :class:`EngineSpec` (template / user config /
    overrides, the two-phase bootstrap) or None (the defaults). Nothing is built until a service is
    asked for; closing the container finalises what was built.
    """
    spec = (
        config
        if isinstance(config, EngineSpec)
        else EngineSpec(config=config)
        if config is not None
        else EngineSpec()
    )
    return make_async_container(
        _provider(),
        _context_provider(),
        scopes=MemScope,
        context={EngineSpec: spec, WiringHooks: hooks or WiringHooks()},
        lock_factory=None,
    )


@asynccontextmanager
async def job_scope(
    container: AsyncContainer, info: JobInfo | None = None
) -> AsyncIterator[AsyncContainer]:
    """Enter the JOB scope: ``JobState`` (a fresh derivation context) is built per entry."""
    async with container(context={JobInfo: info or JobInfo()}, scope=MemScope.JOB) as job:
        yield job


@asynccontextmanager
async def request_scope(
    container: AsyncContainer, info: RequestInfo | None = None
) -> AsyncIterator[AsyncContainer]:
    """Enter the REQUEST scope: ``RequestState`` is built fresh per entry."""
    context = {RequestInfo: info or RequestInfo()}
    async with container(context=context, scope=MemScope.REQUEST) as req:
        yield req
