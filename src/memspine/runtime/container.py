"""The dependency-injection container (F-08; RESTRUCTURE section 1, ARCHITECTURE section 8).

The engine used to wire its own services in ``Engine._start_inner``. The builders now live here
as :mod:`dishka` providers, in three scopes:

``APP``      long-lived services, built once per engine start and finalised at ``stop``: the
             resolved config, secrets, storage, the KV cache, the embedder, the vector / lexical /
             graph stores, the LLM router, the prompt registry, the task runner.
``JOB``      one background job: a fresh :class:`PipelineContext` (the derivation context a
             pipeline run receives).
``REQUEST``  one read or write call: a fresh :class:`RequestState` for the call's scope / viewer.

Providers resolve backends by name through the MOD-02 registries (``memspine.store.registry``,
``memspine.models.registry``) so the plug-ins pinned in ``config.plugins`` apply; the pins are
enabled by the :class:`Capabilities` provider, which every infrastructure provider depends on.

The builder bodies are moved from the engine unchanged. What they read from engine state
(``self._client`` ...) comes from :class:`Wiring`, the one mutable holder of the clients a build
opens; what they call back into the engine for (``_pipeline_ctx``, ``_withheld_texts``,
``_edge_extract_callable``) arrives as :class:`WiringHooks`. Providers are lazy and the engine asks
for them in the order it always built them, so the start order, the lazy/eager choices and the
errors raised are the ones the engine had.

``make_container`` passes ``lock_factory=None``: the container takes no lock of its own (a
resolution never runs a model call, and RESTRUCTURE section 4 keeps model calls out of locks).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from dishka import AsyncContainer, BaseScope, Provider, make_async_container, new_scope

from memspine.clients.cashews import CashewsClient
from memspine.clients.kuzu import KuzuClient
from memspine.clients.ladybug import LadybugClient
from memspine.clients.lancedb import LanceDBClient
from memspine.clients.postgres import PostgresClient
from memspine.clients.sqlite import SQLiteClient
from memspine.config import constants
from memspine.config.loader import ResolvedConfig, default_template, load_config
from memspine.config.schema import MemspineConfig
from memspine.core.registry import SERVICE_EXTRAS, dependency_closure, missing_services
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.memories.semantic.entities import EntityExtractor, LLMEntityExtractor
from memspine.memories.semantic.write_pipeline import (
    GraphWritePipeline,
    WritePipeline,
    cardinality_map,
    extraction_rounds,
)
from memspine.models.cache.base import KVCache, MemoryKV
from memspine.models.cache.semantic import CachedEmbedding, CachedExtractor
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.models.providers.llm import structured as _structured
from memspine.models.providers.llm.base import LLMRouter, LLMService
from memspine.models.providers.llm.tier_gate import TierGatedLLM, is_local_provider
from memspine.models.registry import LLM_PROVIDERS, build_embedder, llm_provider_name
from memspine.observability.logging import get_logger
from memspine.plugins import enable as enable_plugins
from memspine.prompts.registry import PromptRegistry
from memspine.services.graph.base import GraphStore
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.lexical.base import LexicalStore
from memspine.services.secrets.base import SecretsService
from memspine.services.secrets.chained import ChainedSecrets
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.vector.base import VectorStore
from memspine.store.registry import GRAPH_STORES, LEXICAL_INDEXES, VECTOR_STORES
from memspine.workers.inline import InlineRunner
from memspine.workers.pipelines import PIPELINES, ExtractEdges, PipelineContext
from memspine.workers.runner import TaskRunner

__all__ = [
    "Capabilities",
    "DecisionState",
    "EngineSpec",
    "JobInfo",
    "JobState",
    "MemScope",
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
    "memory_policy",
    "request_scope",
]

_log = get_logger(__name__)

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

    #: builds a fresh :class:`PipelineContext` (JOB scope; the DBOS runner's context factory)
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
    """The clients a build opens, plus the small side-results the builders used to leave on the
    engine. Created empty; the builders fill it as they connect. ``close_clients`` is the
    container finaliser: it closes in the order the engine's ``stop`` always did."""

    client: SQLiteClient | None = None
    pg: PostgresClient | None = None
    lance: LanceDBClient | None = None
    kuzu: KuzuClient | None = None
    ladybug: LadybugClient | None = None
    cashews: CashewsClient | None = None
    #: #43: the extraction cache, kept so erasure can purge an erased text's entry
    cached_extractor: CachedExtractor | None = None
    #: E4: whether the vector leg runs the two-stage quantized rescore
    rescore_active: bool = False

    async def close_clients(self) -> None:
        for client in (self.client, self.pg, self.lance, self.kuzu, self.ladybug, self.cashews):
            if client is not None:
                await client.close()


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
    return config.storage.backend == "sqlite" and config.storage.path == ":memory:"


