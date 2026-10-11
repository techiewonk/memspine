"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memspine.config import constants
from memspine.core.events import EventLogMode
from memspine.core.namespace import validate_namespace
from memspine.core.registry import validate_types
from memspine.exceptions import ConfigError

__all__ = [
    "AuditConfig",
    "CacheConfig",
    "ConsentConfig",
    "EmbeddingConfig",
    "EncryptionConfig",
    "EventLogConfig",
    "GraphConfig",
    "LLMConfig",
    "LLMRoleConfig",
    "MemoryTypeConfig",
    "MemspineConfig",
    "NamespaceConfig",
    "PromptsConfig",
    "ReadConfig",
    "RestAuthConfig",
    "RestConfig",
    "RetentionClassConfig",
    "RetentionConfig",
    "StorageConfig",
    "VectorConfig",
]


class EventLogConfig(BaseModel):
    """At-rest lifetime of the memory_events log (D-45)."""

    model_config = ConfigDict(extra="forbid")

    mode: EventLogMode = EventLogMode.FULL
    retention_days: int = Field(default=constants.EVENT_LOG_RETENTION_DAYS, ge=1)
    compress: bool = False


class EncryptionConfig(BaseModel):
    """Encryption at rest for the SQLite event log and read model (#52, ADR-035).

    ``none`` (default) leaves the database file plain. ``sqlcipher`` opens every
    connection through SQLCipher (``pip install memspine[encrypt]``; a missing
    driver fails with ``MissingServiceError``), keyed with the passphrase held in
    the environment variable named by ``key_env``. The key is read from that
    variable only, never from config, and never logged. Derived stores outside the
    SQLite file (LanceDB vectors, the Tantivy index, disk caches) are not
    encrypted by this option; ADR-035 lists them."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "sqlcipher"] = "none"
    key_env: str | None = None

    @model_validator(mode="after")
    def _key_env_named(self) -> EncryptionConfig:
        if self.mode == "sqlcipher" and not self.key_env:
            raise ConfigError(
                "storage.encryption.mode=sqlcipher requires storage.encryption.key_env "
                "(the NAME of the environment variable holding the key)"
            )
        return self


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
    #: #52: SQLCipher encryption at rest (sqlite backend only); off by default.
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)


class EmbeddingConfig(BaseModel):
    """Embedder defaults (D-08). ``hash`` is the deterministic zero-network
    provider for tests/CI; ``fastembed`` (ONNX, CPU) is the production default;
    ``static`` is the cheap model2vec table (``[static]``, E4/ADR-020)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "fastembed"  # fastembed | hash | static | litellm | st
    model: str = "BAAI/bge-small-en-v1.5"
    #: REQUIRED when provider=litellm — a cloud embedder's output dimension is
    #: not locally discoverable, and the vector store needs it up front.
    dim: int | None = None
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None  # bedrock
    #: st only: torch device ("cuda", "cpu"; None = library default) and weight dtype
    #: ("bfloat16", "float16"; None = checkpoint default).
    device: str | None = None
    dtype: str | None = None
    #: st only: sentence-transformers prompt names for queries / documents (Jina v5:
    #: "query" / "document"), and whether the repo's custom code may run.
    query_prompt_name: str | None = None
    document_prompt_name: str | None = None
    trust_remote_code: bool = False
    #: litellm only: ask the model for exactly ``dim`` dimensions (Matryoshka
    #: models: Cohere embed-v4 256/512/1024/1536, Titan v2, OpenAI v3). Off =
    #: the model's default size, which must then equal ``dim``.
    request_dimensions: bool = False
    #: litellm only: asymmetric retrieval models (Cohere: ``search_query`` /
    #: ``search_document``). None = the provider default for both.
    query_input_type: str | None = None
    document_input_type: str | None = None
    #: G9: the most texts one embedding call carries when ``write_messages``
    #: embeds its turns up front (Cohere on Bedrock accepts up to 96).
    batch_size: int = Field(default=32, ge=1)
    #: N64 (plan v3.2 gaps): fastembed only: a text prepended to every retrieval
    #: QUERY before embedding (documents unchanged, so no re-index). The BGE v1.5
    #: models are trained with ``BGE_QUERY_INSTRUCTION`` (``config/constants.py``);
    #: fastembed's own ``query_embed`` does not add it. None = queries embed like
    #: documents (unchanged).
    query_instruction: str | None = None


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
    #: I1 (isolation review 2026-10-08): keep a BITMAP scalar index on the vector
    #: table's ``namespace`` column (rebuilt every ``NAMESPACE_INDEX_EVERY`` writes once
    #: the table has ``NAMESPACE_INDEX_MIN_ROWS`` rows), so the per-user prefilter
    #: reads one user's rows. Results are unchanged. Off: the column is scanned.
    namespace_index: bool = False
    #: I4 (isolation review 2026-10-08): ``shared`` (unchanged: one table per embedder,
    #: namespace prefilter) or ``per_namespace`` (one table per namespace: a search
    #: touches one user's rows only, and ``erase_namespace`` drops the user's table).
    #: Switching rebuilds the vector projection from the event log.
    isolation: Literal["shared", "per_namespace"] = "shared"


class GraphConfig(BaseModel):
    """Graph store selection (D-26). ``sqlite_adjacency`` is the zero-dep
    default and fallback; ``ladybug`` (the maintained Kùzu fork, ``[graph]``) is
    the graph engine for graph features (ADR-034); ``kuzu`` is a deprecated alias
    for one release (Kùzu was archived on 2025-10-10; ``DeprecationWarning``).
    The store is only constructed when associative memory
    is enabled or this block is set explicitly — ``profile="simple"`` never
    touches it."""

    model_config = ConfigDict(extra="forbid")

    #: GR-1 (ADR-064): ``auto`` (default) = LadybugDB when the ``ladybug`` package is
    #: installed (``[graph]``), else ``sqlite_adjacency``. Explicit values pin one.
    provider: str = "auto"  # auto | sqlite_adjacency | ladybug | kuzu (deprecated) | neo4j
    #: GR-3 (graph engine plan 2026-10-08): embed entity-node names (GP-2 entity nodes)
    #: into the graph store's entity index: LadybugDB keeps them in a FLOAT[] column with
    #: its native HNSW vector index and FTS index; sqlite_adjacency in node properties.
    entity_embeddings: bool = False


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


class StructuredConfig(BaseModel):
    """I69: recovery for a structured-output reply that fails validation. Both default
    off (measure first: ``Engine.structured_stats()``)."""

    model_config = ConfigDict(extra="forbid")

    #: Re-prompt ONCE with the validation error appended.
    retry_on_error: bool = False
    #: On that retry only, ask the backend for JSON-schema constrained decoding
    #: (``response_format`` json_schema: OpenAI-compatible servers, Ollama via LiteLLM,
    #: llama.cpp); a backend that refuses it falls back to a plain retry.
    constrained_retry: bool = False


