"""Config area: model (ADR-082 / D-106): one ``model`` block, one slot per model category.

Every slot is ``{provider, <typed common keys>, settings}``. ``provider`` is resolved by the
registry; keys every provider of the category shares are typed at the slot root; keys that vary
by provider live in ``settings`` and are validated by that provider's descriptor. GLiNER
(``gliner2``) and OpenDecider (``opendecider``) settings are the exception: they keep their strict
schema here because the extraction pool and the decider factories read them directly.

There are no named profiles and no aliases (ADR-068): every old key (``embedding``, ``llm``,
``models``, ``decision``, ``read.rerank*``, ``read.decider*``, ``ingest.asset_vision*``) is
rejected by the strict current schema.

Accessors for consumers: :func:`llm_slot`, :func:`llm_slots`, :func:`extraction_slot`,
:func:`decider_slot`, :func:`gliner_settings` and :func:`weights_settings`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from memspine.config import constants
from memspine.config.schema.base import ConfigModel
from memspine.exceptions import ConfigError

if TYPE_CHECKING:
    from memspine.config.schema.root import MemspineConfig

__all__ = [
    "CONSUMER_FIELDS",
    "DeciderConsumer",
    "DeciderReadSlot",
    "DeciderSlot",
    "DeciderSlots",
    "EmbeddingSlot",
    "EscalationPolicy",
    "EscalationTarget",
    "ExtractionConsumer",
    "ExtractionSlot",
    "ExtractionSlots",
    "GlinerConsumer",
    "GlinerSettings",
    "GpuDecl",
    "LLMSlot",
    "ModelConfig",
    "ModelDecl",
    "ModelSlot",
    "OpenDeciderSettings",
    "PlacementConfig",
    "RerankConfig",
    "RerankStage",
    "StructuredConfig",
    "TopicSlot",
    "VisionSlot",
    "decider_slot",
    "extraction_slot",
    "gliner_settings",
    "llm_slot",
    "llm_slots",
    "weights_settings",
]


# ----------------------------------------------------------------------------- base slot


def _check_provider(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("provider must be non-empty and have no surrounding whitespace")
    return value


class ModelSlot(ConfigModel):
    """One model slot: a provider plus that provider's native ``settings`` (ADR-082)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _provider_trimmed(cls, value: str) -> str:
        return _check_provider(value)


# ----------------------------------------------------------------------------- GLiNER / OpenDecider

GlinerTask = Literal["entities", "classification", "records", "relations"]


class GlinerSettings(ConfigModel):
    """Strict native settings of a ``gliner2`` slot (extraction and decider)."""

    model_config = ConfigDict(extra="forbid")

    model: str = "fastino/gliner2.5-base-v1"
    revision: str | None = None
    device: Literal["cpu", "cuda"] = "cpu"
    local_files_only: bool = True
    tasks: tuple[GlinerTask, ...] = ("entities", "classification")
    inference_concurrency: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _tasks_are_unique(self) -> GlinerSettings:
        if not self.tasks:
            raise ValueError("GLiNER settings tasks must contain at least one task")
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("GLiNER settings tasks must not contain duplicates")
        if not self.model.strip() or self.model != self.model.strip():
            raise ValueError(
                "GLiNER settings model must be non-empty and have no surrounding whitespace"
            )
        if self.revision is not None and (
            not self.revision.strip() or self.revision != self.revision.strip()
        ):
            raise ValueError(
                "GLiNER settings revision must be non-empty and have no surrounding whitespace"
            )
        return self


