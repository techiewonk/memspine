"""Config area: llm (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, JsonValue, SecretStr, model_validator

from memspine.config import constants
from memspine.config.schema.base import ConfigModel


class EmbeddingConfig(ConfigModel):
    """Embedder defaults (D-08). ``hash`` is the deterministic zero-network
    provider for tests/CI; ``fastembed`` (ONNX, CPU) is the production default;
    ``static`` is the cheap model2vec table (``[static]``, E4/ADR-020)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "fastembed"
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    #: G9: the most texts one embedding call carries when ``ingest_messages``
    #: embeds its turns up front (Cohere on Bedrock accepts up to 96).
    batch_size: int = Field(default=32, ge=1)
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


class LLMRoleConfig(ConfigModel):
    """Explicit selected-provider binding with descriptor-owned native settings.

    For ``provider=litellm``, settings.model is a LiteLLM model ID (e.g.
    ollama/llama3). Plugin descriptors may define different settings entirely.
    The model ID never silently selects another role provider.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)


class StructuredConfig(ConfigModel):
    """I69: recovery for a structured-output reply that fails validation. Both default
    off (measure first: ``Engine.structured_stats()``)."""

    model_config = ConfigDict(extra="forbid")

    #: Re-prompt ONCE with the validation error appended.
    retry_on_error: bool = False
    #: On that retry only, ask the backend for JSON-schema constrained decoding
    #: (``response_format`` json_schema: OpenAI-compatible servers, Ollama via LiteLLM,
    #: llama.cpp); a backend that refuses it falls back to a plain retry.
    constrained_retry: bool = False


class LLMConfig(ConfigModel):
    """Per-role providers: extract / judge / chat (M14). Roles absent here are
    disabled; the engine only requires them when a feature needs the role."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)
    structured: StructuredConfig = Field(default_factory=StructuredConfig)


TierName = Literal["none", "small-local", "worker", "frontier"]