class LLMConfig(BaseModel):
    """Per-role providers: extract / judge / chat (M14). Roles absent here are
    disabled; the engine only requires them when a feature needs the role."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)
    structured: StructuredConfig = Field(default_factory=StructuredConfig)


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
    #: N58 (gaps plan 2026-10-08): the Tantivy analyzer for the BM25 body. ``default``
    #: (unchanged: alphanumeric runs, lower-cased) or ``english`` (plus English stop
    #: words and the Snowball English stemmer, so "camping" matches "camp"). The
    #: ``english`` index lives in its own directory under its own projector name, so
    #: switching rebuilds it from the event log and never touches the default index.
    lexical_analyzer: Literal["default", "english"] = "default"
    #: N30 (write side; Hindsight indexes dates with the text): the BM25 index also
    #: holds each record's date words (the day it was said and the days it names:
    #: "2023-05-07 7 May 2023 Sunday"), so a question naming a date matches lexically.
    #: Own index and projector name (``lexical:dates``): turning it on rebuilds.
    lexical_dates: bool = False
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
    #: F2 (plan v3.2): with ``temporal_leg``, a question with no absolute date but a
    #: relative phrase ("what did we discuss last week?") gets its span resolved
    #: against the read time (the engine clock) by the H1 rules, using
    #: ``relative_week``. Off: relative phrases name no span (unchanged).
    temporal_relative: bool = False
    #: N45 (gaps plan 2026-10-08): with ``temporal_leg``, a date with no year ("on
    #: 7 May", "in March") takes the latest year that puts it on or before the
    #: namespace's newest record (or the as-of time). Off: such dates name no span.
    temporal_infer_year: bool = False
    #: N61: order of in-span records in the temporal leg. ``midpoint`` (unchanged:
    #: closest to the span's middle) or ``overlap`` (most content words shared with
    #: the question first, then midpoint).
    temporal_rank: Literal["midpoint", "overlap"] = "midpoint"
    #: N44: when fewer than ``fetch_k`` records fall in the span, fill the temporal leg
    #: with the nearest records outside it (within one span length, at least
    #: ``TEMPORAL_SOFT_MARGIN_DAYS``). Off: hard window (unchanged).
    temporal_soft: bool = False
    #: N30 (Hindsight: dates indexed with the text): with ``temporal_leg``, a turn
    #: whose text names a date ("last weekend", "on 7 May"), resolved against the
    #: turn's own time, also enters the leg when that date overlaps the question's
    #: span. Read-side only: nothing stored changes. Off: unchanged.
    temporal_leg_mentions: bool = False
    metadata_leg: bool = False
    #: W8 (plan v3.2): an RRF leg of the turns of every speaker the question names
    #: (``speaker:<name>`` tags from ``memories.episodic.policies.subject_tagging``),
    #: ranked by content-word overlap with the question. A boost, never a filter.
    #: Off: unchanged.
    subject_leg: bool = False
    #: W11 (plan v3.2): assistant-side memory. Assistant turns that recommend something
    #: are tagged ``recommendation`` at ``write_messages``; a question about what the
    #: assistant said ("what did you recommend", "your suggestion") gets an RRF leg of
    #: the assistant's own turns, recommendations first. Off: unchanged.
    role_aware: bool = False
    #: N05 (plan v3.2, EverMemOS ``amaxsim``, lexical variant): an RRF leg ranking records
    #: by their best single sentence (shared content words / sqrt(sentence length)), so a
    #: strong sentence inside a long turn is not diluted. No model. Off: unchanged.
    sentence_leg: bool = False
    #: H13: an extra BM25 leg over the question's core terms (interrogative and
    #: function words removed), fused by RRF. Needs the lexical store (hybrid).
    core_terms_leg: bool = False
    #: H18: run the reranker only when ``top_k`` is at most this (reranking helps
    #: most when few of many candidates are kept, and can hurt abstention when
    #: many are). None = always rerank when a reranker is configured.
    rerank_max_top_k: int | None = Field(default=None, ge=1)
    #: G5b: with a reranker and ``candidate_pool > 1``, keep only the best
    #: ``rerank_keep`` candidates after reranking, before assembly fills the budget,
    #: so a wider pool sharpens the ranking instead of growing the context.
    #: None = keep the whole pool (unchanged).
    rerank_keep: int | None = Field(default=None, ge=1)
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
    #: N41 (Dakera): None (unchanged) = the reranker's relevance replaces the retrieval
    #: score; a weight in [0, 1] blends them: ``w * rerank + (1 - w) * retrieval``,
    #: each min-max normalised over the candidates.
    rerank_blend: float | None = Field(default=None, ge=0.0, le=1.0)
    #: N41 confidence gate: when the reranker's best raw score is below this, it is
    #: not confident any candidate answers, and the retrieval order is kept. None = off.
    rerank_gate: float | None = None
    #: N42 (MemMachine): the reranker sees each candidate with this many neighbouring
    #: turns of its session on each side, so a short reply is judged with its question.
    #: 0 = the candidate alone (unchanged).
    rerank_context: int = Field(default=0, ge=0, le=3)
    #: GR-15 (Graphiti balanced shortlist): with a reranker on, the pool cut before
    #: reranking takes each leg's best hits in turn, so one leg cannot fill it.
    rerank_balanced: bool = False
    #: I75a (leg-protected pool): each leg's top-N hits are guaranteed a slot in the pool sent to
    #: the reranker even when their fused (RRF) rank falls outside the pool size; the rest is
    #: filled by RRF. The pool grows by at most ``legs * N``. 0 = off (unchanged).
    pool_protect_per_leg: int = Field(default=0, ge=0, le=20)
    #: R02 / I75 v2: how ``pool_protect_per_leg`` counts. ``per_leg`` (default) = I75a: every leg
    #: protects its own top-N, so the pool grows. ``source_family`` groups legs into independent
    #: families (lexical; semantic = vector + perspective + other vector-derived legs; temporal;
    #: other), protects the top-N per FAMILY with correlated votes counted once, and keeps the
    #: pool at its normal size by evicting the lowest-RRF unprotected candidates.
    pool_protect_mode: Literal["per_leg", "source_family"] = "per_leg"
    #: G-10 (Graphiti MMR): None = off; a lambda in [0, 1] reorders the final hits by
    #: maximal marginal relevance on embeddings (1 = relevance only, 0 = diversity only).
    mmr_lambda: float | None = Field(default=None, ge=0.0, le=1.0)
    #: N12 (plan v3.2, EverMemOS): the task instruction an instruction-conditioned
    #: reranker (``rerank: qwen3``) judges with; None keeps its memory default.
    rerank_instruction: str | None = None
    #: ``rerank: qwen3`` only: torch device (None = CPU float32) and weight quantisation
    #: ("4bit" | "8bit" via bitsandbytes, GPU only; None = full precision).
    rerank_device: str | None = None
    rerank_quant: Literal["4bit", "8bit"] | None = None
    #: I9: None (default, unchanged) = a long record is cut by the reranker's own context
    #: limit (the tail is dropped). N = records longer than N characters are scored in
    #: overlapping windows of N characters and take the max window score, so a long assistant
    #: turn is judged on its best passage. ``rerank_chunk_overlap`` = characters shared by
    #: neighbouring windows.
    rerank_chunk_chars: int | None = Field(default=None, ge=64)
    rerank_chunk_overlap: int = Field(default=0, ge=0)
    #: I7: a source with no timestamps (ConvoMem, PrefEval, LaMP shapes) gets
    #: ``valid_from`` = the write clock, so every rendered line would show today's date. On:
    #: a write whose event time was not supplied is tagged `ts_defaulted`, and the date
    #: prefix / relative-date annotation / rerank date prefix / timeline date skip it. Only
    #: records written with this key on are affected. Off: byte-identical.
    skip_defaulted_dates: bool = False
    #: F5: the zone a NAIVE datetime (a ``valid_from`` / message ``timestamp`` without an
    #: offset) is read in, an IANA name. Default ``UTC`` (as always); a warning is logged once
    #: per engine whenever a naive value is seen, because labelling dataset-local wall time as
    #: UTC shifts events near midnight onto the wrong day. Aware values are never touched.
    naive_timezone: str = "UTC"
    #: F5: when several turns of one session carry the SAME event time (a day-level or
    #: session-level stamp), each later one is moved by one more microsecond so the event-time
    #: order within the session is the write order everywhere (not only where ``recorded_at``
    #: breaks the tie). Off: stamps are stored as given.
    session_sequence: bool = False
    #: I23: `on` = the regex features that only know English (query shape, temporal phrases,
    #: month names, set nouns) do not fire on text a cheap script/stopword check judges
    #: non-English; they fail closed. Off: unchanged.
    language_guard: Literal["off", "on"] = "off"
    #: Agent Zero: skip the reranker for ordering questions (first / latest / ...).
    skip_rerank_for_ordering: bool = False
    #: H22 (Mastra): with ``render: dated``, mark long gaps ("[3 weeks later]").
    gap_markers: bool = False
    #: P4 (JustMem COMPOSE): the compose read adds up to two answer-free rewrites
    #: from the ``query_rewrite`` LLM role (``@compose`` prompt). Needs the role bound.
    compose_rewrites: bool = False
    #: H24: how ``read(mode="auto")`` picks a mode once full context does not fit:
    #: ``rules`` (deterministic cues), ``decision`` (the decision provider chooses
    #: among compose / replay / retrieve) or ``llm`` (G2a: one ``plan`` role call
    #: returns a ReadPlan; lookup/replay read by replay, aggregate by compose with
    #: the plan's subqueries as extra probes). Rules on any failure.
    planner: Literal["rules", "decision", "llm"] = "rules"
    #: G2b: with ``planner: decision``, a choice whose confidence is below this
    #: does not route the read: it keeps the default ``replay`` (retrieve when no
    #: hit is episodic). 0.0 = every choice routes (unchanged).
    planner_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: #35: with ``planner: llm``, ``v2`` selects the ``plan@v2`` prompt, which also
    #: writes one or two evidence-seeking subqueries for lookup questions ("Is X
    #: religious?" -> "church", "faith"); the lookup read then fuses them by RRF as
    #: extra search legs. ``v3`` (#36) selects ``plan@v3``: v2 plus the people the
    #: question is about (``persons``) and its time expression (``time_expr``), which
    #: feed the persons / time leg (``person_time_leg_k``). ``v1``: unchanged.
    planner_version: Literal["v1", "v2", "v3"] = "v1"
    #: G2c: compose results get the same +-``replay_window`` neighbour expansion as
    #: replay mode (nearest first, within the budget), so routing an aggregation
    #: question to compose no longer loses the turns around each hit.
    compose_replay: bool = False
    #: G11: the ``top_k`` of a read that ``read(mode="auto")`` routes to compose (the
    #: LLM planner's ``aggregate``, the decision planner's or the rules' compose), so
    #: list and count questions whose evidence spans sessions pool more candidates.
    #: The budget still caps the context. None = the caller's ``top_k`` (unchanged).
    aggregate_top_k: int | None = Field(default=None, ge=1)
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
    #: G13: with ``resolve_relative_dates``, state week-level phrases relative to the
    #: record's own day, LoCoMo's convention: "last week [= the week before 2023-06-09
    #: (2023-06-02..2023-06-08)]" instead of the previous calendar week; "last weekend",
    #: "last Friday", "N weeks ago" name the relation too, and "a few days ago" gets
    #: "[= a few days before <day>]". Months, years and days are unchanged. Off:
    #: byte-identical.
    relative_dates_anchored: bool = False
    #: #58: with ``resolve_relative_dates``, the span of "last/past week" and "next
    #: week": ``calendar`` (the previous / next Monday-to-Sunday week, unchanged) or
    #: ``preceding_7_days`` (the seven days before / after the record's own day,
    #: LoCoMo's "the week before <session date>"). Also used by
    #: ``consolidation.mine_event_dates``.
    relative_week: Literal["calendar", "preceding_7_days"] = "calendar"
    #: C5: with ``resolve_relative_dates``, also annotate unambiguous durations against
    #: the record's own day: "for 3 years" -> "[= since about 2020]", "a month now" ->
    #: "[= since about 2023-04]", "since 2019" -> "[= about 4 years as of 2023-05-20]".
    #: Always ``about``; vague spans ("for years", "for a while") are left alone.
    #: Off: byte-identical.
    resolve_durations: bool = False
    #: I79: with ``resolve_relative_dates``, also annotate a bare ``on <weekday>`` against the
    #: record's own day, by the sentence's tense: "won it on Friday" -> "[= the Friday before
    #: 2022-07-10 (Fri 2022-07-08)]" (anchored form with ``relative_dates_anchored``). No clear
    #: tense, the plural and the anchor's own weekday are left alone. Off: byte-identical.
    relative_dates_weekdays: bool = False
    #: I79: with ``resolve_relative_dates``, write each date label as "[= event <date>]" so the
    #: date the event happened reads distinct from the line's leading said-date. Duration
    #: labels are unchanged. Off: byte-identical.
    relative_dates_happened: bool = False
    #: H11: assembly draws from ``candidate_pool x top_k`` search candidates, so
    #: the token budget, not a fixed K, decides how much evidence enters (LoCoMo:
    #: top-10 filled ~400 of 4,096 tokens). 1 = unchanged. Pair with
    #: ``assembly.relative_floor`` to keep precision.
    candidate_pool: int = Field(default=1, ge=1, le=10)
    #: RETRIEVAL_GAPS finding 4: the replay / compose neighbour window is a fixed
    #: +-2 turns (the ``window`` argument of ``Engine.read``), but 63% of the gold the
    #: window supplies lies AFTER the hit, so a 2-before / 4-after window beats a
    #: symmetric 3 at equal cost. These two override the window per side (turns of the
    #: hit's session). ``None`` = that side keeps the ``read`` argument (default 2),
    #: byte-identical; ``0`` = no neighbours on that side. ``read(replay_window=0)``
    #: still switches the compose expansion off.
    replay_window_before: int | None = Field(default=None, ge=0)
    replay_window_after: int | None = Field(default=None, ge=0)
    #: I6 / I20: the window counted in TURNS suits short chat turns; with long assistant
    #: turns or a very long history one neighbour can be thousands of tokens. ``tokens``
    #: expands each hit's neighbours nearest first, per side, until that side's token
    #: allowance (``replay_window_tokens_before`` / ``_after``, per hit, 1:2 like the
    #: turn window) is used; a neighbour that would pass it closes that side. ``turns`` =
    #: today's behaviour, byte-identical.
    replay_window_unit: Literal["turns", "tokens"] = "turns"
    replay_window_tokens_before: int = Field(default=256, ge=0)
    replay_window_tokens_after: int = Field(default=512, ge=0)
    #: I20: scale the window and the candidate pool to the budget the routed read has
    #: left. With ``f = min(1, budget / replay_budget_reference)`` (floored at 0.25) the
    #: turn window, the token allowances and ``top_k`` shrink by ``f`` (a side that had a
    #: neighbour keeps at least one turn; ``top_k`` keeps at least 1). Off: unchanged.
    replay_budget_scaling: bool = False
    replay_budget_reference: int = Field(default=4096, ge=1)
    #: I20: replay admits every hit first (best score first) and only then the neighbours,
    #: so a neighbour of the first hit can never push a lower-ranked hit out of the
    #: budget. Off: each hit is followed at once by its own window (today's order).
    replay_hits_first: bool = False
    #: I31: near-duplicate removal among the candidates before assembly. ``exact`` =
    #: equal text; ``jaccard`` = content-word Jaccard >= ``dedupe_threshold``;
    #: ``embedding`` = record-vector cosine >= ``dedupe_threshold`` (a record with no
    #: vector falls back to jaccard). ``dedupe_keep``: the ``best``-scored copy stays, or
    #: the ``earliest`` (by event time, with the better score). The dropped pairs are in
    #: ``search_forensics()["dedupe_dropped"]``. The pinned persona is never dropped.
    dedupe: Literal["off", "exact", "jaccard", "embedding"] = "off"
    dedupe_threshold: float = Field(default=0.9, gt=0.0, le=1.0)
    dedupe_keep: Literal["best", "earliest"] = "best"
    #: I33: a profile / preference line is shown only when it bears on the question, not
    #: whatever the question is (MemOS injects up to 6 preferences at threshold 0.0).
    #: ``overlap`` = the line shares at least one content word with the question (the
    #: slots header, and the profile header's insights). ``off`` = today's behaviour.
    #: The decider's relevance check will replace the overlap test through
    #: ``Engine._profile_line_relevant``; until then it is the interface seam.
    profile_relevance_gate: Literal["off", "overlap"] = "off"
    #: RETRIEVAL_GAPS finding 3: a reranked candidate list is min-max normalised
    #: (best 1.0, worst 0.0), so ``assembly.relative_floor`` then drops on average half
    #: of the hits (and their replay windows) however relevant the reranker found them.
    #: ``minmax`` = unchanged. ``skip`` = reranked reads do not apply the relative
    #: floor (the reranker, ``rerank_keep`` and the budget bound the context instead);
    #: a read the reranker did not score (off, gated, failed) still applies it. A
    #: ``raw`` mode was left out: reranker outputs are only calibrated 0-1 for some
    #: models, and the floor would still multiply the composite score.
    rerank_floor: Literal["minmax", "skip"] = "minmax"
    #: H16: for ordering questions ("first", "latest", "most recent", ...) present
    #: the assembled volatile records in event-time order instead of score order.
    order_by_time_for_ordering: bool = False
    #: H5: ``dated`` prefixes each episodic/semantic record with its event date,
    #: ``[2023-05-08 Mon]``, after the stable prefix. ``plain`` = unchanged.
    render: Literal["plain", "dated"] = "plain"
    cue_min_trust: float = 0.5
    #: #61 (ADR-050): the read-time query encoder. ``none`` = no encoder (reads are
    #: byte-identical). ``cues`` matches the query against stored anticipatory cues
    #: (H8 / ``add_cues``) by content-word overlap, no model, and adds the cued
    #: records as one more fused retrieval leg; matches respect ``cue_min_trust``
    #: and the cued records pass every read gate. Independent of
    #: ``anticipatory_cues`` (which searches cue text directly).
    query_encoder: Literal["none", "cues"] = "none"
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
    #: N18 (plan v3.2): the standing-preference cues. ``narrow`` (unchanged): requests
    #: aimed at the assistant ("from now on", "please always", "call me").
    #: ``wide``: also first-person evaluative forms ("I avoid / can't stand / am
    #: allergic to / I'm vegetarian / my favourite"). PrefEval explicit preferences:
    #: narrow 11.3%, wide 83.1%; LoCoMo turns matched: 0.4% vs 4.8%.
    standing_patterns: Literal["narrow", "wide"] = "narrow"
    #: H22: the token sub-budget of the lead section (standing preferences, then
    #: timelines), taken out of the assembly budget.
    lead_budget_tokens: int = Field(default=400, ge=0)
    #: G1b (JustMem cards): ``header`` opens the volatile context with the mined
    #: atomic facts relevant to the query, one dated line each, retrieved by the same
    #: hybrid search restricted to ``atomic_fact`` records and gated like any record.
    #: The block stays within ``cards_budget_share`` of the budget; the rest goes to
    #: the normal read, which then leaves mined facts out (no fact twice). ``off``:
    #: byte-identical.
    cards: Literal["off", "header"] = "off"
    cards_budget_share: float = Field(default=0.25, gt=0.0, le=1.0)
    cards_top_k: int = Field(default=10, ge=1)
    #: Smoke 2026-10-05: cards carry the miner's own, often wrong, absolute dates and
    #: cost temporal questions -16; skip the cards header for date questions
    #: (``query_shape.is_temporal``) so the H1-resolved raw turns answer them.
    cards_skip_temporal: bool = False
    #: #29: a card whose mined fact carries a happened date
    #: (``consolidation.mine_event_dates``) that differs from the day it was said
    #: renders ``[said d1 · happened d2]``. Off: byte-identical.
    cards_event_date: bool = False
    #: G3b: after the cards header, an "about" block of the H14 profile insights
    #: (``consolidation.reflect_profile`` records) on the people the query names, or
    #: the most relevant insights when none matches, within ``profile_budget_share``
    #: of the budget. Gated like any record. Off: byte-identical.
    profile_header: bool = False
    profile_budget_share: float = Field(default=0.15, gt=0.0, le=1.0)
    #: E3: for count questions ("how many times ...", ``query_shape.is_count``), lead
    #: the context with an "Occurrences (dated):" block: the distinct dated mentions of
    #: the counted event among the episodic records the read retrieved, one line each
    #: (same-day mentions of one event count once), within ``count_budget_share`` of the
    #: budget, which the read gives up for it. Off: byte-identical.
    count_timeline: bool = False
    count_budget_share: float = Field(default=0.1, gt=0.0, le=1.0)
    #: #60: with ``count_timeline``, mentions of one event are merged before they are
    #: listed: a mention's day is the event day its single-day relative phrase names
    #: ("yesterday", "last Friday", H1 rules), else the day it was said, and two
    #: mentions on the same event day with high word overlap are one occurrence, even
    #: when said on different days. Off: byte-identical.
    count_dedupe: bool = False
    #: GP-3 (#14): fuse a graph leg into the RRF ranking: from the entities the
    #: query names (else those of the best 3 hits of the other legs), walk the
    #: association graph ``graph_depth`` entity hops to fact records and their
    #: source turns. Needs associative memory with ``entity_nodes``. Off:
    #: byte-identical.
    graph_leg: bool = False
    graph_depth: int = Field(default=2, ge=1, le=3)
    graph_leg_k: int = Field(default=10, ge=1)
    #: GP-10 (#16): a graph walk never enters a record below this trust (nor a
    #: quarantined one). Default: the firewall's quarantine threshold.
    graph_min_trust: float = Field(default=constants.GRAPH_MIN_TRUST_DEFAULT, ge=0.0, le=1.0)
    #: GP-5 (#15): a graph facts block after the cards: the edge facts reached from
    #: the entities the query names, with validity ranges and source counts, within
    #: ``cards_budget_share`` (shared with the cards header). Off: byte-identical.
    cards_include_edges: bool = False
    #: GP-4/KB-4/GR-19 (#22): rerank the gated candidates by graph proximity to
    #: the graph leg's seeds (the entities the query names, else those of the best
    #: hits): ``distance`` = 1 / entity hops of the seed walk, ``ppr`` = local
    #: push-PPR over the seeds' subgraph (normalised to the best record). Each
    #: boost, and an episode-mentions boost (facts restated by more episodes,
    #: ``edge_source:`` provenance), lifts relevance as ``r + w * b * (1 - r)``
    #: with ``w = graph_rerank_weight``; unboosted candidates keep their score.
    #: Needs associative memory with ``entity_nodes`` for the graph part. Off:
    #: byte-identical.
    graph_rerank: Literal["off", "distance", "ppr"] = "off"
    graph_rerank_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    #: W12 (plan v3.2, ADR-061): before the cut, walk the ``because`` (effect ->
    #: cause) and ``reply_to`` (reply -> answered message) edges from the best
    #: ``CAUSAL_WALK_SEEDS`` candidates up to ``causal_walk_hops`` hops. A reached
    #: record joins (or is lifted to) ``seed relevance x CAUSAL_WALK_DECAY ** hops``
    #: and then passes every search gate. ``why``: only for questions asking for a
    #: cause ("Why ...", "What made ..."); ``always``: every read. Edges come from
    #: ``memories.associative.policies.rule_edges`` and ``write(reply_to=)``;
    #: needs associative memory. Off: byte-identical.
    causal_walk: Literal["off", "why", "always"] = "off"
    causal_walk_hops: int = Field(default=2, ge=1, le=3)
    #: G27 (plan v3.2, ADR-061): in a replay read, a turn written with ``reply_to``
    #: also shows the message it answers (gated like any replayed neighbour, within
    #: the budget). Off: byte-identical.
    reply_links: bool = False
    #: GP-6 (#17): an "About <Name>: …" block of the entity summaries
    #: (``summarize_entities`` stage) of the entities the query names (the graph
    #: leg's seeds), within ``cards_budget_share`` after the cards and the graph
    #: facts. Off: byte-identical.
    entity_summaries: bool = False
    #: GP-9 (#23): community summaries (``reorganize`` parents) are read only when
    #: a seed entity of the graph leg is a member: one of the records it mentions
    #: belongs to the community. Others never reach the context; with
    #: ``graph_leg`` on, the admitted ones join the graph leg. Off: byte-identical.
    graph_communities: bool = False
    #: #36: with ``planner: llm`` and ``planner_version: v3``, the routed read fuses a
    #: structured leg by RRF: records whose ``valid_from`` lies in the span the plan's
    #: ``time_expr`` names (H1 rules) and/or that are about the plan's ``persons``
    #: (``person:`` tags, else the record's entity), at most this many.
    person_time_leg_k: int = Field(default=10, ge=1)
    #: #38: for aggregate / list / count reads routed to compose, ask the ``sufficiency``
    #: role (else ``plan``) whether the context is complete (+1 call) and, when it is
    #: not, for up to three missing-information queries (+1 call, ``sufficiency@missing``);
    #: the compose read then runs once more with them as extra probes. One round at most.
    #: Off: no call, byte-identical.
    completeness_check: bool = False
    #: G-17 (SimpleMem multi-round reflection): how many completeness rounds a checked
    #: compose read may run (each one sufficiency + one missing-queries call). 1 = #38.
    completeness_rounds: int = Field(default=1, ge=1, le=3)
    #: #40: the profile header packs, within ``profile_header_budget`` tokens (at most
    #: half the read budget), the session summaries, then the profile observations
    #: (H14 insights), then the best other hits for the query, one dated, escaped line
    #: each, in a fixed section order. Independent of ``profile_header``. Off:
    #: byte-identical.
    profile_header_packing: bool = False
    profile_header_budget: int = Field(default=300, ge=1)
    #: ADR-055 question-shape gates on the replay read path (all off by default).
    #: A1: a ``replay`` read of a list or count question (``query_shape.is_aggregation``
    #: / ``is_count``) retrieves ``aggregate_top_k`` candidates (when set) through the
    #: normal replay path; no compose rendering. The budget still caps the context.
    aggregate_in_replay: bool = False
    #: A2: the cards header shows #30 list cards only to list and count questions.
    list_cards_only_aggregate: bool = False
    #: With ``cards: header`` and ``cards_skip_temporal``, a date question gets no cards
    #: header, and then (with this key) no mined fact either: it reads raw turns, as
    #: with mining off. Off: mined facts compete in its raw read (measured to push out
    #: dated turns on LoCoMo temporal questions).
    cards_skip_hides_facts: bool = False
    #: ADR-055 addendum: with ``cards: header``, the cards header (mined facts and
    #: list cards) is built only for list and count questions; any other question
    #: that is not a date question also gets no mined fact (``atomic_fact``) in its
    #: routed read, so it reads raw turns only. Date questions keep the
    #: ``cards_skip_temporal`` / ``cards_temporal`` behaviour. The rule holds in every
    #: read mode, ``full`` included (as with mining off). Off: byte-identical.
    cards_only_aggregate: bool = False
    #: B2: with ``temporal_leg``, a record whose ``happened:`` date (a mined fact's
    #: event date) overlaps the query's date span also enters the temporal leg.
    temporal_leg_event_dates: bool = False
    #: B3: the cards header on date/time questions (``query_shape.is_temporal``):
    #: ``skip`` = today's behaviour (no cards when ``cards_skip_temporal``);
    #: ``event_dates`` = only cards with a ``happened:`` date, rendered
    #: ``[happened d · said d']``, whatever ``cards_skip_temporal`` says.
    cards_temporal: Literal["skip", "event_dates"] = "skip"
    #: D1: no profile header (plain or packed) on date/time questions.
    profile_skip_temporal: bool = False
    #: W9 (plan v3.2, OP-Bench): the profile header (plain or packed) only for questions
    #: about the asker, a choice they face, or a named person (``is_personal`` or a
    #: name in the query); a general-knowledge question ("What is the capital of
    #: France?") gets none, so the profile is not injected where it does not apply.
    #: Standing instructions (style requests) are unaffected. Off: unchanged.
    profile_scope_gate: bool = False
    #: G32 (plan v3.2): a request for something new (``is_novelty``: "a book I haven't
    #: read", "something different") gets a header of what memory says the asker
    #: already likes / does / has (keyed likes, activities, favourite_*, pets facts and
    #: list cards), so the answer avoids repeats. Off: unchanged.
    novelty_exclusions: bool = False
    #: W5 (plan v3.2): the Λ-profile header: the current keyed STATE facts (rule- or
    #: LLM-mined: home, origin, job, favourites, attitudes …) of each person the
    #: question names, else of ``user``; within ``profile_budget_share``. Off: none.
    profile_slots_header: bool = False
    #: W5 / W16: include slots tagged ``sensitive:*`` in that header (default: never).
    profile_sensitive: bool = False
    #: N10 (plan v3.2): a duration question ("how long after ...", "how many weeks
    #: between ...") gets one engine-computed line from the two best-matching dated
    #: turns: "A (date) -> B (date): N days (about W weeks, M months)". Off: none.
    span_line: bool = False
    #: N16 (plan v3.2, LaMP RSPG): per-leg RRF weights for this deployment's task
    #: (``{"vector": w, "lexical": w, "extra": w}``; missing keys weigh 1). Empty:
    #: unchanged. With ``recency_leg``, a recency-only leg (newest records first) joins
    #: the fusion as an extra leg, for tasks where the latest behaviour matters most.
    leg_weights: dict[str, float] = Field(default_factory=dict)
    #: N62 (Dakera per-route weights): leg weights by question shape (``temporal``,
    #: ``count``, ``ordering``, ``inference``, ``plain``; ``query_shape.question_shape``),
    #: applied over ``leg_weights``. Named legs: ``vector``, ``lexical``, ``temporal``,
    #: ``entity``; every other extra leg is ``extra``. Empty: unchanged.
    leg_weights_by_shape: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: N52 (Mem0 / Dakera calibrated fusion): how the retrieval legs are fused. ``rrf``
    #: (unchanged, reciprocal rank) or ``minmax`` (each leg's scores min-max normalised
    #: and summed; tied rule legs normalised by rank).
    fusion: Literal["rrf", "minmax"] = "rrf"
    #: N43 (Dakera session cohesion): an RRF leg of records said within
    #: ``COHESION_WINDOW_MINUTES`` of the first-pass top hits (``ANCHOR_TOP``).
    cohesion_leg: bool = False
    #: N32 (Hindsight / EverMemOS entity links) + N53 (Mem0 damping): an RRF leg of
    #: records naming the proper nouns and years the first-pass top hits name; names
    #: in more than ``ENTITY_EXPAND_MAX_SHARE`` of the records are dropped.
    entity_expand_leg: bool = False
    #: N60 (EverMemOS MaxSim, Dakera sentence sub-memories): an RRF leg of first-pass
    #: candidates with two or more sentences, ranked by their best sentence's cosine
    #: with the query. Embeds candidate sentences at read (cached); no index change.
    maxsim_leg: bool = False
    #: Word-vector leg (gap B5/B7, 2026-10-10): an RRF leg ranking every record of the
    #: namespace by the cosine of pooled static word vectors (query vs record), so related
    #: words meet without sharing a term ("hobbies" ~ "kayaking"). Opt-in; set
    #: ``hybrid: false`` to use it IN PLACE of the BM25 leg, or keep both. Brute force over
    #: the namespace with per-record vectors cached in memory (fine for conversation-sized
    #: stores; not an index).
    word_vector_leg: bool = False
    #: ``model2vec`` ([static] extra) or ``word2vec`` (gensim KeyedVectors, local path).
    word_vector_provider: Literal["model2vec", "word2vec"] = "model2vec"
    #: model id (model2vec) or file path (word2vec); None = minishlab/potion-retrieval-32M.
    word_vector_model: str | None = None
    #: hits the leg contributes to the fusion; None = the search's fetch size.
    word_vector_top_k: int | None = Field(default=None, ge=1)
    #: Gap B5 (2026-10-10): drop speaker/person names (the "Speaker:" prefixes of the
    #: namespace's records) from the BM25 query only, so a name in the question does not
    #: make BM25 rank by the name. The vector leg is untouched; if nothing is left the
    #: original query is used.
    lexical_strip_names: bool = False
    #: Gap B8 (2026-10-10): an RRF leg ("session") that ranks sessions (``group_id``) by the
    #: cosine of the query with the mean of their record vectors, then contributes the best
    #: ``session_leg_per_session`` records of each of the top ``session_leg_top_sessions``.
    session_leg: bool = False
    session_leg_top_sessions: int = Field(default=3, ge=1)
    session_leg_per_session: int = Field(default=2, ge=1)
    #: Gap B1 (2026-10-10) list mode, opt-in. For a list / set question (``list_trigger``)
    #: that names exactly one speaker, the read (1) adds a ``speaker_vote`` RRF leg: the
    #: vector leg fetched ``list_vote_depth`` deep, kept to the named speaker's turns (top
    #: ``list_vote_top_k``); (2) draws ``list_pool`` x top_k candidates (never fewer than
    #: ``candidate_pool``) and skips the ``rerank_keep`` cut; (3) gives only the best
    #: ``window_full_hits`` replay hits their neighbour window, the rest are single turns
    #: (``None``: every hit keeps its window). Other questions read exactly as before.
    list_mode: bool = False
    #: ``set_question_wide`` (R2-3) = ``is_set_question_wide``: also "How did X <verb> ..." and
    #: plural-object questions. Where a question names two speakers with a comparison cue
    #: ("both", "in common", "each") the vote runs once per speaker (R2-2).
    #: ``set_question`` = ``query_shape.is_set_question``; ``aggregation`` = ``is_aggregation``
    #: or ``is_count``.
    #: ``intent`` (I4) = ``query_shape.is_intent_list``, a no-model generic trigger: counts,
    #: totals, "so far", "all the", "each", "both", plural answer heads, enumerating
    #: conjunctions; it stays quiet on one-item "most recent / last / favourite" questions.
    list_trigger: Literal["set_question", "set_question_wide", "aggregation", "intent"] = (
        "set_question"
    )
    #: I5: how the list-mode speaker vote picks its speaker. ``name`` (default) = the one
    #: speaker the question names (``speaker:`` tag or capitalised "Name:" prefix). ``subject``
    #: = that, and otherwise the question's subject: a first-person "I / my" votes on the
    #: ``user`` turns, a second-person "you" on the ``assistant`` turns (role from a
    #: "user:" / "assistant:" prefix or the record's source role; needs both roles in the
    #: store); a pronoun or mixed subject casts no vote.
    #: I39: ``perspective`` = the vote keyed on the resolved question perspective
    #: (``core/perspective``): a named participant, the asker (I / my), the assistant (you),
    #: or a relation-bound third party ("my cousin"); needs the tags of
    #: ``memories.episodic.policies.perspective``, else it falls back to the ``subject`` rules.
    speaker_vote_mode: Literal["name", "subject", "perspective"] = "name"
    #: I39 (perspective layer, tags written by ``memories.episodic.policies.perspective``).
    #: ``subject_weight`` = an RRF leg of the records about the question's subject plus a
    #: multiplier on every candidate's relevance, ``1 - perspective_weight * (1 - match)``
    #: (about the subject 1.0, the subject speaking of others 0.6, unresolved third party 0.5,
    #: about someone else 0.0). ``subject_filter`` = the same leg, and candidates about someone
    #: else are dropped (never below ``perspective_min_keep`` candidates). Off: unchanged.
    perspective_mode: Literal["off", "subject_weight", "subject_filter"] = "off"
    perspective_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    #: I75b: False = the perspective only multiplies the relevance of candidates the other legs
    #: found (``1 - perspective_weight * (1 - match)``); no perspective RRF leg is added.
    #: True (default) = today's behaviour, leg plus multiplier.
    perspective_leg: bool = True
    perspective_min_keep: int = Field(default=3, ge=0)
    #: The asker's id ("caroline", "user"); None = ``user`` when the store has user turns, a
    #: lone participant, else unresolved (first-person questions then stay neutral).
    perspective_asker: str | None = None
    #: Which axes the read uses. ``subject`` = whose / about whom; ``modality`` = a plan,
    #: wish, hypothetical, question or request is weaker evidence of a fact; ``polarity`` = a
    #: negated statement is weaker evidence for a positive question (kept for "never / not /
    #: ever" questions); ``scope`` = a one-off event is weaker evidence of a trait question
    #: ("what does X like"); ``sensitivity`` = a ``sensitive:*`` record needs the question to
    #: touch the same category. Each multiplies the relevance by ``1 - perspective_weight``
    #: (scope: half of it). I43 ``hearsay`` = a reported-speech record ("my mom said ...") is
    #: weaker evidence for a question about its source (half of it); I44 ``certainty`` = a
    #: ``cert:hedged`` record is weaker evidence for a fact question unless the question
    #: itself hedges ("do you think ...", "maybe"); I47 ``ack`` = an assistant statement about
    #: the user that no later user turn acknowledged is weaker evidence for a user-profile
    #: question (dropped under ``subject_filter``).
    perspective_axes: list[
        Literal[
            "subject", "modality", "polarity", "scope", "sensitivity", "hearsay", "certainty", "ack"
        ]
    ] = Field(default_factory=lambda: ["subject"])
    #: I39: show ``[about: Caroline's cousin]`` before a record whose subject differs from its
    #: speaker (the stored content is unchanged). Off: unchanged.
    perspective_marker: bool = False
    #: I59: the read-side owner check, after retrieval and before the reader. Each context line
    #: is judged against the person the question names (``core/owner_check``). ``mark`` = prefix
    #: ``[Melanie, about Melanie]`` (speaker, subject) on every tagged line; ``note`` = when NO
    #: line is about the named person and some line is about someone else, add one reader note
    #: ("none of the memories describe X doing this; the matching memories are about Y"); ``both``
    #: = both. Never drops a line; a question naming two people keeps both. Needs the tags of
    #: ``memories.episodic.policies.perspective``. With ``decider_tasks`` containing
    #: ``about_target`` an untagged line is judged by the decider. Off: unchanged.
    owner_check: Literal["off", "mark", "note", "both"] = "off"
    #: I60: a capitalised name in the question that occurs nowhere in the store (no record text,
    #: no participant; exact match, a 3+ letter prefix of a stored word counts as present) adds
    #: the reader note "<name> is not mentioned in the memories". Off: unchanged.
    entity_check: Literal["off", "note"] = "off"
    #: I63: with a known asker (``perspective_asker`` or the harness's persona / asker), the
    #: context opens with "You are the assistant. The user is <asker>. Memory lines are
    #: labelled with their speaker." No asker (LoCoMo): nothing is added, none is invented.
    user_header: Literal["off", "on"] = "off"
    #: A03: a typed query contract built from the question (``core/query_contract``): target
    #: subjects, predicate, answer type (person / place:city|country|... / date|time|year /
    #: duration / count / quantity / title / name / yes-no / reason / description), time scope,
    #: expected cardinality (one / many / count) and request kind (recall / inference /
    #: recommendation). ``heuristic`` = rules only (English surface cues, no model, no
    #: vocabulary of any dataset); ``llm`` = the rules first, and ONE ``plan@contract`` call
    #: only when the rules are unsure of the answer type (the call may refine, never blank, the
    #: contract). ``off`` (default): nothing is built. The contract is always recorded in the
    #: forensics (``query_contract``); ``query_contract_use`` says where else it goes.
    query_contract: Literal["off", "heuristic", "llm"] = "off"
    #: A03: where a built contract is used. ``header`` = one lead line in the reader's context
    #: ("Answer type: place:city; expected: one."), only when the answer type is known;
    #: ``rerank`` = the same hint appended to the reranker's query (never to the embedder's).
    query_contract_use: list[Literal["header", "rerank"]] = Field(
        default_factory=lambda: ["header"]
    )
    #: I64: with a session id, a record injected in the last ``reinjection_window`` replies of
    #: that (namespace, session) has its relevance multiplied by
    #: ``1 - penalty * uses / window`` (floor ``1 - penalty``). 0 = off. Without a session id
    #: nothing is tracked or penalised, so independent QA is untouched.
    reinjection_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    reinjection_window: int = Field(default=5, ge=1)
    #: I42: under an as-of read (``as_of``) with a question that resolves to a subject, a
    #: superseded record that was current at that time is admitted as history only when it is
    #: about a target (or carries no subject tags); other subjects' history stays out. Needs
    #: ``perspective_mode != off``. Off: unchanged.
    perspective_as_of_subject: bool = False
    #: I50: with ``profile_slots_header``, the slots block becomes a per-subject card: the live
    #: ``kind:state`` facts rolled up per (owner, subject) (``sub:`` tags, else the entity;
    #: cardinality one = the latest, many = a list), injected only for the subjects the
    #: question's resolved persons match. Off: the per-entity slots block, unchanged.
    profile_subject_card: bool = False
    list_vote_depth: int = Field(default=100, ge=1)
    list_vote_top_k: int = Field(default=30, ge=1)
    list_pool: int = Field(default=3, ge=1, le=10)
    window_full_hits: int | None = Field(default=10, ge=0)
    #: GR-6 (Graphiti node search): an RRF leg of the records that mention the entity
    #: nodes best matching the question (cosine on embedded entity names + text match,
    #: fused). Needs ``graph.entity_embeddings`` and associative entity nodes.
    graph_node_search: bool = False
    #: G-16 (SimpleMem symbolic leg): an RRF leg of records whose location / topic /
    #: person view tags (``consolidation.mine_multiview``) share words with the question.
    view_tag_leg: bool = False
    #: G-22 (SimpleMem pyramid retrieval): None = off; N = the best N hits keep their
    #: full text and the rest are shown as their one sentence most like the question,
    #: before the budget fit, so more distinct evidence fits.
    gist_after: int | None = Field(default=None, ge=0)
    #: N31 (Memori session summaries, extractive): a read header with, for the
    #: sessions of the first hits, the two sentences most like the question. It does
    #: not hide the session's turns from the read. Off: no header.
    session_digest: bool = False
    #: N54 (EverMemOS episode/fact pairing): a mined fact in the hits gives its slot,
    #: rank and score to its live source turns, so facts never displace raw turns.
    facts_to_sources: bool = False
    #: N55 (EverMemOS per-source quotas): the most records of each memory type the
    #: read keeps (``{"semantic": 3}``); types not listed are uncapped. Empty: unchanged.
    type_quotas: dict[str, int] = Field(default_factory=dict)
    #: C2 (replay the last exchanges): a read header with the namespace's last N live
    #: episodic turns, oldest first (``RECENT_SHARE`` of the budget); the in-flight
    #: question is left out and the turns are not hidden from the read. 0: off.
    recent_exchanges: int = Field(default=0, ge=0, le=20)
    #: C6 (per-leg score floors): ``{"vector": 0.3, "lexical": 1.0}`` drops a leg's
    #: hits below its floor before fusion (vector = cosine, lexical = BM25). Empty: off.
    leg_min_scores: dict[str, float] = Field(default_factory=dict)
    #: C7 (labelled sections): when read headers lead the context, the retrieved part
    #: gets a caption too (``RETRIEVED_CAPTION``). Off: unchanged.
    section_captions: bool = False
    #: N33 (Memori, Mem0): the BM25 leg's RRF weight for a question of at most
    #: ``SHORT_QUERY_WORDS`` content words. None: unchanged.
    short_query_lexical_weight: float | None = Field(default=None, ge=0.0)
    #: N59 (Dakera name boost): an RRF leg of raw turns naming the question's proper
    #: nouns and years beyond the speakers (``temporal_query.entity_leg``).
    entity_leg: bool = False
    #: N63 (EverMemOS multi-query, by rules): the question rewritten as a statement
    #: ("When did Ana go camping?" -> "Ana go camping") joins the search as a probe.
    statement_probe: bool = False
    #: N40 (MemMachine): when the question names a speaker, "Name: <core terms>" (the
    #: stored-turn form) joins the search as a vector probe.
    speaker_probe: bool = False
    recency_leg: bool = False
    #: H12: cap the total of all lead blocks (cards, graph facts, entity summaries,
    #: profile, count timeline) at this share of the read budget, dropping the
    #: lowest-priority blocks first, so raw evidence keeps its budget. None = no cap.
    lead_budget_share: float | None = Field(default=None, gt=0.0, le=1.0)
    #: W19 (plan v3.2, F3): derived records (mined facts, cards, summaries) found by
    #: the routed search no longer take search slots from raw (episodic) turns: the
    #: search widens by their number, so every raw turn hit of the read without them
    #: stays, with its replay window. The 4K budget was not binding on LoCoMo (about
    #: 1.7K tokens used); slots were. Off: byte-identical.
    raw_turn_floor: bool = False
    #: F5 (plan v3.2): a verbatim question (``is_verbatim``: "what did X say about
    #: ...", "exact words") gets no read header (cards, profile, graph facts, entity
    #: summaries) and, with ``cards: header``, no mined fact either: it reads raw
    #: turns only (LoCoMo quote questions fell 92 -> 68-74 under every header).
    #: Off: byte-identical.
    verbatim_raw_only: bool = False
    #: W3 (plan v3.2, G02): attach an evidence-sufficiency signal to every assembled
    #: context (``AssembledContext.evidence``, ``core/evidence.py``): top score, spread,
    #: distinct days, the asked answer type and whether a top candidate holds one, and
    #: ``weak``. Reported only; the context is unchanged. Off: ``evidence`` is None.
    evidence_signal: bool = False
    #: W3: with ``evidence_signal``, a best evidence score below this marks the read
    #: ``weak`` (scores are on the search's own scale). None: only a missing answer
    #: type makes it weak.
    evidence_weak_below: float | None = Field(default=None, ge=0.0)
    #: W3 step 2 (plan v3.2): with ``evidence_signal``, a weak read opens its volatile
    #: part with a one-line note that memory holds no clear record answering the
    #: question (``constants.WEAK_EVIDENCE_LINE``). Off: unchanged.
    evidence_line: bool = False
    #: F4 (plan v3.2): with ``cards: header``, show the cards header (and let mined
    #: facts into the read) only when a raw-turns-only search gives *weak* evidence
    #: (the W3 signal, with ``evidence_weak_below``); strong raw evidence reads raw turns
    #: only, like a gated question. Replaces question-shape gates (cards helped weak
    #: conversations and hurt strong ones on LoCoMo). One extra local search per read.
    #: The threshold is calibrated on one dataset and checked on another (U5). Off:
    #: byte-identical.
    cards_when_weak: bool = False
    #: T10 (plan v3.2): in a replay read, the best search hit's window of turns comes
    #: first (in time order), then the other windows in time order, so the reader meets
    #: the best evidence before the context around it. Off: all turns in time order.
    evidence_first: bool = False
    #: N01 (plan v3.2, MAB TAM): presentation order of the retrieved (volatile) records.
    #: ``relevance`` (unchanged): score order. ``recorded``: always time order.
    #: ``recorded_if_shared_key``: time order only when two records state the same
    #: keyed fact (entity + attribute), so the newer value reads last. An ordering
    #: question under ``order_by_time_for_ordering`` is time-ordered either way.
    present_order: Literal["relevance", "recorded", "recorded_if_shared_key"] = "relevance"
    #: I17 (``core/latest_wins.py``): when the retrieved evidence states one topic at
    #: several times (a keyed fact, or raw turns by one speaker with overlapping content
    #: words), mark the newest statement ``[latest]`` and each older one ``[earlier
    #: statement; a later one on this topic is dated D]``, keeping the older value as
    #: history. ``annotate_recent_first`` also gathers those records, newest first.
    #: Records tagged ``disputed`` get a disputed mark and no ordering claim. Off: unchanged.
    latest_wins: Literal["off", "annotate", "annotate_recent_first"] = "off"
    #: I17: overlap coefficient (shared / smaller set of content words) at which two raw
    #: turns by one speaker count as one topic. Keyed records match on their key instead.
    latest_wins_min_overlap: float = Field(default=0.5, gt=0.0, le=1.0)
    #: N13 (plan v3.2, Mnemon ``focused``): a retrieved record of six or more lines is
    #: shown as its two lines that best match the question, each with its next line,
    #: cuts marked "…" (``core/excerpt.py``). Never for a verbatim question. The stored
    #: record is unchanged. Off: whole records.
    focused_excerpt: bool = False
    #: W10 (plan v3.2, Mnemon ``fillPerGroup``): at most this many raw-turn hits from any
    #: one session (episodic gap-split sessions, else the calendar day), drawn from a
    #: 4x wider search, so evidence spread over many sessions reaches the read. In
    #: replay each hit brings its window. None: unchanged (no cap).
    session_cap: int | None = Field(default=None, ge=1)
    #: N21 (plan v3.2, RAGDefender): collapse each dense near-duplicate cluster among
    #: the search candidates (3+ records sharing ``concentration_jaccard`` of their
    #: content words) to its best member, tagged ``concentrated:<n>``
    #: (``core/concentration.py``). PoisonedRAG planted sets: 257 / 300 clustered at
    #: 0.2; LoCoMo 10-turn windows: 8 / 581. Off: unchanged.
    concentration_filter: bool = False
    concentration_jaccard: float = Field(default=0.2, gt=0.0, le=1.0)
    #: N03 (plan v3.2, Mnemon ``feedback``): pseudo-relevance feedback. Up to five content
    #: words that two or more of the first-round top five hits share and the question
    #: lacks join the search as one more RRF probe (one extra local search). Off:
    #: unchanged.
    prf_expansion: bool = False
    #: G34 (plan v3.2): a multi-part question ("..., and also where did he move?") is
    #: split on discourse markers and each part joins the search as an RRF probe.
    #: Off: unchanged.
    multi_intent_split: bool = False
    #: N04 (plan v3.2, EverOS / Honcho): when the first search's evidence is weak (W3
    #: signal with ``evidence_weak_below``), the names and dates its top three hits
    #: mention and the question lacks seed a second search over a doubled pool. Off:
    #: unchanged.
    second_round: bool = False
    #: R2-1 (bridge hop): after the first search, up to three key noun phrases of the top
    #: three hits that the question lacks ("home country") join the question subject as
    #: one extra search (``bridge_hop_top_k`` hits), fused in as the ``bridge`` leg. One
    #: hop at most; the phrases are recorded in ``search_forensics`` ("bridge_phrases").
    #: Off: unchanged.
    bridge_hop: bool = False
    bridge_hop_top_k: int = Field(default=10, ge=1)
    #: R2-1b: when the hop fires. ``always`` (default): every read. ``cue``: only when the
    #: question describes its answer's entity ("home country", "where ... move from",
    #: "the studio that X opened", "her son's"). ``weak``: only when the first pass found
    #: one confident anchor without support (second-best raw reranker score under
    #: ``bridge_hop_weak_threshold``). ``cue_or_weak``: either. The decision is recorded
    #: in ``search_forensics`` as ``bridge_gate``.
    bridge_hop_gate: Literal["always", "cue", "weak", "cue_or_weak"] = "always"
    #: Second-best raw reranker score below which the first pass counts as weak. 0.4 was
    #: chosen offline on the dev forensics (second-best score of the 233 screen questions:
    #: 31 under 0.4, 28 under 0.35, 40 under 0.5).
    bridge_hop_weak_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    #: I67 (opt-in agentic read): after the normal first pass (step 0, unchanged), the
    #: ``sufficiency`` role (else ``plan``) sees the question and a compact view of the
    #: evidence and returns ONE structured action: ``answer_ready``, ``search`` (a query)
    #: or ``search_person_time`` (a person and/or a time). Each search goes through the
    #: same gated read search of the same namespace; its new hits are RRF-fused and
    #: appended under the same token budget. Not native tool calling. Off: no call,
    #: byte-identical. Every step is kept in ``search_forensics()["agentic_steps"]``.
    agentic: bool = False
    #: Most action steps per question (each is one LLM call and at most one extra search).
    #: The schema caps it at 5.
    agentic_max_steps: int = Field(default=2, ge=1, le=5)
    #: When the loop fires. ``always``; ``multi_hop`` (a generic surface heuristic:
    #: several names, a relation word such as both / same / before / after, a possessive
    #: chain, a bridge cue); ``decider`` (the ``needs_more_evidence`` task of the
    #: OpenDecider port, falling back to the ``multi_hop`` heuristic when the decider is
    #: off, not listed in ``decider_tasks``, unsure or failing).
    agentic_trigger: Literal["always", "multi_hop", "decider"] = "multi_hop"
    #: Hits one extra search keeps (before de-duplication against the evidence).
    agentic_top_k: int = Field(default=8, ge=1, le=20)
    #: Most NEW records the loop adds over all steps.
    agentic_max_new: int = Field(default=6, ge=1, le=20)
    #: Share of the read budget reserved for the step-0 hits (taken in their order). New
    #: records may displace only step-0 records after that share, last first, and only to
    #: fit themselves.
    agentic_first_share: float = Field(default=0.6, ge=0.0, le=1.0)
    #: E02 (refines I67): how the loop picks its next action. ``query`` (default) = I67 as
    #: built: answer_ready / search / search_person_time. ``slot``: every step names the
    #: MISSING SLOT of the question (seeded by the A03 contract) and picks ONE of
    #: memory_search, relation_expand (one hop through the stored facts of an entity),
    #: neighbor_lookup (the turns next to a cited one), calculate (a date/number computation
    #: done by code), answer, qualified_stop. Stops on sufficiency, ``agentic_max_steps``, the
    #: token budget, a repeated action, a step with no new useful evidence, or a qualified
    #: stop (an unresolved slot is reported, never guessed). Step 0, the reserved first-pass
    #: share and the namespace are as in ``query``. The slot of every step is kept in
    #: ``search_forensics()["agentic_steps"]``. Needs ``read.agentic``.
    agentic_mode: Literal["query", "slot"] = "query"
    #: E01 (read-time arm): ``read_time`` makes ONE bounded structured call per triggered read
    #: (``extract@assertions``, the ``extract`` role) over the evidence already in context:
    #: subject-relation-object assertions with the exact source span, modality and time. Two
    #: assertions are joined only through a resolved entity or an evidenced relation, and the
    #: join is shown to the reader as a short derived-chain block, every link quoting its
    #: span. Raw turns stay authoritative; nothing derived is stored. ``off`` (default): no
    #: call, byte-identical. The write-time projection is a separate arm:
    #: ``memories.semantic.policies.fact_projection``.
    fact_chain: Literal["off", "read_time"] = "off"
    #: When the extraction call fires: ``multi_hop`` (default; the I67 surface heuristic);
    #: ``triggered`` = multi-hop OR the A03 contract's relation is not stated beside its
    #: subject in any evidence line (offline it fires on 58% of LoCoMo questions with no better
    #: hit rate on wrong answers than the base rate: a screen option only); ``always``.
    fact_chain_trigger: Literal["multi_hop", "triggered", "always"] = "multi_hop"
    #: I28: the optional decider for read-path decisions. ``heuristic`` (default) = the
    #: existing regexes and rules, byte-identical. ``opendecider`` asks OpenDecider-nano
    #: (``[decider]`` extra) at the decision points named in ``decider_tasks``; a decision
    #: below ``decider_min_confidence``, or any failure, keeps the heuristic answer. Every
    #: decision is recorded in ``search_forensics`` under ``decisions``.
    decider: Literal["heuristic", "opendecider"] = "heuristic"
    decider_model: str = "manjunathshiva/opendecider-nano"
    #: CPU by default: the GPU is usually shared with the reader.
    decider_device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    decider_tasks: list[
        Literal["list_mode", "bridge_hop", "about_target", "needs_more_evidence"]
    ] = Field(default_factory=lambda: ["list_mode", "bridge_hop"])
    #: I29: whether retrieved memories are injected at all. ``off`` (default): always, as
    #: today. ``decider``: the decider (``read.decider: opendecider``) judges the message
    #: against the retrieved memories; when it is sure (``decider_min_confidence``) that none
    #: bear on it, the read returns an empty, abstained context. ``store_calibrated``: the
    #: namespace's own off-topic score level is learned lazily from a fixed set of generic
    #: off-topic probes (``core/relevance_probes.py``) and a message passes only when its RAW
    #: top score (vector cosine, or the reranker's raw score) clears that level by
    #: ``relevance_gate_margin_sd`` standard deviations; otherwise an empty, abstained context.
    relevance_gate: Literal["off", "decider", "store_calibrated"] = "off"
    #: I74: the personal-reference bypass of either gate. A message that names the store's
    #: own people / entities (a participant of the namespace, or a capitalised name present
    #: in the store, same rules as I60 ``entity_check``: exact, or a 3+ letter prefix) needs
    #: memory, so the gate is skipped and the memories are injected as normal. ``none``: no
    #: bypass. ``named`` (default, only acts when the gate is on): names only.
    #: ``named_or_first_person``: also "I / my / me" (separate option: OP-Bench baiting
    #: probes are phrased in the first person).
    relevance_gate_bypass: Literal["none", "named", "named_or_first_person"] = "named"
    #: I29/I37: margin, in standard deviations of the off-topic probes' top raw scores, above
    #: their 95th percentile. A FIXED portable default (one value for every store, embedder
    #: and reranker); NOT tuned on any benchmark. Raise it for a stricter gate.
    relevance_gate_margin_sd: float = Field(default=1.0, ge=0.0, le=10.0)
    #: Which raw score the calibrated gate reads. ``auto``: the reranker's raw score when a
    #: reranker is configured, else the vector cosine. ``vector`` / ``rerank`` force one.
    #: ``any``: pass when any available leg clears its own threshold.
    relevance_gate_leg: Literal["auto", "vector", "rerank", "any"] = "auto"
    #: Candidates (top of the vector leg) the gate scores per message; the reranker leg
    #: rescored only these.
    relevance_gate_candidates: int = Field(default=10, ge=1, le=100)
    #: Recalibrate when the namespace holds more than this multiple of the record count it
    #: was calibrated on.
    relevance_gate_regrow: float = Field(default=2.0, gt=1.0)
    #: I30: judge ``assembly.theta_abstain`` and ``assembly.relative_floor`` on the RAW
    #: reranker scores (before min-max), so they work under rerank (the min-max makes the top
    #: candidate 1.0, which kills ``theta_abstain``). Off (default): the composite scores,
    #: as before. Raw scores are model-specific; prefer ``relevance_gate: store_calibrated``
    #: for a portable cut.
    abstain_on_raw: bool = False
    decider_min_confidence: float = Field(default=0.5, ge=0.5, le=1.0)
    #: I52: graded-sensitivity read bar. ``off`` (default): every retrieved memory is
    #: eligible as today. ``on``: a record graded ``medium`` or ``high`` at write time
    #: (``write.sensitivity``; the W16 ``sensitive:*`` tags count as a grade too) is injected
    #: only when the question is about its category, names its subject (medium) or shares
    #: enough content words with it (1 for medium, 2 for high). ``decider``: as ``on``, and
    #: when the question names no sensitive topic the ``sensitivity_scope`` decider task may
    #: open the gate (it never closes it). See ``core/sensitivity.py``.
    sensitivity_gate: Literal["off", "on", "decider"] = "off"
    #: I48: records tagged ``src:inferred`` (``write.inferred``) are used only once they
    #: have ``inferred_min_support`` distinct supporting user turns. ``off`` (default): used
    #: as any record.
    inferred_gate: Literal["off", "on"] = "off"
    inferred_min_support: int = Field(default=2, ge=1)
    #: CPU speed (I38). Intra-op threads of the model; 0 = the physical cores. With
    #: ``decider_workers`` > 1 each worker gets ``cores // workers``.
    decider_threads: int = Field(default=0, ge=0)
    #: ``torch`` (default, fp32 as the model was evaluated) | ``onnx`` (the published 8-bit
    #: ONNX build through ONNX Runtime, CPU only; needs ``onnxruntime``).
    decider_backend: Literal["torch", "onnx"] = "torch"
    #: Concurrent decision calls (a bounded thread pool). 1 keeps the engine path as it is.
    decider_workers: int = Field(default=1, ge=1)
    #: torch weights dtype. ``float32`` (default) is how the model was evaluated;
    #: ``bfloat16`` was about 1.5x faster on this 16-core CPU and agreed on 300 of 300
    #: decisions (max probability difference 0.0075).
    decider_dtype: Literal["float32", "bfloat16"] = "float32"
    #: N06 (plan v3.2, EverMemOS clusters / HyperMem hyperedges, read-time variant): the
    #: embedding neighbourhoods of the top two hits, across sessions, join the search
    #: as RRF legs (two embeds and two vector queries per read). Off: unchanged.
    cluster_expand: bool = False
    #: E04: image evidence for a question that needs what a picture shows (requires
    #: ``ingest.assets: on`` at write time). ``cached``: use only evidence already computed
    #: (no network, no model). ``fetch``: also download the turn's own referenced URI once and
    #: describe it through the vision port (needs ``ingest.asset_dir``). Off: unchanged.
    asset_evidence: Literal["off", "cached", "fetch"] = "off"
    #: E04: at most this many assets are resolved for one read.
    asset_max_per_read: int = Field(default=2, ge=1)
    #: E03: public knowledge for an invited inference / recommendation question. ``cache``:
    #: only cached results (no network). ``web``: also call the provider, within
    #: ``external_max_calls``. Only generic topic words leave the process. Off: unchanged.
    external_evidence: Literal["off", "cache", "web"] = "off"
    #: E03: ``none`` (a provider can still be injected with ``Engine.set_external_provider``)
    #: or ``http`` (``MEMSPINE_EXTERNAL_SEARCH_URL`` / ``_KEY`` / ``_HEADER`` from the env)
    #: or ``wikipedia`` (keyless MediaWiki search + REST summary; generic query only).
    external_provider: Literal["none", "http", "wikipedia"] = "none"
    #: E03: network calls the engine may make in its lifetime (cache hits are free).
    external_max_calls: int = Field(default=20, ge=0)
    external_max_results: int = Field(default=3, ge=1, le=10)
    #: E03: directory of the persistent query cache (JSON). None: in memory.
    external_cache_dir: str | None = None

    @model_validator(mode="after")
    def _header_shares_leave_room(self) -> ReadConfig:
        """A-9: the active read headers' shares must leave budget for the read itself."""
        shares = (
            (
                self.cards_budget_share
                if self.cards == "header" or self.cards_include_edges or self.entity_summaries
                else 0.0
            )
            + (self.profile_budget_share if self.profile_header else 0.0)
            + (self.count_budget_share if self.count_timeline else 0.0)
        )
        if shares >= 1.0:
            raise ConfigError(
                "the active read header shares (read.cards_budget_share, "
                "read.profile_budget_share, read.count_budget_share) must sum to < 1, "
                f"got {shares:g}"
            )
        return self


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


