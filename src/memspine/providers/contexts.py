"""Category-specific construction dependencies; builders never receive Engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from memspine.config.schema.runtime import SharedHttpPoolConfig, SharedSqlPoolConfig
from memspine.config.schema.storage import EventLogConfig
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.providers.contracts import LifecycleManager, ProjectionPlacement, SqlAccess
from memspine.providers.shared import ResourceRegistry, shared_registry


@dataclass(frozen=True)
class SharedBuildContext:
    resources: LifecycleManager
    owner_id: str
    health_timeout_s: float
    #: RM-78: the Engine's resource registry (``runtime.shared_resources.scope``); None =
    #: the process registry.
    registry: ResourceRegistry | None = None
    #: RM-78: pool options for SQL engines and HTTP clients memspine constructs; None = the
    #: library defaults (direct builds outside an Engine).
    sql_pool: SharedSqlPoolConfig | None = None
    http_pool: SharedHttpPoolConfig | None = None

    def resource_registry(self) -> ResourceRegistry:
        return self.registry if self.registry is not None else shared_registry()


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
    #: Stable provider-owned resource identity; profile bindings set this when one logical role
    #: can own multiple native provider instances. Inline role builders keep the role-based key.
    resource_key: str | None = None


@dataclass(frozen=True)
class RerankerBuildContext:
    """Engine-local lifecycle and scoring options for a lazy reranker build."""

    shared: SharedBuildContext
    chunk_chars: int | None = None
    chunk_overlap: int = 0