def derived_base(config: MemspineConfig) -> str:
    """Base path for the file-backed projections that live OUTSIDE the SQL
    database (LanceDB vectors, Tantivy lexical). For ``sqlite`` it is the db
    path (``<path>.lance`` etc.); for ``postgres`` it is ``storage.data_dir``
    (required — the DSN is not a filesystem path)."""
    if config.storage.backend == "postgres":
        if not config.storage.data_dir:
            raise ConfigError(
                "storage.data_dir is required when storage.backend='postgres' — "
                "it is the base directory for the LanceDB/Tantivy projections"
            )
        return str(Path(config.storage.data_dir) / "memspine")
    return config.storage.path


def lexical_variant(config: MemspineConfig) -> str:
    """N58 / N30: the lexical index variant ("" = the default index): the analyzer
    when not ``default``, and ``dates`` with ``read.lexical_dates``."""
    parts: list[str] = (
        [] if config.read.lexical_analyzer == "default" else [config.read.lexical_analyzer]
    )
    if config.read.lexical_dates:
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
    override = config.vector.quantization
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
        from memspine.services.secrets.aws import AwsSecrets

        return ChainedSecrets(env_secrets, AwsSecrets())
    raise ConfigError(f"unknown MEMSPINE_SECRETS_BACKEND {backend!r} (valid: env, aws)")


def resolve_config(spec: EngineSpec, secrets: SecretsService) -> ResolvedConfig:
    if spec.config is not None:
        return ResolvedConfig(config=spec.config)
    return load_config(
        template=default_template(spec.template, spec.user_config, spec.overrides),
        user_config=spec.user_config,
        env=os.environ,
        overrides=spec.overrides,
        secret_resolver=secrets.get,
    )


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


async def wiring_of(_caps: Capabilities) -> AsyncIterator[Wiring]:
    wiring = Wiring()
    try:
        yield wiring
    finally:
        await wiring.close_clients()


# ── APP providers: services ──────────────────────────────────────────────────────────────


async def build_storage(config: MemspineConfig, wiring: Wiring) -> SqlStorage:
    """Storage backend dispatch (D-36, Phase 6). ``sqlite`` (default) opens a
    SQLiteClient at ``storage.path``; ``postgres`` opens a PostgresClient at
    ``storage.url`` (already secrets-resolved by the config loader). Both wrap
    the same dialect-neutral :class:`SqlStorage`."""
    backend = config.storage.backend
    mode = config.event_log.mode
    compress = config.event_log.compress
    encryption = config.storage.encryption
    if encryption.mode == "sqlcipher" and backend != "sqlite":
        raise ConfigError(
            "storage.encryption.mode=sqlcipher applies to the sqlite backend only "
            f"(storage.backend={backend!r}; use the database's own encryption at rest)"
        )
    if backend == "sqlite":
        cipher_env = encryption.key_env if encryption.mode == "sqlcipher" else None
        wiring.client = SQLiteClient(config.storage.path, cipher_key_env=cipher_env)
        await wiring.client.connect()
        if cipher_env is not None:
            # #52/ADR-035: name what the option does NOT cover; never the key.
            _log.warning(
                "storage.encryption_partial",
                detail="SQLCipher encrypts the SQLite event log and read model only; "
                "LanceDB vectors, the Tantivy lexical index, disk caches and a DBOS "
                "system database are separate files and are not encrypted",
                key_env=cipher_env,
            )
        return SQLiteStorage(wiring.client, mode=mode, compress=compress)
    if backend == "postgres":
        if not config.storage.url:
            raise ConfigError("storage.url is required when storage.backend='postgres'")
        from memspine.services.storage.postgres.engine import PostgresStorage

        wiring.pg = PostgresClient(config.storage.url)
        await wiring.pg.connect()
        return PostgresStorage(wiring.pg, mode=mode, compress=compress)
    raise ConfigError(f"unknown storage.backend {backend!r} (valid: sqlite, postgres)")


