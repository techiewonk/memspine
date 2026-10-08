"""Run configuration for the memspine evals harness.

This module answers one question: **what, exactly, produced this number?**

The project's own survey work found that unreported protocol variation is the single
largest source of incomparable scores in agent-memory evaluation — two papers reporting
"LoCoMo accuracy" may differ in judge model, judge rubric, backbone, token budget,
retrieval depth, sample subset, and whether the memory was rebuilt per sample. So every
knob that can move a number is a *typed field* here, and every run serialises its full
configuration into its result file (see :mod:`evals.harness.results`).

Three disciplines are encoded structurally rather than by convention:

1. **One flag = one toggle.** :class:`AblationConfig` is a flat set of independent
   booleans whose defaults *are* the reference configuration. An ablation is a run whose
   :meth:`AblationConfig.deviations` returns exactly one entry, and
   ``require_single_toggle`` makes the harness refuse anything else.
2. **Generation is separable from scoring.** :class:`RunMode` splits the two, and
   ``--score-only`` runs carry ``input_results`` so a stored run can be re-judged with a
   different judge or rubric without touching the engine.
3. **Two digests, not one.** :meth:`RunConfig.protocol_digest` covers everything that can
   change a number and is the comparability key between runs.
   :meth:`RunConfig.build_digest` covers only what changes the *constructed memory*, so
   read-side ablations reuse a cached build instead of re-ingesting (MAGMA_HARVEST §2.3).
   Misclassifying a toggle silently corrupts the cache, so the write/read partition is
   checked exhaustively at import time.

The harness lives outside the wheel (D-35): it may import ``memspine`` and may depend on
things core cannot. Nothing here ships to users.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memspine.core.registry import MEMORY_TYPES

__all__ = [
    "HARNESS_VERSION",
    "READ_SIDE_ABLATIONS",
    "WRITE_SIDE_ABLATIONS",
    "AblationConfig",
    "BudgetConfig",
    "CacheConfig",
    "Dataset",
    "DatasetConfig",
    "EmbeddingConfig",
    "GenerationConfig",
    "HarnessConfigError",
    "HarnessError",
    "JudgeConfig",
    "JudgeKind",
    "ModelConfig",
    "RunConfig",
    "RunMode",
    "SystemConfig",
    "SystemUnderTest",
    "TokenAccounting",
]

#: Harness version. Bump on any change that alters what a run *means* (new toggle, new
#: digest input, changed default). Recorded in every result file.
HARNESS_VERSION: Final[str] = "0.2.0"


class HarnessError(RuntimeError):
    """Base class for every error the evals harness raises."""


class HarnessConfigError(HarnessError):
    """A run configuration is internally inconsistent or names something unknown."""


class Dataset(StrEnum):
    """Benchmark corpora the harness knows how to load.

    Variants are separate members, not a ``size`` field, because the haystack size *is*
    the protocol: LongMemEval-S and LongMemEval-M are different numbers.
    """

    LOCOMO = "locomo"
    LOCOMO_PLUS = "locomo_plus"
    LONGMEMEVAL_S = "longmemeval_s"
    LONGMEMEVAL_M = "longmemeval_m"
    LONGMEMEVAL_ORACLE = "longmemeval_oracle"


class SystemUnderTest(StrEnum):
    """What is answering the questions.

    ``full_context`` and ``no_memory`` are the two controls every memory result needs:
    the former is the cost ceiling, the latter the accuracy floor. ``external`` is a peer
    system re-run under this harness (the practice MAGMA demonstrates and we adopt).
    """

    MEMSPINE = "memspine"
    FULL_CONTEXT = "full_context"
    NO_MEMORY = "no_memory"
    EXTERNAL = "external"


class RunMode(StrEnum):
    """Which halves of the pipeline execute.

    ``score`` is ``--score-only``: re-judge a stored result file without the engine.
    """

    GENERATE = "generate"
    SCORE = "score"
    GENERATE_AND_SCORE = "generate_and_score"


class JudgeKind(StrEnum):
    """The instrument, which is not interchangeable with the score it produces.

    A graded reference-based judge measures *answer quality*; a binary reference-free
    judge measures *reachability* of the evidence. They are different constructs and
    their numbers do not compare (MAGMA_HARVEST §4).
    """

    GRADED_REFERENCE = "graded_reference"
    BINARY_REFERENCE = "binary_reference"
    BINARY_REFERENCE_FREE = "binary_reference_free"


class TokenAccounting(StrEnum):
    """How token counts in the results were obtained.

    Provider ``usage`` fields and a local tokenizer disagree, sometimes by several
    percent, so which one produced a reported cost is part of the protocol.
    """

    PROVIDER_USAGE = "provider_usage"
    TOKENIZER = "tokenizer"
    BOTH = "both"


class _Frozen(BaseModel):
    """Config blocks are immutable: a configuration that mutates mid-run is exactly the
    bug this module exists to prevent. Use ``model_copy(update=...)`` to derive."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetConfig(_Frozen):
    """Which corpus, and which slice of it.

    Sample selection is a protocol field, not an operational one: "LoCoMo, first 10
    samples, seed 0" and "LoCoMo, all samples" are different experiments.
    """

    name: Dataset
    path: Path | None = None
    """Local corpus file/directory. ``None`` means the loader's default location."""

    split: str = "test"
    sample_ids: tuple[str, ...] = ()
    """Explicit conversation/haystack ids. Empty means "all, subject to limit/offset"."""

    offset: int = 0
    limit: int | None = None
    shuffle: bool = False
    """Shuffle sample order with the run seed *before* applying offset/limit."""

    categories: tuple[str, ...] = ()
    """Keep only questions in these dataset-native categories. Empty means all."""

    max_questions_per_sample: int | None = None
    haystack_sessions: int | None = None
    """Recorded distractor-session count where the loader can vary it."""

    corpus_sha256: str | None = None
    """Digest of the loaded corpus file, stamped by the loader. The only defence
    against silently comparing runs over two different copies of "LoCoMo"."""

    @field_validator("offset")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("dataset.offset must be >= 0")
        return value

    @field_validator("limit", "max_questions_per_sample", "haystack_sessions")
    @classmethod
    def _positive_or_none(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("must be >= 1 when set")
        return value


class ModelConfig(_Frozen):
    """One LLM role. Instantiated separately for backbone, judge and engine-internal use.

    ``temperature`` defaults to 0.0 because determinism is a claim memspine can make and
    should not trade away for a fraction of a point.
    """

    provider: str
    """``ollama`` | ``openai_compatible`` | ``bedrock`` | ``litellm`` | ``llama_cpp`` …"""

    model: str
    api_base: str | None = None
    temperature: float = 0.0
    top_p: float = 1.0
    max_output_tokens: int = 512
    seed: int | None = None
    stop: tuple[str, ...] = ()
    extra_params: dict[str, Any] = Field(default_factory=dict)
    """Provider-specific knobs, recorded verbatim so they cannot vary unnoticed."""

    def label(self) -> str:
        return f"{self.provider}/{self.model}"


class EmbeddingConfig(_Frozen):
    """Retrieval is only as reproducible as its embedder."""

    provider: str = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"
    dim: int | None = None
    normalize: bool = True
    quantization: Literal["auto", "none", "int8", "binary"] = "auto"
    """E4 (D-54). ``auto`` is memspine's own default (``vector.quantization``): it reads
    the embedder's manifest, and the default embedders declare no quantization, so
    ``auto`` and ``none`` coincide under ``profile="simple"``. Defaulting to ``none``
    here made the harness silently *override* the template whenever the runner applied
    this field, which is the reverse of what a default should do."""


class BudgetConfig(_Frozen):
    """The whole-window budget ``B`` and its decomposition.

    Mirrors the framework's split ``B = B_system + B_history + B_output + B_retrieved``.
    Reporting accuracy without the budget it was reached at is the confound that makes
    full-context baselines look free.
    """

    total_context_tokens: int = 8192
    system_tokens: int = 512
    history_tokens: int = 1024
    output_tokens: int = 512
    retrieved_tokens: int = 4096
    top_k: int = 10
    max_evidence_items: int | None = None
    token_accounting: TokenAccounting = TokenAccounting.PROVIDER_USAGE
    tokenizer: str | None = None
    """Tokenizer id when ``token_accounting`` is not provider-only, e.g. ``o200k_base``."""

    @model_validator(mode="after")
    def _fits(self) -> BudgetConfig:
        parts = self.system_tokens + self.history_tokens + self.output_tokens
        parts += self.retrieved_tokens
        if parts > self.total_context_tokens:
            raise ValueError(
                f"budget parts sum to {parts} > total_context_tokens {self.total_context_tokens}"
            )
        if self.top_k < 1:
            raise ValueError("budget.top_k must be >= 1")
        if self.token_accounting is not TokenAccounting.PROVIDER_USAGE and not self.tokenizer:
            raise ValueError("budget.tokenizer is required unless token_accounting=provider_usage")
        return self


class SystemConfig(_Frozen):
    """The system under test and how it is configured.

    For ``memspine`` this is the template/profile plus an explicit override map; the
    resolved memspine config is dumped into the result file by the runner, so the
    effective world is recorded even when a template default changes later.
    """

    kind: SystemUnderTest = SystemUnderTest.MEMSPINE
    profile: str | None = None
    """Explicit memspine ``profile`` name. ``None`` means "whatever the template
    declares" — the harness must not silently override a template's own profile."""

    template: str | None = "benchmark"
    """Config template name (``base``/``personal``/``benchmark``/…), or ``None``."""

    config_path: Path | None = None
    """User YAML layered over the template."""

    config_sha256: str | None = None
    """Digest of :attr:`config_path`'s **contents**, stamped by the runner.

    Without it the protocol digest fingerprints a *path*, so editing the file and
    re-running produces a byte-identical digest for a materially different engine —
    exactly the "two runs, same digest, different protocol" failure this module exists
    to prevent. ``corpus_sha256`` guards the corpus the same way; this is its twin for
    configuration."""

    enabled_memories: tuple[str, ...] = ("semantic", "episodic", "associative")
    """Lean-profile default: the six other types are not exercised by LoCoMo or
    LongMemEval, and enabling them is unpriced overhead (PLAN Part B §2.2)."""

    event_log_mode: Literal["full", "rolling", "ephemeral"] = "rolling"
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    memory_llm: ModelConfig | None = None
    """Engine-internal LLM role (extraction, conflict, summarisation). ``None`` means the
    lean target: **no LLM call on the write path**. Distinct from the backbone."""

    overrides: dict[str, Any] = Field(default_factory=dict)
    """Dotted-path overrides applied on top of the template (``read.hybrid: false``)."""

    external_name: str | None = None
    """Peer system id when ``kind=external`` (``a-mem``, ``mem0``, ``magma``, …)."""

    external_version: str | None = None

    @field_validator("enabled_memories")
    @classmethod
    def _known_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - MEMORY_TYPES)
        if unknown:
            raise ValueError(f"unknown memory type(s): {unknown} — valid: {sorted(MEMORY_TYPES)}")
        return value

    @model_validator(mode="after")
    def _external_named(self) -> SystemConfig:
        if self.kind is SystemUnderTest.EXTERNAL and not self.external_name:
            raise ValueError("system.external_name is required when kind=external")
        return self