class ModelDecl(ConfigModel):
    """MD-01: one model the deployment declares, with the memory it needs. The device plan is
    computed from these declared sizes alone (no GPU, no download). ``id`` is the LiteLLM model id
    for an ``llm`` (for example ``ollama/qwen3.5:9b`` or ``llamacpp/<path>``); ``provider`` selects
    the adapter independently of that id. ``family`` names a reranker."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["llm", "reranker", "embedder"] = "llm"
    #: llm only. none = serves no role; small-local = 1-4B worker class; worker = today's 9B class;
    #: frontier = large local or hosted.
    tier: TierName = "worker"
    #: Explicit adapter selection for llm declarations (for example ``litellm`` or ``llamacpp``).
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
    timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    #: Optional per-model generation defaults. Omitted values leave the selected adapter's own
    #: defaults in force; explicit zero temperature is meaningful and is preserved.
    temperature: float | None = Field(default=None, ge=0, strict=True, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, strict=True, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _explicit_llm_timeout(self) -> ModelDecl:
        if self.kind == "llm":
            if not self.provider.strip():
                raise ValueError("LLM catalogue declarations require an explicit provider")
            if self.timeout_seconds is None:
                raise ValueError("LLM catalogue declarations require explicit timeout_seconds")
        return self


class GpuDecl(ConfigModel):
    """MD-01: one declared CUDA device. Only CUDA is planned (MD-03: the AMD iGPU through DirectML
    is 5-8x slower than the CPU and resets the driver), so a non-NVIDIA name is ignored."""

    model_config = ConfigDict(extra="forbid")

    name: str = "cuda"
    vram_mb: int = Field(ge=0)


class GlinerSettings(ConfigModel):
    """Strict native settings for a GLiNER model profile."""

    model_config = ConfigDict(extra="forbid")

    model: str = "fastino/gliner2.5-base-v1"
    revision: str | None = None
    device: Literal["cpu", "cuda"] = "cpu"
    local_files_only: bool = True
    tasks: tuple[Literal["entities", "classification", "records", "relations"], ...] = (
        "entities",
        "classification",
    )
    inference_concurrency: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _tasks_are_unique(self) -> GlinerSettings:
        if not self.tasks:
            raise ValueError("GLiNER profile tasks must contain at least one task")
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("GLiNER profile tasks must not contain duplicates")
        if not self.model.strip() or self.model != self.model.strip():
            raise ValueError(
                "GLiNER profile model must be non-empty and have no surrounding whitespace"
            )
        if self.revision is not None and (
            not self.revision.strip() or self.revision != self.revision.strip()
        ):
            raise ValueError(
                "GLiNER profile revision must be non-empty and have no surrounding whitespace"
            )
        return self


class ModelProfile(ConfigModel):
    """Named provider configuration, validated only as data until a consumer resolves it."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["llm", "gliner"]
    provider: str = Field(min_length=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _profile_settings(self) -> ModelProfile:
        if not self.provider.strip() or self.provider != self.provider.strip():
            raise ValueError(
                "model profile provider must be non-empty and have no surrounding whitespace"
            )
        if self.kind == "gliner":
            if self.provider != "gliner2":
                raise ValueError("GLiNER profiles require provider 'gliner2'")
            GlinerSettings.model_validate(self.settings)
        return self


class EscalationPolicy(ConfigModel):
    """Bounded retry ladder for structured LLM calls."""

    model_config = ConfigDict(extra="forbid")

    targets: tuple[str, ...] = ()
    triggers: tuple[Literal["error", "empty", "invalid_json"], ...] = ("error",)
    max_attempts: int = Field(default=2, ge=1)
    timeout_s: float = Field(gt=0, strict=True, allow_inf_nan=False)
    max_tokens_per_attempt: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def _bounded_chain(self) -> EscalationPolicy:
        if not self.triggers or len(set(self.triggers)) != len(self.triggers):
            raise ValueError(
                "models.escalation.triggers must be non-empty and contain no duplicates"
            )
        if any(not target.strip() or target != target.strip() for target in self.targets):
            raise ValueError("models.escalation.targets must be non-empty profile names")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("models.escalation.targets must be unique")
        if self.targets and self.max_attempts < 2:
            raise ValueError(
                "models.escalation.max_attempts must be at least 2 when targets are set"
            )
        if self.max_attempts > 1 + len(self.targets):
            raise ValueError(
                "models.escalation.max_attempts exceeds the primary plus target profiles"
            )
        return self


class TopicConfig(ConfigModel):
    """RM-106: how the opt-in ``gliner_topic`` tagger maps classification hits to domains.

    Candidate labels are the domain ids the active vocabulary packs declare plus the keys of
    ``labels`` (free topic label -> domain id). Inert unless ``models.bindings.gliner_topic``
    is bound."""

    model_config = ConfigDict(extra="forbid")

    #: Operator label map: free topic label -> hierarchical domain id (validated at load).
    labels: dict[str, str] = Field(default_factory=dict)
    #: A hit sets the domain only at or above this confidence.
    min_confidence: float = Field(
        default=constants.TOPIC_MIN_CONFIDENCE, ge=0.0, le=1.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _labels_map_to_domains(self) -> TopicConfig:
        from memspine.semantics.domains import is_domain_id  # lazy: no import cycle

        for label, target in self.labels.items():
            if not label.strip() or label != label.strip():
                raise ValueError(
                    "models.topic.labels keys must be non-empty labels without surrounding "
                    "whitespace"
                )
            if not is_domain_id(target):
                raise ValueError(
                    f"models.topic.labels[{label!r}] maps to {target!r}, which is not a domain "
                    "id (lower-case segments joined by '/', e.g. 'health/condition')"
                )
        return self


class ModelsConfig(ConfigModel):
    """MD-01 (P-01, P-02, P-03, P-15, P-19): model tiers, placement, no-model fallbacks and thinking
    per role. ``tiers_v2`` off (default) = roles resolve exactly as before, from ``llm.roles``."""

    model_config = ConfigDict(extra="forbid")

    #: Named provider profiles are data-only until resolved by an active consumer.
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    #: Consumer role -> named model profile. GLiNER consumers use the three fixed role names.
    bindings: dict[str, str] = Field(default_factory=dict)
    #: Upper bound on simultaneously loaded GLiNER checkpoints.
    gliner_max_models: int = Field(default=1, ge=1)
    #: Per-LLM-role bounded escalation policy.
    escalation: dict[str, EscalationPolicy] = Field(default_factory=dict)
    #: RM-106: label sources and threshold of the opt-in ``gliner_topic`` tagger.
    topic: TopicConfig = Field(default_factory=TopicConfig)

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


class PromptsConfig(ConfigModel):
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


class TemporalWorkerSettings(ConfigModel):
    """Remote Temporal connection and bounded workflow execution settings."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1)
    address: str = Field(default="localhost:7233", min_length=1)
    namespace: str = Field(default="default", min_length=1)
    task_queue: str | None = Field(default=None, min_length=1)
    tls: bool = False
    api_key: SecretStr | None = None
    rpc_timeout_s: float = Field(default=30.0, gt=0, le=3600)
    activity_timeout_s: float = Field(default=300.0, gt=0, le=86400)
    workflow_timeout_s: float = Field(default=3600.0, gt=0, le=604800)
    max_concurrent_activities: int = Field(default=4, ge=1, le=1024)
    retry_max_attempts: int = Field(default=3, ge=1, le=100)
    max_result_bytes: int = Field(default=65536, ge=1, le=1048576)


class ResonateWorkerSettings(ConfigModel):
    """Embedded Resonate promise execution and SQLite step journal settings."""

    model_config = ConfigDict(extra="forbid")

    journal_path: str = Field(default="./memspine-resonate.sqlite", min_length=1)
    task_timeout_s: float = Field(default=270.0, gt=0, le=604800)
    step_timeout_s: float = Field(default=240.0, gt=0, le=86400)
    step_lease_s: float = Field(default=360.0, gt=0, le=86400)
    journal_busy_timeout_s: float = Field(default=5.0, gt=0, le=3600)
    max_concurrent_tasks: int = Field(default=4, ge=1, le=1024)

    @model_validator(mode="after")
    def _require_persistent_journal(self) -> ResonateWorkerSettings:
        if self.journal_path == ":memory:" or self.journal_path.startswith("file:"):
            raise ValueError("workers.resonate.journal_path must be a persistent filesystem path")
        return self


class WorkersConfig(ConfigModel):
    """Background runner selection and provider-owned runner settings."""

    model_config = ConfigDict(extra="forbid")

    runner: Literal["inline", "temporal", "taskiq"] = "inline"
    broker_url: str = "redis://localhost:6379/0"
    temporal: TemporalWorkerSettings | None = None
    resonate: ResonateWorkerSettings = Field(default_factory=ResonateWorkerSettings)
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

    @model_validator(mode="after")
    def _validate_temporal_deadlines(self) -> WorkersConfig:
        if self.runner != "temporal":
            return self
        temporal = self.temporal
        if temporal is None:
            raise ValueError(
                "workers.temporal with deployment_id is required for runner='temporal'"
            )
        resonate = self.resonate
        if resonate.step_lease_s <= temporal.activity_timeout_s + resonate.journal_busy_timeout_s:
            raise ValueError(
                "workers.resonate.step_lease_s must exceed temporal.activity_timeout_s "
                "plus journal_busy_timeout_s"
            )
        if resonate.task_timeout_s <= resonate.step_timeout_s:
            raise ValueError("workers.resonate.task_timeout_s must exceed step_timeout_s")
        if temporal.activity_timeout_s <= resonate.task_timeout_s:
            raise ValueError(
                "workers.temporal.activity_timeout_s must exceed resonate.task_timeout_s"
            )
        activity_budget = (
            temporal.activity_timeout_s * temporal.retry_max_attempts
            + resonate.step_lease_s * (temporal.retry_max_attempts - 1)
        )
        if temporal.workflow_timeout_s <= activity_budget:
            raise ValueError(
                "workers.temporal.workflow_timeout_s must exceed the configured Activity retry budget"
            )
        return self


class DecisionConfig(ConfigModel):
    """H24: the optional decision provider (calibrated choice among described options,
    no generation). ``off`` = none; ``gliner2`` uses the ``[ner]`` extra."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["off", "gliner2"] = "off"
