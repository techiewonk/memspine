"""Benchmark dataset adapters for the memspine evals harness.

Each adapter in this package does three things and nothing else:

1. **Parse** a benchmark's published layout into frozen, fully typed records,
   preserving whatever label taxonomy the benchmark defines so per-category
   statistics stay reportable end to end.
2. **Ingest** those records into an engine through the *normal* write path, so
   the measured write cost is the real one.
3. **Cache** the built store per sample, keyed by ``(dataset, sample_id, profile,
   config_hash, options_hash, adapter_version)``, so read-side ablations pay for
   construction once and a write-side ablation cannot reuse a build made without it.

:mod:`evals.legacy_datasets.base` owns everything the two adapters must agree on: the neutral
sample model, the outcome vocabulary, the ingestion-driver protocol, the namespace rule
and the build cache. Two benchmarks cannot report one comparable frontier through two
incompatible cost types, so there is exactly one of each.

:func:`evals.legacy_datasets.base.load_dataset` is the harness's entry point — it resolves the
adapter lazily by name, so loading LoCoMo never imports the LongMemEval adapter and vice
versa, and yields :class:`~evals.legacy_datasets.base.BenchmarkSample` either way.

Scoring, judging, CLI wiring and engine configuration live elsewhere in ``evals/``;
adapters import none of it.

**No corpus is vendored here.** Benchmark data carries its own licence (LoCoMo is
Snap Inc.'s, released under its own terms; LongMemEval is the authors') and is never
committed to this repository — every loader reads from a path the operator supplies.
The only fixtures in the test suite are hand-written and inline.

This package lives under ``evals/`` at the repo root, outside the shipped wheel
(D-19/D-35): nothing in ``src/memspine`` may import it, and it may depend on dev
tooling the wheel must never carry.
"""

from __future__ import annotations

from .base import (
    LOADERS,
    MANIFEST_VERSION,
    Ability,
    AnswerOutcome,
    BenchmarkQuery,
    BenchmarkSample,
    BuildCache,
    BuildKey,
    BuildManifest,
    BuildSlot,
    CacheContractError,
    ChatTurn,
    DatasetAdapter,
    IngestionDriver,
    IngestionStats,
    IngestUnit,
    config_hash,
    content_digest,
    drive_ingestion,
    is_false_abstention,
    known_datasets,
    load_dataset,
    options_digest,
    sample_namespace,
    scored_correct,
)
from .locomo import (
    ABILITY_BY_CATEGORY,
    CATEGORY_LABELS,
    BuildOutcome,
    EngineFactory,
    IngestOptions,
    LocomoAdapter,
    LocomoCategory,
    LocomoFormatError,
    LocomoQuestion,
    LocomoSample,
    LocomoSession,
    LocomoTurn,
    SupportsEngineWrite,
    build_or_load_memory,
    category_counts,
    ingest_sample,
    iter_locomo,
    load_locomo,
    parse_locomo,
    resolve_dataset_path,
)
from .longmemeval import (
    ABILITY_BY_QUESTION_TYPE,
    ABSTENTION_SUFFIX,
    LongMemEvalAdapter,
    LongMemEvalFormatError,
    SessionOrder,
    build_sample,
)

__all__ = [
    "ABILITY_BY_CATEGORY",
    "ABILITY_BY_QUESTION_TYPE",
    "ABSTENTION_SUFFIX",
    "CATEGORY_LABELS",
    "LOADERS",
    "MANIFEST_VERSION",
    "Ability",
    "AnswerOutcome",
    "BenchmarkQuery",
    "BenchmarkSample",
    "BuildCache",
    "BuildKey",
    "BuildManifest",
    "BuildOutcome",
    "BuildSlot",
    "CacheContractError",
    "ChatTurn",
    "DatasetAdapter",
    "EngineFactory",
    "IngestOptions",
    "IngestUnit",
    "IngestionDriver",
    "IngestionStats",
    "LocomoAdapter",
    "LocomoCategory",
    "LocomoFormatError",
    "LocomoQuestion",
    "LocomoSample",
    "LocomoSession",
    "LocomoTurn",
    "LongMemEvalAdapter",
    "LongMemEvalFormatError",
    "SessionOrder",
    "SupportsEngineWrite",
    "build_or_load_memory",
    "build_sample",
    "category_counts",
    "config_hash",
    "content_digest",
    "drive_ingestion",
    "ingest_sample",
    "is_false_abstention",
    "iter_locomo",
    "known_datasets",
    "load_dataset",
    "load_locomo",
    "options_digest",
    "parse_locomo",
    "resolve_dataset_path",
    "sample_namespace",
    "scored_correct",
]
