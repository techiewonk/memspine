"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memspine.config import constants
from memspine.core.events import EventLogMode
from memspine.core.namespace import validate_namespace
from memspine.core.registry import validate_types
from memspine.exceptions import ConfigError

__all__ = [
    "CacheConfig",
    "EmbeddingConfig",
    "EventLogConfig",
    "GraphConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "MemoryTypeConfig",
    "MemspineConfig",
    "NamespaceConfig",
    "PromptsConfig",
    "ReadConfig",
    "StorageConfig",
    "VectorConfig",
]


class EventLogConfig(BaseModel):
    """At-rest lifetime of the memory_events log (D-45)."""

    model_config = ConfigDict(extra="forbid")

    mode: EventLogMode = EventLogMode.FULL
    retention_days: int = Field(default=constants.EVENT_LOG_RETENTION_DAYS, ge=1)
    compress: bool = False


class StorageConfig(BaseModel):
    """Event-log + read-model storage (D-36, Phase 6). ``sqlite`` (default) uses
    ``path`` (a file, or ``:memory:``); ``postgres`` uses ``url`` (a DSN,
    secrets-resolved) and ``data_dir`` as the base directory for the derived
    file-backed projections (LanceDB vectors, Tantivy lexical) that live outside
    the SQL database. Both backends share one dialect-neutral schema (ADR-025)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "sqlite"  # sqlite | postgres
    path: str = "./memspine.db"  # sqlite db file, or ":memory:"
    url: str | None = None  # postgres DSN, required when backend=postgres
    data_dir: str | None = None  # base dir for derived vector/lexical files (postgres)


class EmbeddingConfig(BaseModel):
    """Embedder defaults (D-08). ``hash`` is the deterministic zero-network
    provider for tests/CI; ``fastembed`` (ONNX, CPU) is the production default;
    ``static`` is the cheap model2vec table (``[static]``, E4/ADR-020)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "fastembed"  # fastembed | hash | static | litellm
    model: str = "BAAI/bge-small-en-v1.5"
    #: REQUIRED when provider=litellm — a cloud embedder's output dimension is
    #: not locally discoverable, and the vector store needs it up front.
    dim: int | None = None
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None  # bedrock


class VectorConfig(BaseModel):
    """Vector store selection (D-09, amended by ADR-021). ``lance`` (LanceDB) is
    the sole vector store and now a core dependency — the zero-dep SQLite
    brute-force store was removed, so there is no ``auto``/``sqlite`` fallback.
    ``weaviate`` (and future remote stores) keep the config seam open but are
    not built yet; selecting one raises ``ConfigError``. An ``:memory:`` event
    log points LanceDB at a per-engine scratch dir removed on ``stop()`` so the
    projection never outlives its log (D0.1).

    ``quantization`` drives the E4 two-stage rescore (ADR-020), realized by
    LanceDB's native compressed ANN index (IVF_HNSW_SQ / IVF_PQ): ``auto``
    (default) reads the embedder manifest — the default embedders declare none,
    so the exact float32 path is unchanged; ``none`` forces it off;
    ``int8``/``binary`` force that scheme even for an embedder that does not
    declare it (a deployer opting a known-tolerant model in). Matryoshka
    truncation is manifest-only (the model must be trained for it).
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "lance"  # lance (sole store); weaviate reserved (ADR-021)
    quantization: str = "auto"  # auto | none | int8 | binary (E4/ADR-020)


class GraphConfig(BaseModel):
    """Graph store selection (D-26). ``sqlite_adjacency`` is the zero-dep v0.1
    default; ``ladybug`` (the published Kuzu fork, ``[graph]``) is the intended
    embedded default once a follow-up ADR flips it — until then it is a fully
    working opt-in; ``kuzu`` is the first-class embedded-Cypher alternative
    behind ``[kuzu]``. The store is only constructed when associative memory
    is enabled or this block is set explicitly — ``profile="simple"`` never
    touches it."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "sqlite_adjacency"  # sqlite_adjacency | kuzu | ladybug | neo4j


class LLMRoleConfig(BaseModel):
    """One provider binding per role (D-07/D-22/D-33), routed through LiteLLM.

    ``model`` is a LiteLLM model id whose **prefix selects the provider**:
    ``openai/gpt-4o``, ``ollama/llama3`` (local — set ``api_base``, e.g.
    ``http://localhost:11434``), ``bedrock/anthropic.claude-3-5-sonnet-...``
    (set ``aws_region`` or rely on the boto3 credential chain),
    ``vertex_ai/gemini-...``, ``azure/...``, etc. The special prefix
    ``llamacpp/<path-to.gguf>`` routes to the in-process
    :class:`LlamaCppLLM` (``[llmlocal]``) instead of LiteLLM."""

    model_config = ConfigDict(extra="forbid")

    model: str = ""
    api_base: str | None = None  # local endpoint override (e.g. Ollama, vLLM)
    api_key: str | None = None
    aws_region: str | None = None  # bedrock
    timeout_seconds: float = 60.0


