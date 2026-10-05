"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.
"""

from __future__ import annotations

from typing import Any, Literal

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
    #: litellm only: ask the model for exactly ``dim`` dimensions (Matryoshka
    #: models: Cohere embed-v4 256/512/1024/1536, Titan v2, OpenAI v3). Off =
    #: the model's default size, which must then equal ``dim``.
    request_dimensions: bool = False
    #: litellm only: asymmetric retrieval models (Cohere: ``search_query`` /
    #: ``search_document``). None = the provider default for both.
    query_input_type: str | None = None
    document_input_type: str | None = None


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
    #: Qwen3 thinking control: ``true`` appends the ``/no_think`` soft switch to the
    #: last user message; ``None`` turns it on for model ids containing ``qwen3``
    #: (Qwen3 thinks by default, which costs tokens and breaks structured parsing).
    #: ``<think>...</think>`` blocks are stripped from every reply regardless.
    no_think: bool | None = None


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
    #: Append a RETRIEVE event per search (access stats feed reinforcement, M1).
    #: ``false`` makes reads side-effect free, e.g. so benchmark questions cannot
    #: change the store that later questions see.
    record_access: bool = True
    #: C4': render each retrieved keyed fact as ``CURRENT (since date)`` plus its
    #: superseded ``HISTORY`` from the bi-temporal chain (deterministic, no LLM).
    current_state_view: bool = False
    #: C3': fuse a temporal leg (records whose event time lies in an absolute
    #: date span named in the query) and a metadata leg (records whose entity is
    #: named in the query) into the RRF ranking. Off: bit-identical ranking.
    temporal_leg: bool = False
    metadata_leg: bool = False
    #: H13: an extra BM25 leg over the question's core terms (interrogative and
    #: function words removed), fused by RRF. Needs the lexical store (hybrid).
    core_terms_leg: bool = False
    #: H18: run the reranker only when ``top_k`` is at most this (reranking helps
    #: most when few of many candidates are kept, and can hurt abstention when
    #: many are). None = always rerank when a reranker is configured.
    rerank_max_top_k: int | None = Field(default=None, ge=1)
    #: H15: replay windows stay inside the hit's topic segment (lexical-cohesion
    #: boundaries within a session), so neighbours from another topic are not replayed.
    replay_topic_segments: bool = False
    #: H17: label search candidates relevant / related / irrelevant with the
    #: ``relevance`` LLM role and drop only "irrelevant" ones, always keeping the
    #: ``relevance_safety_net`` best-scored candidates (a yes/no filter loses gold).
    relevance_filter: bool = False
    #: RRF rank constant (k in 1/(k+rank)); None = the default 60. Graphiti uses 1.
    rrf_k: int | None = Field(default=None, ge=1)
    #: ContextPipe: tokens kept free for the reply inside the assembly budget.
    reply_reserve_tokens: int = Field(default=0, ge=0)
    #: The mode ``Engine.read()`` uses when the caller passes none. ``auto`` keeps the
    #: routed behaviour; the ``assistant`` template pins ``replay`` (combo-A, LoCoMo:
    #: compose routing measured -2.1, replay is the base of every winning arm).
    default_mode: Literal["auto", "full", "replay", "retrieve", "compose"] = "auto"
    #: Hindsight: prefix reranker inputs with ``[Date: YYYY-MM-DD]``.
    rerank_date_prefix: bool = False
    #: Agent Zero: skip the reranker for ordering questions (first / latest / ...).
    skip_rerank_for_ordering: bool = False
    #: H22 (Mastra): with ``render: dated``, mark long gaps ("[3 weeks later]").
    gap_markers: bool = False
    #: P4 (JustMem COMPOSE): the compose read adds up to two answer-free rewrites
    #: from the ``query_rewrite`` LLM role (``@compose`` prompt). Needs the role bound.
    compose_rewrites: bool = False
    #: H24: how ``read(mode="auto")`` picks a mode once full context does not fit:
    #: ``rules`` (deterministic cues) or ``decision`` (the decision provider chooses
    #: among compose / replay / retrieve; rules on any failure).
    planner: Literal["rules", "decision"] = "rules"
    relevance_safety_net: int = Field(default=10, ge=0)
    #: C8': resolve search hits on anticipatory cues (``Engine.add_cues``) to
    #: their target records. A cue below ``cue_min_trust`` is ignored, so cues
    #: from low-trust sources cannot redirect retrieval. Off: cues are invisible.
    anticipatory_cues: bool = False
    #: H1: annotate relative-time phrases in assembled/read records with the absolute
    #: date they denote, resolved against each record's event time (``valid_from``):
    #: "last Friday" -> "last Friday [= Fri 2023-07-14]". Deterministic rules, no model;
    #: stored content is never changed. Off: byte-identical.
    resolve_relative_dates: bool = False
    #: H11: assembly draws from ``candidate_pool x top_k`` search candidates, so
    #: the token budget, not a fixed K, decides how much evidence enters (LoCoMo:
    #: top-10 filled ~400 of 4,096 tokens). 1 = unchanged. Pair with
    #: ``assembly.relative_floor`` to keep precision.
    candidate_pool: int = Field(default=1, ge=1, le=10)
    #: H16: for ordering questions ("first", "latest", "most recent", ...) present
    #: the assembled volatile records in event-time order instead of score order.
    order_by_time_for_ordering: bool = False
    #: H5: ``dated`` prefixes each episodic/semantic record with its event date,
    #: ``[2023-05-08 Mon]``, after the stable prefix. ``plain`` = unchanged.
    render: Literal["plain", "dated"] = "plain"
    cue_min_trust: float = 0.5
    #: H22 (Mnemon): open the volatile context with a dated timeline per topic
    #: entity of the retrieved keyed facts: every live fact on that entity plus its
    #: superseded history, oldest first. Built at read time from the stored facts
    #: (no model, nothing stored); entries pass the same gates as any context record.
    topic_timelines: bool = False
    #: H22: how many entities get a timeline (best-scored first).
    timeline_entities: int = Field(default=3, ge=1)
    #: H22: the newest entries kept per timeline.
    timeline_items: int = Field(default=8, ge=2)
    #: H22: lead the context, right after the pinned persona, with preferences and
    #: standing requests the user stated ("from now on ...", "please always ...").
    #: Only user-role records at or above ``standing_min_trust``; shown as data the
    #: user stated, never as system instructions. Deterministic cue rules.
    standing_instructions: bool = False
    standing_min_trust: float = Field(default=0.7, ge=0.0, le=1.0)
    #: H22: the token sub-budget of the lead section (standing preferences, then
    #: timelines), taken out of the assembly budget.
    lead_budget_tokens: int = Field(default=400, ge=0)


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


class FirewallConfig(BaseModel):
    """Memory Firewall switches (E1 + B8). Defaults reproduce the pre-B8 firewall.

    - ``enabled``: ``false`` keeps trust scoring but disables flagging, anomaly
      checks and quarantine: the N1 ablation arm. Never use it in production.
    - ``redact_secrets``: replace cloud keys, tokens, JWTs, private keys,
      ``key=value`` credentials and emails with ``[REDACTED:<kind>]`` at write.
    - ``max_content_chars``: a non-privileged write longer than this is
      quarantined (size anomaly, a common bulk-injection signature).
    - ``protected_keys``: fact keys (``entity`` or ``entity.attribute``) only an
      operator or system source may write; others are quarantined.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    redact_secrets: bool = False
    max_content_chars: int | None = Field(default=None, ge=1)
    protected_keys: list[str] = Field(default_factory=list)
    #: H21 (G->D self-contamination): ``write_messages`` never deposits turns whose role
    #: is listed here (e.g. ``["system", "tool"]``).
    skip_message_roles: list[str] = Field(default_factory=list)
    #: H21: never re-deposit a turn that carries memspine's own assembly markers
    #: (recalled memory echoed back into the conversation).
    skip_injected_recall: bool = False
    #: H21: tag assistant turns ``assistant_claim``: a proposal, not an observed fact.
    tag_assistant_claims: bool = False


