"""memspine evaluation harness.

Lives outside the wheel (D-35): not shipped, and free to depend on what core cannot.
Two modules carry the contract that everything else in ``evals/`` builds on —
:mod:`evals.harness.config` (what produced a number) and :mod:`evals.harness.results`
(the number, its cost, and everything needed to re-judge it).

Run from the repository root: ``uv run python -m evals.run --help``. ``evals/`` is an
implicit namespace package with no ``__init__.py`` on purpose, so it is never mistaken
for part of the distribution.

:mod:`evals.harness.accounting` deliberately imports nothing — not memspine, not
pydantic, not the two modules above — so the cost arithmetic is testable in isolation.
The dependency runs one way: :mod:`~evals.harness.results` knows about accounting (see
``stage_costs_from_report``), never the reverse. It is not re-exported here for the same
reason.
"""

from __future__ import annotations

from .config import (
    HARNESS_VERSION,
    READ_SIDE_ABLATIONS,
    WRITE_SIDE_ABLATIONS,
    AblationConfig,
    BudgetConfig,
    CacheConfig,
    Dataset,
    DatasetConfig,
    EmbeddingConfig,
    GenerationConfig,
    HarnessConfigError,
    HarnessError,
    JudgeConfig,
    JudgeKind,
    ModelConfig,
    RunConfig,
    RunMode,
    SystemConfig,
    SystemUnderTest,
    TokenAccounting,
)
from .results import (
    BUILD_STAGES,
    QUERY_STAGES,
    SCHEMA_VERSION,
    BuildRecord,
    CostPerCycle,
    Environment,
    ItemResult,
    JudgeResult,
    LexicalScores,
    LoopStage,
    MemoryTypeBreakdown,
    RetrievedItem,
    RunResults,
    RunSummary,
    SchemaVersionError,
    ScoreBreakdown,
    StageCost,
    TokenCounts,
    build_results,
    stage_costs_from_report,
    summarise,
    utcnow,
)

__all__ = [
    "BUILD_STAGES",
    "HARNESS_VERSION",
    "QUERY_STAGES",
    "READ_SIDE_ABLATIONS",
    "SCHEMA_VERSION",
    "WRITE_SIDE_ABLATIONS",
    "AblationConfig",
    "BudgetConfig",
    "BuildRecord",
    "CacheConfig",
    "CostPerCycle",
    "Dataset",
    "DatasetConfig",
    "EmbeddingConfig",
    "Environment",
    "GenerationConfig",
    "HarnessConfigError",
    "HarnessError",
    "ItemResult",
    "JudgeConfig",
    "JudgeKind",
    "JudgeResult",
    "LexicalScores",
    "LoopStage",
    "MemoryTypeBreakdown",
    "ModelConfig",
    "RetrievedItem",
    "RunConfig",
    "RunMode",
    "RunResults",
    "RunSummary",
    "SchemaVersionError",
    "ScoreBreakdown",
    "StageCost",
    "SystemConfig",
    "SystemUnderTest",
    "TokenAccounting",
    "TokenCounts",
    "build_results",
    "stage_costs_from_report",
    "summarise",
    "utcnow",
]