class LLMConfig(BaseModel):
    """Per-role providers: extract / judge / chat (M14). Roles absent here are
    disabled; the engine only requires them when a feature needs the role."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)


class PromptsConfig(BaseModel):
    """User prompt customization (D-43): per-prompt overrides that ride the
    ordinary config layering. Keys under an override: body / system / format /
    version / output_model / token_budget (validated by the registry).

    ``partials`` (B1) supplies override fragments for the shared Jinja
    ``{% include %}`` partials (anti-injection block, output footer, format
    instructions): ``prompts.partials.<name>`` maps a partial name to its
    replacement text, consulted before the shipped ``_partials/`` directory.

    ``selection`` (B2) pins per-role default scenario selectors:
    ``prompts.selection.<role>`` is a map with optional ``memory_type`` /
    ``condition`` keys, merged into every ``select(role)`` query the caller
    doesn't override — so a deployment can force a scenario variant of a role's
    prompt without a code change."""

    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    partials: dict[str, str] = Field(default_factory=dict)
    selection: dict[str, dict[str, str]] = Field(default_factory=dict)


class WorkersConfig(BaseModel):
    """Background runner selection (D-16): inline (default) / dbos [dbos] /
    taskiq [taskiq] (P7, D-42 §3). Validated against the known set at engine
    start. ``broker_url`` is the Redis/Valkey endpoint the taskiq runner's
    per-scope streams live on; ignored by the other runners.
    ``dbos_system_database_url`` is ignored by the other runners too: None
    (default) derives a SQLite file colocated with ``storage.path`` — zero
    external infra, matching every other core default (D-09/D-25/D-26); set
    it to a Postgres URL only for multi-instance deployments where DBOS's own
    cross-replica recovery requires a shared system database."""

    model_config = ConfigDict(extra="forbid")

    runner: str = "inline"
    broker_url: str = "redis://localhost:6379/0"
    dbos_system_database_url: str | None = None
    #: D1: autonomous maintenance. When set to a positive number of seconds the
    #: engine starts a background loop that runs the full sleep cycle
    #: (consolidate → … → decay → prune) on that interval. None (default) keeps
    #: v0.1 behavior — the cycle runs only on an explicit ``Engine.sleep()``.
    sleep_interval_seconds: float | None = None


class ReadConfig(BaseModel):
    """ReadPolicy bindings (M12) + the E8/E5 opt-in stages (D-51, default OFF).

    ``scoring``/``assembly`` flow into ``ScoringPolicy.bind`` /
    ``AssemblyPolicy.bind``; per-namespace overrides ride the D-14
    policy-override channel later.

    - ``rerank``: ``off`` (default) | ``fastembed`` (ONNX cross-encoder) |
      ``flashrank`` (``[rerank]`` extra) — E8 rerank stage over the candidate
      set, fed concat_background text (D-42 §5).
    - ``static_prefilter``: cheap lexical-overlap gate before rerank/score (E8).
    - ``static_embedding_prefilter``: E4 model2vec static-embedding gate
      (``[static]``) that narrows the candidate set with cheap static cosine
      before rerank/score. Default OFF; when on but the extra is missing the
      engine skip-logs and the stage is a no-op (retrieval never fails).
    - ``hybrid``: fuse the vector leg with a lexical BM25 leg via RRF (D-25).
      **Default ON (v0.2 A3)** — the lexical BM25 index is built and fused into
      every search so records that only exact keywords surface can still land.
      Set ``hybrid: false`` for the pre-v0.2 vector-only pipeline: bit-identical
      to vector-only results and no lexical index is built. This is D-25's
      core-default intent (see ADR-019); the default ``tantivy`` provider is a
      **core** dependency and is backend-independent, so the default works on
      every storage backend with no extra to install.
    - ``lexical_provider``: which lexical store backs the hybrid leg —
      ``tantivy`` (default, standalone Tantivy BM25 index — **core**, no extra,
      independent of the storage backend; an on-disk index dir beside the db, or
      in-RAM for a ``:memory:`` log) or ``opensearch`` (``[opensearch]`` extra —
      server-scale BM25 for large multi-node deployments). All satisfy the same
      ``LexicalStore`` port and share ``LexicalProjector``. The transactional-DB
      ``sqlite_fts5`` provider was removed (v0.2): the lexical leg is a dedicated
      search index, never bolted onto the system-of-record.
      Only consulted when ``hybrid`` is on (the default); with ``hybrid: false``
      no store is built. There is intentionally **no lexical-only reranking** — the fused
      RRF result is reranked (when enabled) by the E8 cross-encoder stage
      (``rerank`` above), which scores the query against fused candidates from
      *both* legs, so a second BM25 pass would add nothing.
    - ``compression``: options for the E5 assembly-stage ``CompressionPolicy``
      binding (``{"assembly": true, "assembly_stage": [...]}``); the master
      switch defaults off so ``profile="simple"`` behavior never changes.
    """

    model_config = ConfigDict(extra="forbid")

    scoring: dict[str, Any] = Field(default_factory=dict)
    assembly: dict[str, Any] = Field(default_factory=dict)
    rerank: str = "off"  # off | fastembed | flashrank | litellm
    #: LiteLLM rerank model id, required when rerank=litellm
    #: (e.g. cohere/rerank-english-v3.0, bedrock/amazon.rerank-v1:0).
    rerank_model: str | None = None
    static_prefilter: bool = False
    static_embedding_prefilter: bool = False
    hybrid: bool = True  # v0.2 A3: default-on hybrid retrieval (D-25, ADR-019)
    lexical_provider: str = "tantivy"  # tantivy (core, default) | opensearch [opensearch] (D-25)
    compression: dict[str, Any] = Field(default_factory=dict)


class MemoryTypeConfig(BaseModel):
    """Per-instance enablement + per-type policy overrides (D-14)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policies: dict[str, Any] = Field(default_factory=dict)