class AblationConfig(_Frozen):
    """One flag = one toggle (MAGMA_HARVEST §2.2, actions M2).

    **The defaults are the reference configuration.** Every field is an independent
    switch over a single mechanism, so a single-toggle ablation is a run in which exactly
    one field differs from its default — machine-checkable via :meth:`deviations`.

    Defaults follow the lean benchmark profile (PLAN Part B §2.2): the firewall stays
    **on** because it is one deterministic gate, and leaving it on is what lets the
    result say the defence costs *this much* rather than *unknown*.
    """

    # --- write path -----------------------------------------------------------
    firewall: bool = True
    dedup: bool = True
    conflict_resolution: bool = True
    entity_extraction: bool = False
    corroboration: bool = True
    evolve_links: bool = True
    consolidation: bool = True
    graph_extraction: bool = False
    reorganize: bool = False
    """Leiden community detection. NOTE: the engine has no config switch for this stage —
    it self-skips unless ``memspine[community]`` is installed, so honouring this toggle
    is an environment change, not a config change. The exception to "one flag = one
    toggle" and the one place the runner must guard against reporting a toggle it did
    not actually apply."""

    decay: bool = True
    cold_compression: bool = False
    typed_relations: bool = False
    """PLAN Part A T1-T4. Write-side because it changes the *stored* edge taxonomy."""

    # --- read path ------------------------------------------------------------
    hybrid_retrieval: bool = True
    rerank: bool = False
    static_prefilter: bool = False
    mmr: bool = True
    cache_aware_assembly: bool = True
    assembly_compression: bool = False
    graph_retrieval: bool = True
    typed_adaptive_routing: bool = False
    validity_aware_traversal: bool = False
    trust_bounded_traversal: bool = False

    def deviations(self) -> dict[str, bool]:
        """Fields differing from the reference configuration, in declaration order."""
        out: dict[str, bool] = {}
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            if value != field.default:
                out[name] = bool(value)
        return out

    def label(self) -> str:
        """Filename-safe description of the deviation set, e.g. ``no_firewall``."""
        deviations = self.deviations()
        if not deviations:
            return "reference"
        return "+".join((name if enabled else f"no_{name}") for name, enabled in deviations.items())

    def write_side(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in sorted(WRITE_SIDE_ABLATIONS)}

    def read_side(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in sorted(READ_SIDE_ABLATIONS)}


