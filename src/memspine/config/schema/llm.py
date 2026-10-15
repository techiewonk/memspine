"""Config area: llm (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    #: F-03 (P-25 C): version the vector space. Each stored vector belongs to a space (embedder
    #: model, dim, dtype, query instruction, normalisation); after an embedder change new writes go
    #: to the new space, reads query only a namespace's active space, and ``reembed`` builds the new
    #: space in batches then switches. Off = one table per embedder, as before.
    versioned_spaces: bool = False
    #: MD-02 (P-17 C, M-23 B): a named embedding profile (``gpu`` = Qwen3-Embedding-0.6B bf16 on
    #: CUDA with the retrieval instruction; ``cpu_multilingual`` = a small multilingual ONNX
    #: model; more via ``register_profile``). It fills provider, model, dim and query instruction
    #: over the fields above. None (default) = the fields above are used as written.
    profile: str | None = None
    #: MD-02: pin the resolved profile (model, dim, dtype, instruction, normalisation, version
    #: hash) beside the store at creation and refuse to open the store with a different one.
    #: Off (default) = nothing is written or checked. In-memory stores pin nothing.
    pin_profile: bool = False
    #: MD-02: accept a profile change on open: the pin is replaced and the old hash kept as
    #: ``previous``. Needs ``versioned_spaces`` (new vectors go to a new space; run the F-03
    #: re-embed to move the old records).
    migrate_profile: bool = False


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


TierName = Literal["none", "small-local", "worker", "frontier"]


class ModelDecl(BaseModel):
    """MD-01: one model the deployment declares, with the memory it needs. The device plan is
    computed from these declared sizes alone (no GPU, no download). ``id`` is the LiteLLM model id
    for an ``llm`` (``ollama/qwen3.5:9b``, ``llamacpp/<path>``), else a free name; ``family`` names
    a reranker (``jina-v3.5`` | ``qwen3-reranker-0.6b`` | ``minilm``)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["llm", "reranker", "embedder"] = "llm"
    #: llm only. none = serves no role; small-local = 1-4B worker class; worker = today's 9B class;
    #: frontier = large local or hosted.
    tier: TierName = "worker"
    #: Empty = the prefix of ``id`` (``ollama``, ``openai``), else ``local``.
    provider: str = ""
    family: str = ""
    hosted: bool = False
    vram_mb: int = Field(default=0, ge=0)
    ram_mb: int = Field(default=0, ge=0)
    #: llm: memory reserved for the context window, added to whichever device hosts it (P-19).
    context_vram_mb: int = Field(default=0, ge=0)
    #: Where it may run: ``any`` (GPU if it fits, else CPU) | ``gpu`` | ``cpu``.
    placement: Literal["any", "gpu", "cpu"] = "any"
    supports_thinking: bool = False
    #: embedder only: ``cpu`` (default multilingual) | ``gpu`` (instruction embedder).
    profile: str = ""
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    timeout_seconds: float = 60.0


class GpuDecl(BaseModel):
    """MD-01: one declared CUDA device. Only CUDA is planned (MD-03: the AMD iGPU through DirectML
    is 5-8x slower than the CPU and resets the driver), so a non-NVIDIA name is ignored."""

    model_config = ConfigDict(extra="forbid")

    name: str = "cuda"
    vram_mb: int = Field(ge=0)