class FirewallSignalsConfig(BaseModel):
    """W2 / N20 / N22 (plan v3.2): which write signals the firewall runs.

    The defaults reproduce the firewall before W2. ``instruction_extended`` widens the
    instruction patterns (ASB / MEM-INV framings); ``semantic_risk`` flags content that
    claims its own authority or binds a future answer (MAPLE-Guard); ``query_anomaly``
    flags a write that sits unusually close to recently asked queries (MemSAD; z-score
    above ``query_anomaly_kappa``). Turning ``instruction``, ``anomaly`` or
    ``minja_bridge`` off is an ablation arm, never a production setting.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: bool = True
    anomaly: bool = True
    minja_bridge: bool = True
    instruction_extended: bool = False
    semantic_risk: bool = False
    query_anomaly: bool = False
    query_anomaly_kappa: float = Field(default=3.0, gt=0.0)
    #: I21 (opt-in): roles whose writes skip the MINJA prefix-repeat / embedding-outlier
    #: signal. Default empty = unchanged. A chat assistant's templated openers share a
    #: 96-char prefix and are otherwise quarantined (measured: 39/40 on a synthetic chat);
    #: ``minja_bridge_exempt_roles: [assistant]`` is the chat-data setting. The exemption
    #: removes the prefix defence for that role (instruction patterns still apply).
    minja_bridge_exempt_roles: list[str] = Field(default_factory=list)
    anomaly_exempt_roles: list[str] = Field(default_factory=list)
    #: I21: shared-prefix length for the bridge signal; None = 96 (unchanged).
    minja_bridge_prefix_chars: int | None = Field(default=None, ge=16)


class FirewallConfig(BaseModel):
    """Memory Firewall switches (E1 + B8). Defaults reproduce the pre-B8 firewall.

    - ``enabled``: ``false`` keeps trust scoring but disables flagging, anomaly
      checks and quarantine: the N1 ablation arm. Never use it in production.
    - ``redact_secrets``: replace cloud keys, tokens, JWTs, private keys,
      ``key=value`` credentials and emails with ``[REDACTED:<kind>]`` at write,
      in content, entity, attribute and tags.
    - ``pii``: the PII pack, ``off`` | ``redact`` | ``tag`` (see the field).
    - ``max_content_chars``: a non-privileged write longer than this is
      quarantined (size anomaly, a common bulk-injection signature).
    - ``protected_keys``: fact keys (``entity`` or ``entity.attribute``) only an
      operator or system source may write; others are quarantined.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    redact_secrets: bool = False
    #: #44 PII pack (phone, Luhn-checked card, US SSN, mod-97 IBAN, IPv4/IPv6) over
    #: content, entity, attribute and tags. ``redact`` replaces each match with
    #: ``[REDACTED:<kind>]``; ``tag`` keeps the text, adds ``pii:<kind>`` tags and
    #: raises the record's ``pii_tier`` to at least ``high``; ``off`` does neither.
    pii: Literal["off", "redact", "tag"] = "off"
    #: N23 (plan v3.2): with ``pii`` on, also the cue-anchored kinds that have no
    #: checksum: card numbers after a card cue, bank accounts, passports, driving
    #: licences, licence plates, street addresses. Off: the #44 pack only.
    pii_extended: bool = False
    #: W16 (plan v3.2): tag GDPR art. 9-style sensitive topics (health, religion,
    #: sexual orientation, politics, ethnicity, legal, financial hardship;
    #: ``core/sensitive.py``) as ``sensitive:<topic>`` and raise ``pii_tier`` to at least
    #: ``high``. Text is kept; consent / purpose / remote-LLM tier rules then apply.
    sensitive_topics: bool = False
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
    #: I43: a hearsay record (``rep:`` tag from ``memories.episodic.policies.perspective``:
    #: "my mom said X", "I heard X") gets at most this trust, so it ranks below a first-hand
    #: statement and, through the parent cap, so does everything derived from it. None: off.
    hearsay_trust_cap: float | None = Field(default=None, ge=0.0, le=1.0)
    #: W2 / N20 / N22 (plan v3.2): per-signal switches (see FirewallSignalsConfig).
    signals: FirewallSignalsConfig = Field(default_factory=FirewallSignalsConfig)


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
    #: W13 (plan v3.2): corroboration counts independent LINEAGE ROOTS, not records:
    #: a corroborating write whose ``source.parents`` lineage shares a root with the
    #: quarantined record, or with an earlier corroborator, does not count (a summary
    #: or a re-statement of the poison's own source is not a second opinion). Applies
    #: whether or not ``integrity.enabled``. Off: unchanged.
    corroboration_roots: bool = False
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