#: Toggles that change the **constructed memory**. These enter the build cache key; a
#: run differing only in read-side toggles reuses a cached build.
WRITE_SIDE_ABLATIONS: Final[frozenset[str]] = frozenset(
    {
        "firewall",
        "dedup",
        "conflict_resolution",
        "entity_extraction",
        "corroboration",
        "evolve_links",
        "consolidation",
        "graph_extraction",
        "reorganize",
        "decay",
        "cold_compression",
        "typed_relations",
    }
)

#: Toggles that change only retrieval/assembly. Cheap to ablate over a cached build.
READ_SIDE_ABLATIONS: Final[frozenset[str]] = frozenset(
    {
        "hybrid_retrieval",
        "rerank",
        "static_prefilter",
        "mmr",
        "cache_aware_assembly",
        "assembly_compression",
        "graph_retrieval",
        "typed_adaptive_routing",
        "validity_aware_traversal",
        "trust_bounded_traversal",
    }
)


class GenerationConfig(_Frozen):
    """Answer generation. memspine's Generate stage is absent by design — ``assemble()`` returns
    a context and the caller generates — so the harness *is* the generator, and its
    prompt is as much a part of the protocol as the model."""

    backbone: ModelConfig
    answer_prompt_id: str = "evals/answer"
    answer_prompt_sha256: str | None = None
    """Digest of the rendered prompt template, stamped by the runner. Without it,
    "same backbone, same dataset" still permits two different experiments."""

    max_attempts: int = 1
    request_timeout_s: float = 120.0
    include_no_answer_option: bool = False
    """Whether the prompt licenses abstention. Changes both accuracy and its meaning."""

    @field_validator("max_attempts")
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("generation.max_attempts must be >= 1")
        return value


