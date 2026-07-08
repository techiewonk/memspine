"""Config tree (D-11). Constructed by the loader from merged layers.

``namespaces.<ns>.memories`` is RESERVED for v0.2 per-namespace type enablement
(D-14): the key is rejected today so configs written now stay forward-compatible.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from memspine.config import constants
from memspine.core.events import EventLogMode
from memspine.core.namespace import validate_namespace
from memspine.core.registry import validate_types
from memspine.exceptions import ConfigError

__all__ = [
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
    model_config = ConfigDict(extra="forbid")

    path: str = "./memspine.db"


class EmbeddingConfig(BaseModel):
    """Embedder defaults (D-08). ``hash`` is the deterministic zero-network
    provider for tests/CI; ``fastembed`` (ONNX, CPU) is the production default."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "fastembed"  # fastembed | hash
    model: str = "BAAI/bge-small-en-v1.5"


class VectorConfig(BaseModel):
    """Vector store selection (D-09). ``auto`` prefers lancedb when installed
    and falls back to the zero-dep SQLite brute-force store."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "auto"  # auto | lance | sqlite


class GraphConfig(BaseModel):
    """Graph store selection (D-26). ``sqlite_adjacency`` is the zero-dep v0.1
    default (ladybugdb is not on PyPI yet — its ``[graph]`` extra is empty);
    ``kuzu`` is the first-class embedded-Cypher alternative behind ``[kuzu]``.
    The store is only constructed when associative memory is enabled or this
    block is set explicitly — ``profile="simple"`` never touches it."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "sqlite_adjacency"  # sqlite_adjacency | kuzu | ladybug | neo4j


class LLMRoleConfig(BaseModel):
    """One provider binding per role (D-07/D-22). The default base_url is the
    Ollama endpoint; any OpenAI-compatible host works unchanged (D-39)."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai_compat"  # openai_compat | llama_cpp | bedrock
    base_url: str = "http://localhost:11434/v1"
    model: str = ""
    api_key: str | None = None
    timeout_seconds: float = 60.0


class LLMConfig(BaseModel):
    """Per-role providers: extract / judge / chat (M14). Roles absent here are
    disabled; the engine only requires them when a feature needs the role."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, LLMRoleConfig] = Field(default_factory=dict)


class PromptsConfig(BaseModel):
    """User prompt customization (D-43): per-prompt overrides that ride the
    ordinary config layering. Keys under an override: body / system / format /
    version / output_model / token_budget (validated by the registry)."""

    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class WorkersConfig(BaseModel):
    """Background runner selection (D-16): inline (default) / dbos [dbos] /
    taskiq [taskiq] (P7, D-42 §3). Validated against the known set at engine
    start. ``broker_url`` is the Redis/Valkey endpoint the taskiq runner's
    per-scope streams live on; ignored by the other runners."""

    model_config = ConfigDict(extra="forbid")

    runner: str = "inline"
    broker_url: str = "redis://localhost:6379/0"


class ReadConfig(BaseModel):
    """ReadPolicy bindings (M12) + the E8/E5 opt-in stages (D-51, default OFF).

    ``scoring``/``assembly`` flow into ``ScoringPolicy.bind`` /
    ``AssemblyPolicy.bind``; per-namespace overrides ride the D-14
    policy-override channel later.

    - ``rerank``: ``off`` (default) | ``fastembed`` (ONNX cross-encoder) |
      ``flashrank`` (``[rerank]`` extra) — E8 rerank stage over the candidate
      set, fed concat_background text (D-42 §5).
    - ``static_prefilter``: cheap lexical-overlap gate before rerank/score (E8).
    - ``hybrid``: fuse the vector leg with a lexical BM25 leg via RRF (D-25).
      Default OFF — off means bit-identical results to the vector-only pipeline
      and no lexical index is built. Default-on is the intended v0.2 flip
      (D-25's core-default intent), held back only for backward-compat.
    - ``compression``: options for the E5 assembly-stage ``CompressionPolicy``
      binding (``{"assembly": true, "assembly_stage": [...]}``); the master
      switch defaults off so ``profile="simple"`` behavior never changes.
    """

    model_config = ConfigDict(extra="forbid")

    scoring: dict[str, Any] = Field(default_factory=dict)
    assembly: dict[str, Any] = Field(default_factory=dict)
    rerank: str = "off"
    static_prefilter: bool = False
    hybrid: bool = False
    compression: dict[str, Any] = Field(default_factory=dict)


class MemoryTypeConfig(BaseModel):
    """Per-instance enablement + per-type policy overrides (D-14)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policies: dict[str, Any] = Field(default_factory=dict)


class NamespaceConfig(BaseModel):
    """Per-namespace *policy* overrides only (D-14)."""

    model_config = ConfigDict(extra="forbid")

    policies: dict[str, Any] = Field(default_factory=dict)


class MemspineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = "simple"
    strict_services: bool = True
    event_log: EventLogConfig = Field(default_factory=EventLogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
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