class RetentionClassConfig(BaseModel):
    """#48: one retention class. Records of a namespace matching ``namespace`` (a
    glob, ``*`` = any) and of ``memory_type`` (None = any type) expire ``ttl_days``
    after they were recorded."""

    model_config = ConfigDict(extra="forbid")

    namespace: str = "*"
    memory_type: str | None = None
    ttl_days: float = Field(gt=0.0)


class RetentionConfig(BaseModel):
    """#48 retention classes (storage limitation). ``classes`` is checked in order;
    the first class that matches a record sets its TTL. A sleep-cycle stage
    hard-forgets expired records through the ordinary forget path, so legal holds
    and the per-type ``retention`` policy's ``may_delete`` are respected. Empty
    (default): no stage runs and nothing expires."""

    model_config = ConfigDict(extra="forbid")

    classes: list[RetentionClassConfig] = Field(default_factory=list)


class AuditConfig(BaseModel):
    """#49 durable audit, opt-in.

    - ``reads``: every ``search`` / ``assemble`` / ``read`` / ``retrieve`` /
      ``shared_search`` / ``export`` appends a ``memory.read_audit`` event (principal,
      namespace, returned record ids, purpose, time).
    - ``actions``: ``forget``, ``correct``, retention expiry and export append a
      ``memory.audit`` event with the actor and the reason.

    Both kinds carry a hash chain (each event stores the previous event's hash);
    ``Engine.audit_chain_ok()`` validates it. No projector reads them.
    """

    model_config = ConfigDict(extra="forbid")

    reads: bool = False
    actions: bool = False