class NamespaceConfig(BaseModel):
    """Per-namespace *policy* overrides only (D-14)."""

    model_config = ConfigDict(extra="forbid")

    policies: dict[str, Any] = Field(default_factory=dict)


class CacheConfig(BaseModel):
    """KV cache selection (D-09, ADR-022 amendment). One cache is built and shared
    by the embedding (E3) and extraction (E3) caches — the ``emb:``/``ext:``
    producer key prefixes already keep them from colliding in a shared store.

    - ``memory`` (default): in-process :class:`MemoryKV`, zero-dep core (slim-core
      D-03 keeps this hand-rolled — cashews never imports into core).
    - ``disk`` (``[cache]``): persistent on-disk cache (cashews → diskcache) at
      ``path`` (a directory); survives restarts.
    - ``redis`` / ``valkey`` (``[cache]``): shared cross-process cache at ``url``
      (cashews → redis-py; valkey is redis-wire-compatible), native TTL.

    ``namespace`` prefixes every key so multiple memspine instances can share
    one disk dir / redis server safely. ``default_ttl_seconds`` (``None`` = no
    expiry) applies when a caller does not pass an explicit TTL. ``url`` is
    secrets-resolved by the config loader (Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "memory"  # memory | disk | redis | valkey (disk/redis/valkey via cashews)
    path: str = "./memspine.cache"  # disk cache directory
    url: str = "redis://localhost:6379/0"  # redis/valkey DSN
    namespace: str = "memspine"
    default_ttl_seconds: float | None = None
    max_entries: int = constants.MEMORY_KV_MAX_ENTRIES  # memory backend cap


class MemspineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "simple"
    strict_services: bool = True
    event_log: EventLogConfig = Field(default_factory=EventLogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    memories: dict[str, MemoryTypeConfig] = Field(default_factory=dict)
    namespaces: dict[str, NamespaceConfig] = Field(default_factory=dict)

    @field_validator("memories")
    @classmethod
    def _known_memory_types(cls, value: dict[str, MemoryTypeConfig]) -> dict[str, MemoryTypeConfig]:
        validate_types(set(value))
        return value

    @field_validator("namespaces", mode="before")
    @classmethod
    def _validate_namespaces(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        checked: dict[str, Any] = {}
        for raw_path, ns_config in value.items():
            path = validate_namespace(str(raw_path))
            if isinstance(ns_config, dict) and "memories" in ns_config:
                raise ConfigError(
                    f"namespaces.{path}.memories is reserved for v0.2 "
                    "per-namespace type enablement (D-14) — remove it"
                )
            checked[path] = ns_config
        return checked

    def enabled_memories(self) -> set[str]:
        return {name for name, mem in self.memories.items() if mem.enabled}
