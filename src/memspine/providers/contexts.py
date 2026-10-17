"""Category-specific construction dependencies; builders never receive Engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from memspine.config.schema.storage import EventLogConfig
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.providers.contracts import LifecycleManager, ProjectionPlacement, SqlAccess


@dataclass(frozen=True)
class SharedBuildContext:
    resources: LifecycleManager
    owner_id: str
    health_timeout_s: float


@dataclass(frozen=True)
class StorageBuildContext:
    shared: SharedBuildContext
    event_log: EventLogConfig
    profile: Literal["embedded", "server"]


@dataclass(frozen=True)
class CacheBuildContext:
    shared: SharedBuildContext
    placement: ProjectionPlacement


@dataclass(frozen=True)
class VectorBuildContext:
    shared: SharedBuildContext
    placement: ProjectionPlacement
    embedding: EmbeddingService
    quantization: str | None
    matryoshka_dim: int | None
    profile_meta: Mapping[str, JsonValue] | None
    versioned_spaces: bool
    query_instruction: str | None
    generation_id: str = "active"
    sql: SqlAccess | None = None


@dataclass(frozen=True)
class GraphBuildContext:
    shared: SharedBuildContext
    placement: ProjectionPlacement
    sql: SqlAccess | None
    generation_id: str = "active"
    resource_id: str = "graph"


@dataclass(frozen=True)
class LexicalBuildContext:
    shared: SharedBuildContext
    placement: ProjectionPlacement
    generation_id: str = "active"


@dataclass(frozen=True)
class LLMBuildContext:
    shared: SharedBuildContext
    role: str


@dataclass(frozen=True)
class RerankerBuildContext:
    """Engine-local lifecycle and scoring options for a lazy reranker build."""

    shared: SharedBuildContext
    chunk_chars: int | None = None
    chunk_overlap: int = 0