class ObservabilityConfig(BaseModel):
    """I73 diagnostics, opt-in.

    - ``write_timers``: time each step of the write door (validation, firewall, redaction,
      embedding, projection, dedup, conflict ladder, perspective / sensitivity tagging and
      the inline LLM steps) with the monotonic clock; per step count, total, p50 and p95
      through ``Engine.write_timers()`` and ``describe()``. Off: nothing is wrapped.
    """

    model_config = ConfigDict(extra="forbid")

    write_timers: bool = False


class ConsentConfig(BaseModel):
    """#50 purpose limitation and the remote-LLM gate, opt-in.

    - ``enforce``: a read returns only records whose purpose set (``consent_tags``,
      set by ``write(..., purposes=[...])``) allows the read's ``purpose``. A record
      with ``*`` allows every purpose. A read without a purpose sees only untagged
      records.
    - ``untagged``: whether records with no purpose are visible to every read
      (``allow``) or to none (``deny``) while ``enforce`` is on.
    - ``remote_llm_max_tier``: text of records whose ``pii_tier`` is above this tier
      is withheld from every prompt sent to a remote LLM provider. None = off.
    - ``local_hosts``: extra ``api_base`` host names that count as local.
    """

    model_config = ConfigDict(extra="forbid")

    enforce: bool = False
    untagged: Literal["allow", "deny"] = "allow"
    remote_llm_max_tier: Literal["none", "low", "high", "regulated"] | None = None
    local_hosts: list[str] = Field(default_factory=list)


