"""Config area: root (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memspine.config.schema.firewall import FirewallConfig
from memspine.config.schema.integrity import IntegrityConfig
from memspine.config.schema.isolation import IsolationConfig
from memspine.config.schema.llm import (
    DecisionConfig,
    EmbeddingConfig,
    LLMConfig,
    PromptsConfig,
    WorkersConfig,
)
from memspine.config.schema.observability import ObservabilityConfig
from memspine.config.schema.offline import OfflineConfig
from memspine.config.schema.privacy import (
    AuditConfig,
    ConsentConfig,
    PrivacyConfig,
    RetentionConfig,
)
from memspine.config.schema.read import ReadConfig
from memspine.config.schema.rest import RestConfig
from memspine.config.schema.storage import (
    CacheConfig,
    EventLogConfig,
    GraphConfig,
    StorageConfig,
    VectorConfig,
)
from memspine.config.schema.write import DataShapeConfig, IngestConfig, WriteConfig
from memspine.core.registry import validate_types
from memspine.exceptions import ConfigError
from memspine.schemas.public.plugins import CapabilityManifest
from memspine.store.scope import validate_namespace


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
    offline: OfflineConfig = Field(default_factory=OfflineConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    rest: RestConfig = Field(default_factory=RestConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    memories: dict[str, MemoryTypeConfig] = Field(default_factory=dict)
    namespaces: dict[str, NamespaceConfig] = Field(default_factory=dict)
    #: MOD-02 / ER-34: pinned third-party plug-ins. Discovery is not enablement: an entry-point
    #: provider is used only when listed here with ``name``, ``slot`` (``<kind>:<name>``), an exact
    #: ``version`` pin, ``lifetime`` and optionally ``hash`` (``sha256:`` of the dist's RECORD).
    #: Empty (default) = no plug-in code is imported.
    plugins: list[CapabilityManifest] = Field(default_factory=list)

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