class IntegrityConfig(BaseModel):
    """Trust-horizon invariant (THI, formerly "MTI") for shared memory — opt-in, default OFF.

    Off, every path is byte-identical to the pre-MTI engine: shared reads keep
    the flat ``TRUST_RETRIEVED_CAP`` min-cap, ranking stays trust-blind, and
    ``derived_from`` parents are recorded but never lower trust.

    On:

    - ``write(..., derived_from=[ids])`` sets trust to
      ``min(base_trust, view_trust(parent) for each parent) * derivation_decay``
      (MTI-D). An unreadable parent counts as view trust 0.0 (fail closed).
    - A foreign record's view trust is ``trust ⊗ kappa`` for its grant edge, with
      ``⊗`` = ``attenuation`` (``product`` or ``min``). ``edge_kappa`` overrides
      per edge, keyed ``"grantor->grantee"``.
    - ``trust_weighted_ranking`` ranks by ``composite_score * view_trust``;
      ``admission_threshold`` drops candidates whose view trust is below it.
    - ``principal_bound_corroboration`` requires every corroborator to name a
      ``source.principal`` that differs from the held record's and from every
      earlier corroborator's.
    - ``merge_reinforcement_gate`` skips dedup-merge reinforcement when the
      incoming write is less trusted than the kept record.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    attenuation: str = "product"  # product | min
    kappa: float = Field(default=0.5, gt=0.0, le=1.0)
    edge_kappa: dict[str, float] = Field(default_factory=dict)
    derivation_decay: float = Field(default=1.0, gt=0.0, le=1.0)
    #: Baseline emulation ONLY (MAP-Graph-style verification bonus): multiplies
    #: derived trust and so can exceed the parents' minimum — it breaks the
    #: invariant on purpose, to measure what that costs. Never raise in production.
    verification_bonus: float = Field(default=1.0, ge=1.0, le=2.0)
    admission_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    trust_weighted_ranking: bool = True
    principal_bound_corroboration: bool = True
    merge_reinforcement_gate: bool = True
    #: B0: the engine records what each session READS and treats it as parents
    #: of the session's next writes, so omitting ``derived_from`` cannot launder
    #: (Paper A's A1/A2 become enforced, not assumed). ``turn``: the read ledger
    #: is consumed by each write; ``session``: kept until ``end_session``; ``off``.
    implicit_parents: str = "off"
    #: B6: assembled records whose view trust is below this are rendered inside
    #: an untrusted-data wrapper (data, not instructions). 0.0 = off.
    untrusted_wrap_below: float = Field(default=0.0, ge=0.0, le=1.0)
    #: B9 facts-only: a context record whose view trust is below this never enters
    #: a context window as raw text. It is replaced by the live atomic facts mined
    #: from it (H2), shown as unverified claims; with none, it is left out. Mined
    #: facts are themselves kept (they are claims already). Raw low-trust text is
    #: where instruction framing survives, so this trades recall for containment.
    #: 0.0 = off.
    claims_only_below: float = Field(default=0.0, ge=0.0, le=1.0)
    #: B4': re-check each candidate's trust against its CURRENT parents at read
    #: time. Ancestor quarantine or rollback propagates, and so does grant revocation:
    #: a parent behind a revoked grant counts 0 (N6, ADR-029 edge cases). Radii can
    #: only shrink. Costs extra storage reads per candidate.
    live_reevaluation: bool = False
    #: B7: per-principal reputation. A principal whose records were quarantined,
    #: or whose record was the SEED of a taint rollback, has its later writes'
    #: trust multiplied by min(1, 2 * Beta-mean(good, bad)) with a uniform prior:
    #: a new or clean principal keeps full trust, a bad history lowers it. It can
    #: only LOWER trust, so the per-hop gain stays below 1 and the horizon holds.
    principal_reputation: bool = False

    @field_validator("attenuation")
    @classmethod
    def _known_attenuation(cls, value: str) -> str:
        if value not in {"product", "min"}:
            raise ConfigError(f"integrity.attenuation must be 'product' or 'min', got {value!r}")
        return value

    @field_validator("implicit_parents")
    @classmethod
    def _known_implicit_mode(cls, value: str) -> str:
        if value not in {"off", "turn", "session"}:
            raise ConfigError(f"integrity.implicit_parents must be off|turn|session, got {value!r}")
        return value

    @field_validator("edge_kappa")
    @classmethod
    def _edge_kappa_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        for edge, kappa in value.items():
            if "->" not in edge or not 0.0 < kappa <= 1.0:
                raise ConfigError(f"integrity.edge_kappa[{edge!r}] must be 'a->b' with 0<kappa<=1")
        return value


class DecisionConfig(BaseModel):
    """H24: the optional decision provider (calibrated choice among described options,
    no generation). ``off`` = none; ``gliner2`` uses the ``[ner]`` extra."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["off", "gliner2"] = "off"
    model: str = "fastino/gliner2-base-v1"


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
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
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