class JudgeConfig(_Frozen):
    """Scoring. Always recorded, even for ``mode=generate`` runs, so a later
    ``--score-only`` pass knows which instrument the run was designed for — and so a
    re-judge under a *different* one is visible as a protocol change, not hidden."""

    kind: JudgeKind = JudgeKind.GRADED_REFERENCE
    model: ModelConfig
    rubric_id: str = "evals/judge"
    rubric_version: str = "v1"
    rubric_sha256: str | None = None
    pass_threshold: float = 0.5
    """Score at or above which an item counts as correct for ``pass_rate``."""

    max_attempts: int = 2
    request_timeout_s: float = 120.0
    category_guidance: bool = True
    """Whether the rubric injects per-category instructions (date leniency and the
    like). MAGMA's does; a run without it is a different instrument."""

    @model_validator(mode="after")
    def _threshold_in_range(self) -> JudgeConfig:
        if not 0.0 <= self.pass_threshold <= 1.0:
            raise ValueError("judge.pass_threshold must lie in [0, 1]")
        return self


class CacheConfig(_Frozen):
    """Per-sample memory-build cache (MAGMA_HARVEST §2.3, action M3).

    Construction is the expensive half; caching it is what makes the read-side ablation
    matrix affordable at all. Deliberately **excluded from the protocol digest**: a
    correct cache cannot change a number. If it ever does, the bug is in the build key,
    not here — which is why :data:`WRITE_SIDE_ABLATIONS` is checked exhaustively.
    """

    enabled: bool = True
    dir: Path = Path("evals/.cache")
    """Build-cache root. Ignored by ``evals/.gitignore``; nothing here belongs in git."""

    rebuild: bool = False
    """Force reconstruction, ignoring any cached build."""