async def build_cache(config: MemspineConfig, wiring: Wiring) -> KVCache:
    """Build the one shared KV cache (D-09, ADR-022 amendment). ``memory`` is
    the zero-dep :class:`MemoryKV` core default; ``disk``/``redis``/``valkey``
    go through **cashews** (`[cache]` extra) — on-disk (diskcache) or a shared
    server. A missing extra hard-fails with the D-10 error (raised by the
    client on ``connect()``)."""
    cache = config.cache
    backend = cache.backend
    if backend == "memory":
        return MemoryKV(max_entries=cache.max_entries)
    if backend in ("disk", "redis", "valkey"):
        from memspine.models.cache.cashews_cache import CashewsCache

        # disk → diskcache dir at cache.path; redis/valkey → cache.url DSN
        # (valkey is redis-wire-compatible, so it rides the same client).
        url = f"disk://?directory={cache.path}" if backend == "disk" else cache.url
        wiring.cashews = CashewsClient(url)
        await wiring.cashews.connect()
        return CashewsCache(
            wiring.cashews,
            namespace=cache.namespace,
            default_ttl_seconds=cache.default_ttl_seconds,
        )
    raise ConfigError(f"unknown cache.backend {backend!r} (valid: memory, disk, redis, valkey)")


def make_embedder(config: MemspineConfig) -> EmbeddingService:
    # MOD-02: the typed ``embedders`` registry (memspine.models.registry) holds the factories.
    return cast("EmbeddingService", build_embedder(config.embedding))


def provide_embedder(
    config: MemspineConfig, cache: KVCache, _caps: Capabilities
) -> EmbeddingService:
    """The E3-cached embedder (the cache is the one shared KV cache, Phase 2)."""
    return CachedEmbedding(make_embedder(config), cache)


async def build_vector_store(
    config: MemspineConfig, embedder: EmbeddingService, wiring: Wiring
) -> VectorStore:
    quantization, matryoshka_dim = rescore_settings(config, embedder)
    wiring.rescore_active = quantization is not None or matryoshka_dim is not None
    backend = config.vector.backend
    plug = VECTOR_STORES.find(backend)
    if plug is not None:  # MOD-02: an enabled plug-in vector store
        return cast(
            "VectorStore",
            plug(config, derived_base(config), client_is_memory(config)),
        )
    if backend not in VECTOR_STORES:
        # ``weaviate`` (and future remote stores) keep the config seam open
        # but are not built yet — LanceDB is the sole reference adapter
        # (ADR-021, amends D-09). The zero-dep SQLite brute-force store was
        # removed with it, so there is no ``auto``/``sqlite`` path anymore.
        raise ConfigError(f"unknown vector.backend {backend!r} (valid: lance; weaviate reserved)")
    # LanceDB is the only vector store (ADR-021). E4 (ADR-020 §6): LanceDB
    # owns quantization natively — an active int8/binary/Matryoshka manifest
    # drives its native compressed ANN index (IVF_HNSW_SQ / IVF_PQ) queried
    # with refine_factor + nprobes. ``rescore_active`` stays as resolved
    # above; the store degrades to a flat exact query (skip-logged once)
    # when the corpus is too small to train an index.
    from memspine.services.vector.lancedb_store import LanceDBVectorStore

    if client_is_memory(config):
        # A projection must never outlive its log (D0.1): an in-memory event
        # log gets an in-memory Lance table. LanceDB keeps a ``memory://``
        # store private to its connection, so concurrent :memory: engines
        # never share rows and the table is freed when the client closes.
        # A scratch directory on disk gave the same results but cost a new
        # fragment and version file per write (and minutes to delete).
        lance_path = "memory://vectors"
        exclusive = True  # no other engine can reach this table
    else:
        # sqlite: <path>.lance beside the db; postgres: <data_dir>/memspine.lance
        lance_path = f"{derived_base(config)}.lance"
        # A file-backed table may be shared with concurrent engines (D-45):
        # compacting it could conflict with their writes, and only a merge
        # knows whether an id is already there, so it keeps the plain path.
        exclusive = False
    wiring.lance = LanceDBClient(lance_path)
    await wiring.lance.connect()
    lance = wiring.lance

    def make(suffix: str = "") -> LanceDBVectorStore:
        return LanceDBVectorStore(
            lance,
            embedder,
            quantization=quantization,
            matryoshka_dim=matryoshka_dim,
            oversample=constants.RESCORE_OVERSAMPLE,
            compact_every=constants.LANCE_COMPACT_EVERY if exclusive else None,
            exclusive=exclusive,
            namespace_index=config.vector.namespace_index,
            table_suffix=suffix,
        )

    if config.vector.isolation == "per_namespace":
        # I4: one table per namespace (``services/vector/per_namespace.py``).
        from memspine.services.vector.per_namespace import PerNamespaceVectorStore

        return cast("VectorStore", PerNamespaceVectorStore(lance, embedder, make))
    return make()