class RestApiKeyConfig(BaseModel):
    """#51: one API key of the reference REST auth middleware. The key itself is
    read from the environment variable ``key_env`` (or given in ``key``, which the
    loader can resolve from a secret reference); it is never logged or echoed."""

    model_config = ConfigDict(extra="forbid")

    key_env: str | None = None
    key: str | None = Field(default=None, repr=False)
    principal: str
    namespaces: list[str] = Field(default_factory=list)
    admin: bool = False


class RestJwtConfig(BaseModel):
    """#51: OIDC/JWT bearer verification (needs ``pyjwt``). The verification key
    comes from ``jwks_url`` (asymmetric, fetched by PyJWT) or the environment
    variable ``key_env`` (a PEM public key or an HMAC secret)."""

    model_config = ConfigDict(extra="forbid")

    issuer: str | None = None
    audience: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    jwks_url: str | None = None
    key_env: str | None = None
    principal_claim: str = "sub"
    namespaces_claim: str = "memspine_namespaces"
    roles_claim: str = "roles"
    admin_role: str = "memspine-admin"


class RestRateLimitConfig(BaseModel):
    """#51: in-memory token bucket per principal (per client address without auth)."""

    model_config = ConfigDict(extra="forbid")

    requests_per_second: float = Field(gt=0.0)
    burst: int = Field(default=10, ge=1)