#: Fields excluded from :meth:`RunConfig.protocol_digest`. Exclusion is explicit and
#: audited (see ``_check_protocol_exclusions``) so a newly added field is fingerprinted
#: by default — the safe direction. ``concurrency`` is deliberately *not* excluded:
#: it does not change accuracy but it certainly changes measured latency.
_PROTOCOL_EXCLUDED: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "output_dir",
        "notes",
        "tags",
        "mode",
        "input_results",
        "cache",
        "require_single_toggle",
    }
)


class RunConfig(BaseModel):
    """Everything that defines one evaluation run.

    Serialised in full into every result file. If a field is missing from this model, the
    harness cannot claim to have recorded the protocol — add the field rather than
    passing the value some other way.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    dataset: DatasetConfig
    system: SystemConfig = Field(default_factory=SystemConfig)
    ablation: AblationConfig = Field(default_factory=AblationConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    generation: GenerationConfig
    judge: JudgeConfig
    cache: CacheConfig = Field(default_factory=CacheConfig)

    seed: int = 0
    concurrency: int = 1
    """Parallel samples. In the protocol digest because it moves latency numbers."""

    mode: RunMode = RunMode.GENERATE_AND_SCORE
    input_results: Path | None = None
    """Result file to re-judge. Required for ``mode=score``."""

    output_dir: Path = Path("evals/runs")
    require_single_toggle: bool = False
    """Refuse to run unless the ablation deviates from the reference in exactly one
    field. Turn on for the ablation matrix; leave off for profile comparisons."""

    harness_version: str = HARNESS_VERSION
    notes: str = ""
    tags: tuple[str, ...] = ()

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("run_id must be non-empty")
        if any(ch in cleaned for ch in '/\\:*?"<>|'):
            raise ValueError("run_id must be usable as a path segment")
        return cleaned

    @field_validator("concurrency")
    @classmethod
    def _concurrency_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("concurrency must be >= 1")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> RunConfig:
        if self.mode is RunMode.SCORE and self.input_results is None:
            raise ValueError("mode=score requires input_results (this is --score-only)")
        if self.require_single_toggle:
            deviations = self.ablation.deviations()
            if len(deviations) != 1:
                raise ValueError(
                    "require_single_toggle is set but the ablation deviates from the "
                    f"reference in {len(deviations)} field(s): {sorted(deviations)}"
                )
        return self

    # -- digests ---------------------------------------------------------------

    def protocol_payload(self) -> dict[str, Any]:
        """Everything that can change a number, as plain JSON-able data."""
        data: dict[str, Any] = self.model_dump(mode="json")
        for name in _PROTOCOL_EXCLUDED:
            data.pop(name, None)
        return data

    def protocol_digest(self) -> str:
        """Comparability key. Two runs with the same digest were run the same way."""
        return _digest(self.protocol_payload())

    #: ``DatasetConfig`` fields that change what gets *ingested*. Everything else on
    #: that model selects which **questions** are asked, which cannot change the built
    #: memory — a 10-sample pilot followed by the full run, or one ablation per
    #: category, must not re-ingest a corpus it already has.
    _BUILD_DATASET_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"name", "path", "split", "haystack_sessions", "corpus_sha256"}
    )

    def build_payload(self) -> dict[str, Any]:
        """The subset that changes the **constructed memory**, and nothing else.

        Read-side toggles, the backbone, the judge, the answer budget and every
        question-selection field are excluded so a read-side ablation reuses the cached
        build. ``system.overrides`` is narrowed the same way: the runner writes the
        *whole* override map into that field (it is the only place it can), so
        digesting it verbatim would make a read-side ablation rebuild a memory it could
        have reused. Narrowing is safe in exactly one direction — a write-side key that
        fell out of the narrowing would corrupt the cache — so the filter is driven by
        :data:`WRITE_SIDE_ABLATIONS` via the seam table's own keys, defaulting to
        *keeping* anything it cannot classify.
        """
        dataset = {
            key: value
            for key, value in self.dataset.model_dump(mode="json").items()
            if key in self._BUILD_DATASET_FIELDS
        }
        system = self.system.model_dump(mode="json")
        system["overrides"] = {
            key: value
            for key, value in self.system.overrides.items()
            if not _is_read_side_override(key)
        }
        return {
            "dataset": dataset,
            "system": system,
            "ablation": self.ablation.write_side(),
            "seed": self.seed,
            "harness_version": self.harness_version,
        }

    def build_digest(self) -> str:
        return _digest(self.build_payload())

    def build_cache_key(self, sample_id: str) -> str:
        """Cache key for one sample's constructed memory."""
        return f"{self.dataset.name.value}/{sample_id}/{self.build_digest()}"

    def sample_seed(self, sample_id: str) -> int:
        """Deterministic per-sample seed derived from the run seed."""
        material = f"{self.seed}:{sample_id}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:4], "big")

    # -- io --------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path | str) -> RunConfig:
        """Load from ``.yaml``/``.yml``/``.json``."""
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - surfaced verbatim to the operator
            raise HarnessConfigError(f"cannot read run config {source}: {exc}") from exc
        raw = json.loads(text) if source.suffix == ".json" else yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise HarnessConfigError(f"run config {source} must be a mapping")
        return cls.model_validate(raw)

    def to_file(self, path: Path | str) -> Path:
        """Write the resolved configuration beside its results."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return target


#: Dotted memspine config prefixes that only affect retrieval and assembly.
#:
#: Used to narrow ``system.overrides`` out of the build digest. Deliberately a
#: **prefix allow-list of read-only subtrees** rather than a deny-list: an override the
#: harness has never seen is treated as build-affecting, so a new knob over-invalidates
#: the cache (slow, correct) instead of under-invalidating it (fast, wrong).
_READ_ONLY_OVERRIDE_PREFIXES: Final[tuple[str, ...]] = (
    "read.hybrid",
    "read.rerank",
    "read.static_prefilter",
    "read.assembly.",
    "read.compression.",
    "read.mmr",
    "read.top_k",
)


def _is_read_side_override(key: str) -> bool:
    """True when a dotted override cannot change what a write puts in the store."""
    return any(
        key == prefix.rstrip(".") or key.startswith(prefix)
        for prefix in _READ_ONLY_OVERRIDE_PREFIXES
    )


def _digest(payload: dict[str, Any]) -> str:
    """Stable 16-hex-char digest over canonical JSON.

    ``sort_keys`` plus fixed separators makes the digest independent of field order and
    of Python version, so it is comparable across machines and across time.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _check_ablation_partition() -> None:
    """Every ablation toggle must be classified write-side or read-side, exactly once.

    An unclassified toggle would silently fall out of the build cache key, so a run with
    it flipped would reuse a build made without it — a wrong number that looks fine.
    Fail at import rather than at analysis time.
    """
    declared = set(AblationConfig.model_fields)
    classified = WRITE_SIDE_ABLATIONS | READ_SIDE_ABLATIONS
    both = WRITE_SIDE_ABLATIONS & READ_SIDE_ABLATIONS
    if both:
        raise HarnessConfigError(f"ablation toggles classified twice: {sorted(both)}")
    if declared != classified:
        missing = sorted(declared - classified)
        stale = sorted(classified - declared)
        raise HarnessConfigError(
            "ablation write/read partition is out of sync with AblationConfig — "
            f"unclassified: {missing}; unknown: {stale}"
        )


def _check_protocol_exclusions() -> None:
    """Guard against a typo silently un-excluding (or mis-excluding) a field."""
    unknown = sorted(_PROTOCOL_EXCLUDED - set(RunConfig.model_fields))
    if unknown:
        raise HarnessConfigError(f"_PROTOCOL_EXCLUDED names unknown field(s): {unknown}")


_check_ablation_partition()
_check_protocol_exclusions()