def build_lexical_store(config: MemspineConfig, _caps: Capabilities) -> LexicalStore:
    """Lexical provider selection (D-25). ``tantivy`` (default, **core**)
    builds a standalone BM25 index independent of the storage backend;
    ``opensearch`` (``[opensearch]`` extra) is the server-scale seam. Both are
    rebuildable projections driven by the same :class:`LexicalProjector`. The
    transactional-DB ``sqlite_fts5`` provider was removed (v0.2): the lexical
    leg is a dedicated search index, never bolted onto the system-of-record."""
    provider = config.read.lexical_provider
    plug = LEXICAL_INDEXES.find(provider)
    if plug is not None:  # MOD-02: an enabled plug-in lexical index
        return cast(
            "LexicalStore",
            plug(config, derived_base(config), client_is_memory(config)),
        )
    if provider == "tantivy":
        from memspine.services.lexical.tantivy import TantivyLexical

        # In-RAM index for an in-memory event log (same lifetime, no ghost
        # segments across runs — mirrors the vector store's :memory: rule);
        # else an on-disk directory beside the derived base.
        variant = lexical_variant(config)
        suffix = f".tantivy-{variant.replace('+', '-')}" if variant else ".tantivy"
        index_path = None if client_is_memory(config) else f"{derived_base(config)}{suffix}"
        return TantivyLexical(
            index_path,
            analyzer=config.read.lexical_analyzer,
            date_tokens=config.read.lexical_dates,
        )
    if provider == "opensearch":
        from memspine.services.lexical.opensearch import OpenSearchLexical

        return cast("LexicalStore", OpenSearchLexical(config.read))  # stub — always raises
    raise ConfigError(f"unknown read.lexical_provider {provider!r} (valid: tantivy, opensearch)")


async def build_graph_store(config: MemspineConfig, wiring: Wiring) -> GraphStore:
    """Graph provider selection (D-26). ``sqlite_adjacency`` (default) rides
    the existing SQLite client; ``kuzu`` and ``ladybug`` each open their own
    embedded database via a dedicated client (D-24) — in-memory when the
    event log is in-memory, so the projection can never outlive its log
    (D0.1)."""
    provider = config.graph.provider
    if provider == "auto":
        # GR-1 (ADR-064): LadybugDB, the graph engine (ADR-034), when installed;
        # else the zero-dep SQLite adjacency lists (or LadybugDB on postgres).
        import importlib.util

        has_ladybug = importlib.util.find_spec("ladybug") is not None
        provider = "ladybug" if has_ladybug or wiring.client is None else "sqlite_adjacency"
    plug = GRAPH_STORES.find(provider)
    if plug is not None:  # MOD-02: an enabled plug-in graph store
        return cast(
            "GraphStore",
            plug(config, derived_base(config), client_is_memory(config)),
        )
    if provider == "sqlite_adjacency":
        if wiring.client is None:
            raise ConfigError(
                "graph.provider='sqlite_adjacency' requires storage.backend='sqlite' — "
                "use 'kuzu' or 'ladybug' (own embedded db) with a postgres backend"
            )
        return SQLiteAdjacencyGraph(wiring.client)
    if provider == "kuzu":
        from memspine.services.graph.kuzu import KuzuGraphStore

        path = ":memory:" if client_is_memory(config) else f"{derived_base(config)}.kuzu"
        wiring.kuzu = KuzuClient(path)
        await wiring.kuzu.connect()
        return KuzuGraphStore(wiring.kuzu)
    if provider == "ladybug":
        from memspine.services.graph.ladybug import LadybugGraphStore

        path = ":memory:" if client_is_memory(config) else f"{derived_base(config)}.ladybug"
        wiring.ladybug = LadybugClient(path)
        await wiring.ladybug.connect()
        return LadybugGraphStore(wiring.ladybug)
    if provider == "neo4j":
        from memspine.services.graph.neo4j import Neo4jGraphStore

        return cast("GraphStore", Neo4jGraphStore())  # stub — always raises
    raise ConfigError(
        f"unknown graph.provider {provider!r} (valid: sqlite_adjacency, kuzu, ladybug, neo4j)"
    )