class RestAuthConfig(BaseModel):
    """#51 reference auth middleware (not a production auth plane, ADR-041)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "api_key", "oidc_jwt"] = "none"
    api_keys: list[RestApiKeyConfig] = Field(default_factory=list)
    jwt: RestJwtConfig = Field(default_factory=RestJwtConfig)

    @model_validator(mode="after")
    def _jwt_is_pinned(self) -> RestAuthConfig:
        """ADR-041 addendum: ``oidc_jwt`` needs an issuer and an audience (a token
        minted for another app or by another issuer is refused), and a pinned
        algorithm family: never ``none``, never HMAC next to an asymmetric family
        (key confusion), never HMAC with a JWKS URL (a public key used as a secret)."""
        if self.mode != "oidc_jwt":
            return self
        cfg = self.jwt
        if not cfg.issuer or not cfg.audience:
            raise ValueError("rest.auth.mode=oidc_jwt needs rest.auth.jwt.issuer and audience")
        algorithms = [a.strip() for a in cfg.algorithms]
        if not algorithms:
            raise ValueError("rest.auth.jwt.algorithms must name at least one algorithm")
        if any(a.lower() == "none" or not a for a in algorithms):
            raise ValueError("rest.auth.jwt.algorithms may not include 'none'")
        families = {a[:2].upper() for a in algorithms}
        if "HS" in families and len(families) > 1:
            raise ValueError("rest.auth.jwt.algorithms may not mix HMAC (HS*) with others")
        if "HS" in families and cfg.jwks_url:
            raise ValueError("rest.auth.jwt.jwks_url needs asymmetric algorithms, not HS*")
        return self


class RestConfig(BaseModel):
    """#51 REST protocol options. Defaults keep the unauthenticated v0.1 app."""

    model_config = ConfigDict(extra="forbid")

    auth: RestAuthConfig = Field(default_factory=RestAuthConfig)
    rate_limit: RestRateLimitConfig | None = None