class OpenDeciderSettings(ConfigModel):
    """Strict native settings of an ``opendecider`` slot (I28/I38; were ``read.decider_*``)."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="manjunathshiva/opendecider-nano", min_length=1)
    #: CPU by default: the GPU is usually shared with the reader.
    device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    #: Intra-op threads; 0 = the physical cores (with ``workers`` > 1, ``cores // workers``).
    threads: int = Field(default=0, ge=0)
    #: ``torch`` (fp32 as evaluated) | ``onnx`` (the published 8-bit build, CPU only).
    backend: Literal["torch", "onnx"] = "torch"
    #: torch weights dtype (``bfloat16`` agreed on 300 of 300 decisions, I38).
    dtype: Literal["float32", "bfloat16"] = "float32"


#: ADR-082 item 4 / plan section 3: per-consumer settings each weights provider names; the
#: shared-weights key (ADR-079) is the effective settings minus these.
CONSUMER_FIELDS: dict[str, frozenset[str]] = {
    "gliner2": frozenset({"tasks", "inference_concurrency"}),
    "opendecider": frozenset({"workers"}),
}

#: Providers whose settings this schema validates strictly.
_STRICT_SETTINGS: dict[str, type[ConfigModel]] = {
    "gliner2": GlinerSettings,
    "opendecider": OpenDeciderSettings,
}


def _strict_settings(provider: str, settings: Mapping[str, JsonValue]) -> None:
    model = _STRICT_SETTINGS.get(provider)
    if model is not None:
        model.model_validate(dict(settings))


# ----------------------------------------------------------------------------- embedding


class EmbeddingSlot(ModelSlot):
    """``model.embedding`` (D-08). ``hash`` is the deterministic zero-network provider for
    tests/CI; ``onnx`` (ONNX, CPU) is the production default; ``static`` is model2vec."""

    provider: str = Field(default="onnx", min_length=1)
    #: G9: the most texts one embedding call carries when ``ingest_messages`` embeds its turns.
    batch_size: int = Field(default=32, ge=1)
    #: F-03 (P-25 C): version the vector space; an embedder change writes a new space and
    #: ``reembed`` builds it in batches then switches. Off = one table per embedder.
    versioned_spaces: bool = False
    #: MD-02: a shipped embedding preset (``gpu``, ``cpu_multilingual``, more via
    #: ``register_profile``) that fills provider, model, dim and query instruction. Replaces
    #: ``embedding.profile`` and ``models.embedder_profile``. None (default) = ``auto``: the
    #: fields above are used as written.
    preset: str | None = Field(default=None, min_length=1)
    #: MD-02: pin the resolved preset beside the store at creation and refuse a different one.
    pin_preset: bool = False
    #: MD-02: accept a preset change on open (needs ``versioned_spaces``).
    migrate_preset: bool = False


# ----------------------------------------------------------------------------- llm


class StructuredConfig(ConfigModel):
    """I69: recovery for a structured-output reply that fails validation (both off)."""

    model_config = ConfigDict(extra="forbid")

    #: Re-prompt ONCE with the validation error appended.
    retry_on_error: bool = False
    #: On that retry only, ask the backend for JSON-schema constrained decoding.
    constrained_retry: bool = False


def _timeout() -> Any:
    return Field(gt=0, strict=True, allow_inf_nan=False)


def _reject_settings_timeout(settings: Mapping[str, JsonValue], where: str) -> None:
    if "timeout_seconds" in settings:
        raise ConfigError(
            f"{where}.settings.timeout_seconds was removed by ADR-082; use {where}.timeout_s"
        )


class EscalationTarget(ModelSlot):
    """One inline escalation target: an LLM slot without its own structured / escalation."""

    timeout_s: float = _timeout()

    @model_validator(mode="after")
    def _no_settings_timeout(self) -> EscalationTarget:
        _reject_settings_timeout(self.settings, "model.llm.<role>.escalation.targets[]")
        return self


class EscalationPolicy(ConfigModel):
    """Bounded retry ladder for structured LLM calls (inline targets, ADR-082 item 5)."""

    model_config = ConfigDict(extra="forbid")

    targets: tuple[EscalationTarget, ...] = ()
    triggers: tuple[Literal["error", "empty", "invalid_json"], ...] = ("error",)
    max_attempts: int = Field(default=2, ge=1)
    timeout_s: float = _timeout()
    max_tokens_per_attempt: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def _bounded_chain(self) -> EscalationPolicy:
        if not self.triggers or len(set(self.triggers)) != len(self.triggers):
            raise ValueError("escalation.triggers must be non-empty and contain no duplicates")
        keys = [(t.provider, _canonical(t.settings)) for t in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("escalation.targets must be unique")
        if self.targets and self.max_attempts < 2:
            raise ValueError("escalation.max_attempts must be at least 2 when targets are set")
        if self.max_attempts > 1 + len(self.targets):
            raise ValueError("escalation.max_attempts exceeds the primary plus target slots")
        return self


def _canonical(value: Any) -> Any:
    """A hashable, order-independent form of a settings mapping."""
    if isinstance(value, Mapping):
        return tuple(sorted((k, _canonical(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(v) for v in value)
    return value


#: P-15: thinking is honoured only for background roles where a wrong decision persists.
THINKING_ROLES: frozenset[str] = frozenset({"judge", "reflect"})


class LLMSlot(ModelSlot):
    """``model.llm.<role>``: one role's provider, deadline, structured recovery, thinking budget
    and escalation ladder. For ``provider=openai_compat``, ``settings.model`` is a OpenAICompat model id."""

    timeout_s: float = _timeout()
    structured: StructuredConfig = Field(default_factory=StructuredConfig)
    #: P-15: capped thinking token budget; judge / reflect only (validated by ``ModelConfig``).
    thinking: int | None = Field(default=None, ge=0)
    escalation: EscalationPolicy | None = None

    @model_validator(mode="after")
    def _targets_differ_from_primary(self) -> LLMSlot:
        _reject_settings_timeout(self.settings, "model.llm.<role>")
        if self.escalation is not None:
            primary = (self.provider, _canonical(self.settings))
            for target in self.escalation.targets:
                if (target.provider, _canonical(target.settings)) == primary:
                    raise ValueError(
                        "escalation target is the same model as the primary slot "
                        "(same provider and settings)"
                    )
        return self


# ----------------------------------------------------------------------------- rerank


class RerankStage(ModelSlot):
    """One reranker in ``model.rerank.chain`` (``jina``, ``qwen3``, ``onnx``, ...)."""


class RerankConfig(ConfigModel):
    """``model.rerank``: always a chain; the first entry that builds and works wins, ``[]`` is
    off, one entry is a single reranker. Behaviour keys lost their ``rerank_`` prefix."""

    model_config = ConfigDict(extra="forbid")

    chain: list[RerankStage] = Field(default_factory=list)
    #: R3-07: the pool the chain reranks is capped at this size (was ``rerank_pool``).
    pool: int = Field(default=64, ge=1, le=256)
    #: FU-RRPOOL: ``search`` fetches ``max(top_k, search_pool)``, reranks, cuts to ``top_k``;
    #: 0 = off (was ``rerank_search_pool``).
    search_pool: int = Field(default=0, ge=0, le=256)
    #: H18: rerank only when ``top_k`` is at most this; None = always.
    max_top_k: int | None = Field(default=None, ge=1)
    #: G5b: keep only the best ``keep`` candidates after reranking; None = the whole pool.
    keep: int | None = Field(default=None, ge=1)
    #: N41: None = the reranker score replaces retrieval; w in [0, 1] blends them.
    blend: float | None = Field(default=None, ge=0.0, le=1.0)
    #: N41: below this best raw score the retrieval order is kept; None = off.
    gate: float | None = None
    #: N42: neighbouring turns on each side the reranker sees with a candidate.
    context: int = Field(default=0, ge=0, le=3)
    #: Hindsight: prefix reranker inputs with ``[Date: YYYY-MM-DD]``.
    date_prefix: bool = False
    #: GR-15: the pool cut before reranking takes each leg's best hits in turn.
    balanced: bool = False
    #: I9: records longer than N characters are scored in overlapping windows (max score).
    chunk_chars: int | None = Field(default=None, ge=64)
    chunk_overlap: int = Field(default=0, ge=0)
    #: RETRIEVAL_GAPS finding 3: ``skip`` = reranked reads skip the relative floor.
    floor: Literal["minmax", "skip"] = "minmax"
    #: Agent Zero: skip the reranker for ordering questions (first / latest / ...).
    skip_for_ordering: bool = False

    @model_validator(mode="after")
    def _unique_chain(self) -> RerankConfig:
        keys = [(s.provider, _canonical(s.settings)) for s in self.chain]
        if len(set(keys)) != len(keys):
            raise ValueError("model.rerank.chain entries must be unique")
        return self


# ----------------------------------------------------------------------------- extraction

ExtractionConsumer = Literal["semantic", "prepass", "topic", "sensitivity"]
DeciderConsumer = Literal["route", "read"]
GlinerConsumer = Literal["semantic", "prepass", "topic", "sensitivity", "route", "read"]

#: The GLiNER task each consumer needs (plan section 2).
REQUIRED_TASK: dict[str, GlinerTask] = {
    "semantic": "entities",
    "prepass": "entities",
    "sensitivity": "entities",
    "topic": "classification",
    "route": "classification",
    "read": "classification",
}

#: RM-99 / RM-106 / ADR-080 decision 12: these consumers are offline only.
OFFLINE_ONLY: frozenset[str] = frozenset({"topic", "sensitivity"})


class ExtractionSlot(ModelSlot):
    """``model.extraction.<consumer>``. A ``gliner2`` slot's settings are strict."""

    @model_validator(mode="after")
    def _settings_are_valid(self) -> ExtractionSlot:
        _strict_settings(self.provider, self.settings)
        return self