async def build_llm_router(config: MemspineConfig, hooks: WiringHooks) -> LLMRouter:
    """Route each role by its ``model`` prefix (D-33). ``llamacpp/<path>``
    binds the in-process :class:`LlamaCppLLM` (``[llmlocal]``); every other
    model id — ``openai/…``, ``ollama/…``, ``bedrock/…``, ``vertex_ai/…`` —
    goes through the unified LiteLLM adapter. litellm is imported lazily so a
    default engine with no LLM role never pays its import cost."""
    providers: dict[str, LLMService] = {}
    _structured.configure(config.llm.structured.model_dump())  # I69 (process-wide)
    for role, role_config in config.llm.roles.items():
        model = role_config.model
        if model:
            # MOD-02: typed ``llm_providers`` registry (llamacpp/, plug-in, else LiteLLM).
            providers[role] = LLM_PROVIDERS.get(llm_provider_name(model))(model, role_config)
        else:
            raise ConfigError(
                f"llm.roles.{role}.model is required — a LiteLLM model id "
                "(e.g. openai/gpt-4o, ollama/llama3, bedrock/...) or llamacpp/<path>"
            )
    consent = config.consent
    if consent.remote_llm_max_tier is not None:
        # #50: a remote provider never sees text of records above the tier.
        for role, role_config in config.llm.roles.items():
            if not is_local_provider(role_config.model, role_config.api_base, consent.local_hosts):
                providers[role] = TierGatedLLM(providers[role], hooks.withheld_texts)
    return LLMRouter(providers)


async def provide_llm_router(
    config: MemspineConfig, hooks: WiringHooks, _caps: Capabilities
) -> LLMRouter:
    return await build_llm_router(config, hooks)


def check_decision_provider(config: MemspineConfig) -> DecisionState:
    """H24, at ``start()``: a configured gliner2 provider must be importable (D-10).

    Missing gliner2 raises ``MissingServiceError(extra="ner")``; with
    ``strict_services: false`` it logs once and the provider stays off.
    """
    if config.decision.provider == "off":
        return DecisionState(off=False)
    from memspine.models.providers.decision.gliner2_decision import gliner2_class

    try:
        gliner2_class()
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

        return SemanticExtractor(GlinerEntityExtractor())
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


def build_runner(config: MemspineConfig, hooks: WiringHooks, _caps: Capabilities) -> TaskRunner:
    if config.workers.runner == "inline":
        return InlineRunner()
    if config.workers.runner == "dbos":
        from memspine.workers.dbos_runner import DBOSRunner, default_system_database_url

        system_database_url = (
            config.workers.dbos_system_database_url
            or default_system_database_url(config.storage.path)
        )
        # ``hooks.pipeline_ctx`` is safe to hand over now: by this point in the
        # engine's start storage/config are already set, and the hook builds a
        # FRESH PipelineContext on every call — exactly what the durable
        # workflow needs on recovery (see dbos_runner's module docstring).
        if hooks.pipeline_ctx is None:
            raise ConfigError("workers.runner='dbos' needs the pipeline_ctx wiring hook")
        runner = DBOSRunner(
            system_database_url=system_database_url,
            context_factory=hooks.pipeline_ctx,
        )
        # Register every pipeline BEFORE launch(): DBOS dispatches
        # recovery of any crash-orphaned PENDING workflow the moment
        # launch() runs, and that recovery resolves pipelines by name
        # through this same runner (dbos_runner's `_run_pipeline_
        # workflow`) — an empty registry would race it. The generic
        # registration loop in the engine (identical for every runner) still
        # runs afterwards; re-registering the same names is a no-op.
        for name, pipeline in PIPELINES.items():
            runner.register(name, pipeline)
        runner.launch()
        return runner
    if config.workers.runner == "taskiq":
        from memspine.workers.taskiq_runner import TaskiqRunner

        return TaskiqRunner(url=config.workers.broker_url)
    raise ConfigError(
        f"unknown workers.runner {config.workers.runner!r} (valid: inline, dbos, taskiq)"
    )


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