class WriteConfig(BaseModel):
    """Write-time governance tags (I48, I52, I53). Every key is off by default: a record is
    stored exactly as it was before. The tags are labels only (never the text)."""

    model_config = ConfigDict(extra="forbid")

    #: I52: grade each record none/low/medium/high with a category (``core/sensitivity.py``)
    #: and tag it ``sens:<grade>`` / ``sensc:<category>``. ``heuristic``: the fixed lexicon.
    #: ``decider``: the lexicon, raised by the decider ``sensitivity`` task when it is sure
    #: (``read.decider: opendecider``); the decider can raise a grade, never lower it.
    sensitivity: Literal["off", "heuristic", "decider"] = "off"
    #: I53: ``session``: ``write_messages`` tags each turn ``participant:<name>`` for every
    #: speaker of the call (the turn's own speaker first; a turn's ``name``/``speaker`` key,
    #: else its role), and a derived record (mined fact, summary) inherits the union of its
    #: parents' participants and the strictest visibility. ``viewer`` reads then filter.
    participants: Literal["off", "session"] = "off"
    #: I48: tag engine-derived facts ``src:inferred`` (mined / reflected / consolidated, or
    #: assistant-proposed with no user turn behind them), cap their trust at
    #: ``inferred_trust_cap`` and record the distinct supporting user turns as ``support:<id>``.
    #: A mined fact whose words are at least ``inferred_explicit_overlap`` contained in one user
    #: turn is that user's statement, not an inference, and is left alone.
    inferred: Literal["off", "on"] = "off"
    inferred_trust_cap: float = Field(default=0.4, ge=0.0, le=1.0)
    inferred_explicit_overlap: float = Field(default=0.6, gt=0.0, le=1.0)
    inferred_support_overlap: float = Field(default=0.3, gt=0.0, le=1.0)


class IngestConfig(BaseModel):
    """E04 attachment handling at write time. Off by default: a message's ``attachments`` are
    ignored exactly as before. On: each attachment's identity (source turn, original URI,
    source caption, availability) is registered and the record is tagged ``asset:<id>``;
    nothing is downloaded at ingest."""

    model_config = ConfigDict(extra="forbid")

    assets: Literal["off", "on"] = "off"
    #: Cache directory (gitignored in the eval harness): ``registry.sqlite`` and the
    #: downloaded bytes under ``blobs/`` by content hash. None: registry in memory, no blobs
    #: (so ``read.asset_evidence: fetch`` is refused at start).
    asset_dir: str | None = None
    asset_max_bytes: int = Field(default=5_000_000, ge=1)
    asset_timeout_s: float = Field(default=15.0, gt=0.0)
    asset_allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/gif", "image/webp"]
    )
    #: ``none``: no description is produced (text-only). ``ollama``: a local vision model.
    asset_vision: Literal["none", "ollama"] = "none"
    asset_vision_model: str = "qwen2.5vl:3b"
    asset_vision_url: str = "http://localhost:11434"
    asset_vision_timeout_s: float = Field(default=120.0, gt=0.0)


class DataShapeConfig(BaseModel):
    """I25: facts about the data the engine will serve, declared by the data adapter (or
    the deployer). Only ``data_profile: auto`` reads them; an undeclared fact (None /
    ``unknown``) selects no preset."""

    model_config = ConfigDict(extra="forbid")

    has_timestamps: bool | None = None
    has_question_date: bool | None = None
    speaker_kind: Literal["named", "user_assistant", "single_author", "unknown"] = "unknown"
    turn_length: Literal["short", "medium", "long", "unknown"] = "unknown"
    history_size: Literal["small", "medium", "large", "unknown"] = "unknown"
    language: str | None = None
    abstention_possible: bool | None = None


class MemspineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "simple"
    #: I25: ``off`` (default, nothing changes) | ``auto`` (presets chosen from
    #: ``data_shape``) | comma-separated preset names from ``config/presets/``. Presets are
    #: a layer between the template and the user config, so explicit settings still win.
    data_profile: str = "off"
    data_shape: DataShapeConfig = Field(default_factory=DataShapeConfig)
    strict_services: bool = True
    event_log: EventLogConfig = Field(default_factory=EventLogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    write: WriteConfig = Field(default_factory=WriteConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    rest: RestConfig = Field(default_factory=RestConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    memories: dict[str, MemoryTypeConfig] = Field(default_factory=dict)
    namespaces: dict[str, NamespaceConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _asset_keys_agree(self) -> MemspineConfig:
        """E04: ``read.asset_evidence`` needs the identities ``ingest.assets`` registers, and
        ``fetch`` needs a directory to cache the downloaded bytes by content hash."""
        if self.read.asset_evidence != "off" and self.ingest.assets != "on":
            raise ConfigError("read.asset_evidence needs ingest.assets: on")
        if self.read.asset_evidence == "fetch" and not self.ingest.asset_dir:
            raise ConfigError("read.asset_evidence: fetch needs ingest.asset_dir (the cache)")
        return self

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