class ModelsConfig(BaseModel):
    """MD-01 (P-01, P-02, P-03, P-15, P-19): model tiers, placement, no-model fallbacks and thinking
    per role. ``tiers_v2`` off (default) = roles resolve exactly as before, from ``llm.roles``."""

    model_config = ConfigDict(extra="forbid")

    #: Roles resolve through tier, placement and fallback (``memspine.models.resolve``). A role with
    #: an explicit ``llm.roles`` entry keeps it (explicit wins). Taps: ``models.resolve``,
    #: ``models.placement``.
    tiers_v2: bool = False
    #: Declared models (any mix of llm / reranker / embedder) with their memory estimates.
    catalogue: list[ModelDecl] = Field(default_factory=list)
    #: Declared CUDA devices; empty = CPU only. A name that is not NVIDIA is ignored.
    gpus: list[GpuDecl] = Field(default_factory=list)
    #: Also probe ``nvidia-smi`` for CUDA devices (adds to ``gpus`` when none is declared).
    detect_gpus: bool = False
    #: Host RAM budget for CPU placement; 0 = not checked.
    ram_mb: int = Field(default=0, ge=0)
    #: VRAM kept free on every GPU.
    gpu_headroom_mb: int = Field(default=512, ge=0)
    #: ``shipped``: add the shipped reranker chain (Jina v3.5, Qwen3-Reranker-0.6B, MiniLM CPU) to
    #: the catalogue
    #: when no reranker is declared, with estimated sizes.
    default_rerankers: Literal["shipped", "none"] = "shipped"
    rerank: Literal["chain", "off"] = "chain"
    embedder_profile: Literal["auto", "cpu", "gpu"] = "auto"
    #: Per catalogue role: the tier it runs on. ``none`` turns the role off (the no-model path);
    #: otherwise the smallest declared model at that tier or above serves it.
    role_tier: dict[str, TierName] = Field(default_factory=dict)
    #: Per catalogue role: a declared model id (wins over tier choice).
    pins: dict[str, str] = Field(default_factory=dict)
    #: Hosted models may serve roles that are marked hosted-ok (reader side, tier-gated; P-02).
    allow_hosted: bool = False
    #: Catalogue roles whose absence is an error when ``on_missing_model`` is ``error``.
    required_roles: list[str] = Field(default_factory=list)
    on_missing_model: Literal["error", "rules", "skip"] = "error"
    #: P-15: role -> capped thinking token budget; honoured only for judge / reflect (background
    #: roles where a wrong decision persists). Every other role stays thinking off.
    thinking: dict[str, int] = Field(default_factory=dict)


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
    #: W2-07 (decisions W-18, P-07, P-08, P-10; LIB-46): prompt layout v2. When on, the write
    #: prompts that carry a worked example or a per-call variable in the system text
    #: (``window_extract``, ``extract@session3``, ``extract@session4``, ``extract_edges``,
    #: ``extract_edges@session``) are served from ``defaults/v2/``: no worked example, a system
    #: text that is byte-identical for every call (its hash is ``PromptIdentity``), the
    #: coarse-date partial in that stable part, and the same-language partial in the user
    #: part for non-English input only. Default off: every prompt renders as before.
    layout_v2: bool = False
    #: The layout-v2 partials left switched on (screened one at a time, RV15):
    #: ``same_language`` and/or ``coarse_date``. Ignored while ``layout_v2`` is off.
    layout_partials: list[Literal["same_language", "coarse_date"]] = Field(
        default_factory=lambda: ["same_language", "coarse_date"]  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    )


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
    #: F-04 (W-13, ER-06): the durable outbox. Off (default): window calls queue in memory and
    #: are lost on stop, exactly as before. On: every window job is a row in the ``outbox``
    #: tables first, an inline runner drains it after each write and at start (leftovers), and a
    #: stop persists what it could not finish. Model calls never run inside a namespace lock.
    outbox: bool = False
    #: RT-01 (G-19, W-13): "outbox = truth, taskiq = transport". ``inline`` (default): unchanged,
    #: the inline runner alone. ``taskiq``: with ``outbox`` on, every enqueued job id is also sent
    #: over a taskiq broker (the message carries only the id), and ``start`` re-sends every
    #: unfinished row. Without ``outbox`` it has no effect.
    transport: Literal["inline", "taskiq"] = "inline"
    #: RT-01: which broker carries the ids when ``transport="taskiq"``. ``memory``: taskiq's
    #: InMemoryBroker, drained in this process (library mode). ``redis``: a Redis stream on
    #: ``broker_url``; this process only enqueues and ``python -m memspine.api.worker`` drains.
    transport_broker: Literal["memory", "redis"] = "memory"


class DecisionConfig(BaseModel):
    """H24: the optional decision provider (calibrated choice among described options,
    no generation). ``off`` = none; ``gliner2`` uses the ``[ner]`` extra."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["off", "gliner2"] = "off"
    model: str = "fastino/gliner2-base-v1"