class TopicSlot(ExtractionSlot):
    """``model.extraction.topic`` (RM-106): candidate labels are the active packs' domain ids
    plus the keys of ``labels`` (free label -> domain id)."""

    labels: dict[str, str] = Field(default_factory=dict)
    #: A hit sets the domain only at or above this confidence.
    min_confidence: float = Field(
        default=constants.TOPIC_MIN_CONFIDENCE, ge=0.0, le=1.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _labels_map_to_domains(self) -> TopicSlot:
        from memspine.semantics.domains import is_domain_id  # lazy: no import cycle

        for label, target in self.labels.items():
            if not label.strip() or label != label.strip():
                raise ValueError(
                    "model.extraction.topic.labels keys must be non-empty labels without "
                    "surrounding whitespace"
                )
            if not is_domain_id(target):
                raise ValueError(
                    f"model.extraction.topic.labels[{label!r}] maps to {target!r}, which is not "
                    "a domain id (lower-case segments joined by '/', e.g. 'health/condition')"
                )
        return self


class ExtractionSlots(ConfigModel):
    """``model.extraction``: one optional slot per consumer; a consumer not listed is off.
    ``topic`` and ``sensitivity`` are enabled by their slot; ``semantic`` and ``prepass`` stay
    switched by ``memories.semantic.policies.entity_extraction`` / ``write.prepass``."""

    model_config = ConfigDict(extra="forbid")

    semantic: ExtractionSlot | None = None
    prepass: ExtractionSlot | None = None
    topic: TopicSlot | None = None
    sensitivity: ExtractionSlot | None = None


# ----------------------------------------------------------------------------- decider

ReadDeciderTask = Literal["list_mode", "bridge_hop", "about_target", "needs_more_evidence"]


class DeciderSlot(ModelSlot):
    """``model.decider.route`` (H24 mode routing; was ``decision.provider``). Providers:
    ``heuristic``, ``opendecider``, ``gliner2``; later deciders register as providers."""

    #: The decider's own floor: a decision below this (or of unknown confidence) is not used and
    #: the heuristic answer stands. ``route`` (RM-118): the H24 read mode falls back to the
    #: heuristic ``auto`` routing; this is applied before ``read.planner_min_confidence``, the
    #: planner's routing floor (a choice that passes this floor but not that one reads by
    #: ``replay``). ``read``: the I28 decision points keep their heuristic label.
    min_confidence: float = Field(default=0.5, ge=0.5, le=1.0)
    #: Concurrent decision calls (a bounded thread pool). 1 keeps the engine path as it is.
    #: ``opendecider`` only: a ``gliner2`` slot rejects any other value (its concurrency is
    #: ``settings.inference_concurrency`` on the shared GLiNER pool, RM-118).
    workers: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _settings_are_valid(self) -> DeciderSlot:
        _strict_settings(self.provider, self.settings)
        return self


class DeciderReadSlot(DeciderSlot):
    """``model.decider.read`` (I28 read-path decision points; was ``read.decider*``)."""

    tasks: list[ReadDeciderTask] = Field(
        default_factory=lambda: ["list_mode", "bridge_hop"]  # type: ignore[arg-type]  # CI-GREEN: list[str] literal default
    )

    @model_validator(mode="after")
    def _unique_tasks(self) -> DeciderReadSlot:
        if len(set(self.tasks)) != len(self.tasks):
            raise ValueError("model.decider.read.tasks must not contain duplicates")
        return self


class DeciderSlots(ConfigModel):
    """``model.decider``: ``route`` absent = no decision router; ``read`` absent = the
    heuristic rules (today's default)."""

    model_config = ConfigDict(extra="forbid")

    route: DeciderSlot | None = None
    read: DeciderReadSlot | None = None

    @model_validator(mode="after")
    def _workers_need_a_thread_pool(self) -> DeciderSlots:
        """RM-118: ``workers`` sizes the OpenDecider thread pool. A ``gliner2`` decider has none:
        it runs on the shared GLiNER pool, so a non-default value would be silently ignored."""
        for consumer in ("route", "read"):
            slot: DeciderSlot | None = getattr(self, consumer)
            if slot is not None and slot.provider == "gliner2" and slot.workers != 1:
                raise ConfigError(
                    f"model.decider.{consumer}.workers has no effect for provider 'gliner2' (its "
                    "inference runs on the shared GLiNER pool); set "
                    f"model.decider.{consumer}.settings.inference_concurrency instead and leave "
                    "workers at 1"
                )
        return self


# ----------------------------------------------------------------------------- vision


class VisionSlot(ModelSlot):
    """``model.vision`` (E04; was ``ingest.asset_vision*``): ``settings`` = ``{model,
    api_base}``. ``model.vision: null`` = no description (text only)."""

    provider: Literal["ollama"] = "ollama"
    timeout_s: float = Field(default=120.0, gt=0.0, allow_inf_nan=False)


# ----------------------------------------------------------------------------- placement

TierName = Literal["none", "small-local", "worker", "frontier"]


class ModelDecl(ConfigModel):
    """MD-01: one model the deployment declares, with the memory it needs. The device plan is
    computed from these declared sizes alone (no GPU, no download). ``id`` is the OpenAICompat model id
    for an ``llm``; ``provider`` selects the adapter independently. ``family`` names a reranker.
    Catalogue entries are declarations, not slots (``timeout_seconds`` stays here)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["llm", "rerank", "embedding"] = "llm"
    #: llm only. none = serves no role; small-local = 1-4B; worker = 9B class; frontier = large.
    tier: TierName = "worker"
    provider: str = ""
    family: str = ""
    hosted: bool = False
    vram_mb: int = Field(default=0, ge=0)
    ram_mb: int = Field(default=0, ge=0)
    #: llm: memory reserved for the context window (P-19).
    context_vram_mb: int = Field(default=0, ge=0)
    #: Where it may run: ``any`` (GPU if it fits, else CPU) | ``gpu`` | ``cpu``.
    placement: Literal["any", "gpu", "cpu"] = "any"
    supports_thinking: bool = False
    #: embedding only: ``cpu`` (default multilingual) | ``gpu`` (instruction embedder).
    profile: str = ""
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
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
    """MD-01: one declared CUDA device (only CUDA is planned, MD-03)."""

    model_config = ConfigDict(extra="forbid")

    name: str = "cuda"
    vram_mb: int = Field(ge=0)


class PlacementConfig(ConfigModel):
    """``model.placement`` (MD-01; P-01, P-02, P-03, P-19): device planning over declared
    models. ``tiers`` off (default) = roles resolve from ``model.llm`` only."""

    model_config = ConfigDict(extra="forbid")

    #: Roles resolve through tier, placement and fallback (``memspine.models.resolve``); an
    #: explicit ``model.llm.<role>`` slot wins.
    tiers: bool = False
    catalogue: list[ModelDecl] = Field(default_factory=list)
    #: ``shipped``: add the shipped reranker chain to the catalogue when none is declared.
    catalogue_rerankers: Literal["shipped", "none"] = "shipped"
    gpus: list[GpuDecl] = Field(default_factory=list)
    detect_gpus: bool = False
    ram_mb: int = Field(default=0, ge=0)
    gpu_headroom_mb: int = Field(default=512, ge=0)
    role_tier: dict[str, TierName] = Field(default_factory=dict)
    pins: dict[str, str] = Field(default_factory=dict)
    allow_hosted: bool = False
    required_roles: list[str] = Field(default_factory=list)
    on_missing_model: Literal["error", "rules", "skip"] = "error"
    #: Upper bound on simultaneously loaded GLiNER checkpoints (was ``gliner_max_models``).
    max_loaded_models: int = Field(default=1, ge=1)
    #: RM-118 (P-15; was ``models.thinking``): role -> capped thinking token budget for a role the
    #: tier plan picks (``tiers``). judge / reflect only; 0 = off. A role with an explicit
    #: ``model.llm.<role>`` slot sets ``model.llm.<role>.thinking`` instead (one place per role).
    #: Honoured only on a model that declares ``supports_thinking``.
    thinking: dict[str, int] = Field(default_factory=dict)

    @field_validator("thinking")
    @classmethod
    def _thinking_roles(cls, value: dict[str, int]) -> dict[str, int]:
        for role, budget in value.items():
            if role not in THINKING_ROLES:
                raise ConfigError(
                    f"model.placement.thinking.{role} is honoured only for judge / reflect (P-15)"
                )
            if isinstance(budget, bool) or budget < 0:
                raise ConfigError(
                    f"model.placement.thinking.{role} must be a token budget >= 0 (0 = off)"
                )
        return value


# ----------------------------------------------------------------------------- the block


class ModelConfig(ConfigModel):
    """ADR-082: one ``model`` block, one slot per model category."""

    model_config = ConfigDict(extra="forbid")

    embedding: EmbeddingSlot = Field(default_factory=EmbeddingSlot)
    #: Role -> slot. A role that is not listed is off.
    llm: dict[str, LLMSlot] = Field(default_factory=dict)
    rerank: RerankConfig = Field(default_factory=RerankConfig)
    extraction: ExtractionSlots = Field(default_factory=ExtractionSlots)
    decider: DeciderSlots = Field(default_factory=DeciderSlots)
    vision: VisionSlot | None = None
    placement: PlacementConfig = Field(default_factory=PlacementConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_null_required(cls, value: Any) -> Any:
        return cls.reject_null(value)

    @staticmethod
    def reject_null(value: Any) -> Any:
        """Only ``model.vision`` may be null; the root calls this before the strict input check
        so the error names the slot."""
        if isinstance(value, Mapping):
            for slot in ("embedding", "rerank", "extraction", "decider", "placement"):
                if slot in value and value[slot] is None:
                    raise ConfigError(f"model.{slot} cannot be null (only model.vision may be)")
        return value

    @field_validator("llm")
    @classmethod
    def _known_roles(cls, value: dict[str, LLMSlot]) -> dict[str, LLMSlot]:
        from memspine.prompts.roles import CATALOGUE_ROLE, PROMPT_ROLES  # lazy: no cycle

        known = set(PROMPT_ROLES) | set(CATALOGUE_ROLE)
        for role, slot in value.items():
            if role not in known:
                raise ConfigError(f"model.llm has unknown role {role!r}")
            if slot.thinking is not None and role not in THINKING_ROLES:
                raise ConfigError(
                    f"model.llm.{role}.thinking is honoured only for judge / reflect (P-15)"
                )
        return value

    @model_validator(mode="after")
    def _thinking_in_one_place(self) -> ModelConfig:
        """RM-118: a role's thinking budget lives in exactly one place. A role with an explicit
        ``model.llm`` slot is never planned, so ``model.placement.thinking`` would be inert."""
        for role in sorted(self.placement.thinking):
            if role in self.llm:
                raise ConfigError(
                    f"model.placement.thinking.{role} is set but model.llm.{role} is an explicit "
                    f"slot; use model.llm.{role}.thinking (one place per role)"
                )
        return self


# ----------------------------------------------------------------------------- accessors


def llm_slot(config: MemspineConfig, role: str) -> LLMSlot | None:
    """The slot bound to an LLM ``role``, or None when the role is off."""
    return config.model.llm.get(role)


def llm_slots(config: MemspineConfig) -> dict[str, LLMSlot]:
    """Every bound LLM role (a copy of ``model.llm``)."""
    return dict(config.model.llm)


def extraction_slot(config: MemspineConfig, consumer: ExtractionConsumer) -> ExtractionSlot | None:
    """``model.extraction.<consumer>``, or None when the consumer has no slot."""
    slot: ExtractionSlot | None = getattr(config.model.extraction, consumer)
    return slot


def decider_slot(config: MemspineConfig, consumer: DeciderConsumer) -> DeciderSlot | None:
    """``model.decider.<consumer>``, or None (``route`` off / ``read`` = heuristic rules)."""
    slot: DeciderSlot | None = getattr(config.model.decider, consumer)
    return slot


def gliner_settings(config: MemspineConfig, consumer: GlinerConsumer) -> GlinerSettings:
    """The strict GLiNER settings of a consumer's ``gliner2`` slot (replaces
    ``resolve_gliner_settings``). Raises ``ConfigError`` naming the slot when it is absent or
    not ``gliner2``."""
    slot: ModelSlot | None
    if consumer == "route" or consumer == "read":
        slot = decider_slot(config, consumer)
        path = f"model.decider.{consumer}"
    else:
        slot = extraction_slot(config, consumer)
        path = f"model.extraction.{consumer}"
    if slot is None:
        raise ConfigError(f"{path} is not configured")
    if slot.provider != "gliner2":
        raise ConfigError(f"{path} uses provider {slot.provider!r}, not 'gliner2'")
    return GlinerSettings.model_validate(slot.settings)


def weights_settings(slot: ModelSlot) -> dict[str, JsonValue]:
    """The settings that identify a slot's loaded weights (ADR-082 item 4, ADR-079): the
    effective settings minus the consumer-only fields of :data:`CONSUMER_FIELDS`, so several
    consumers of one checkpoint share one load. Strictly validated providers include their
    defaults, so an omitted default and an explicit one give the same key."""
    drop = CONSUMER_FIELDS.get(slot.provider, frozenset())
    strict = _STRICT_SETTINGS.get(slot.provider)
    if strict is not None:
        effective: dict[str, JsonValue] = strict.model_validate(dict(slot.settings)).model_dump(
            mode="json"
        )
    else:
        effective = dict(slot.settings)
    return {key: value for key, value in effective.items() if key not in drop}
