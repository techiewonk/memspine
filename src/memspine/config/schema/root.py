"""Config area: root (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cache
from typing import Any, Self, SupportsIndex

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from memspine.config.requires import check_requires
from memspine.config.schema.base import ConfigInputError, ConfigModel, validate_config_input
from memspine.config.schema.economy import EconomyConfig
from memspine.config.schema.firewall import FirewallConfig
from memspine.config.schema.integrity import IntegrityConfig
from memspine.config.schema.isolation import IsolationConfig
from memspine.config.schema.llm import (
    DecisionConfig,
    EmbeddingConfig,
    GlinerSettings,
    LLMConfig,
    ModelsConfig,
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
from memspine.config.schema.runtime import RuntimeConfig
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
from memspine.prompts.roles import CATALOGUE_ROLE, PROMPT_ROLES
from memspine.schemas.public.plugins import CapabilityManifest
from memspine.store.scope import validate_namespace


class MemoryTypeConfig(ConfigModel):
    """Per-instance enablement + per-type policy overrides (D-14)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policies: dict[str, Any] = Field(default_factory=dict)


class NamespaceConfig(ConfigModel):
    """Per-namespace *policy* overrides only (D-14)."""

    model_config = ConfigDict(extra="forbid")

    policies: dict[str, Any] = Field(default_factory=dict)


class MemspineConfig(ConfigModel):
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
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    write: WriteConfig = Field(default_factory=WriteConfig)
    offline: OfflineConfig = Field(default_factory=OfflineConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    integrity: IntegrityConfig = Field(default_factory=IntegrityConfig)
    firewall: FirewallConfig = Field(default_factory=FirewallConfig)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    runtime: RuntimeConfig
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
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
        if self.runtime.environment == "production" and self.runtime.deployment_id is None:
            raise ConfigError("runtime.environment=production requires runtime.deployment_id")
        return self

    @model_validator(mode="after")
    def _requirements_hold(self) -> MemspineConfig:
        """FU-REQS: a flag that needs another switch raises, naming it, instead of being
        silently inert (``config/requires.py``; the table is also ``FlagSpec.requires``)."""
        check_requires(self)
        return self

    @model_validator(mode="after")
    def _model_profile_bindings_are_valid(self) -> MemspineConfig:
        """Validate profile references and capabilities without loading any provider SDK."""
        gliner_roles = {
            "gliner_decision": "classification",
            "gliner_semantic": "entities",
            "gliner_prepass": "entities",
            # RM-91 (d): opt-in topic tags for topical domains; inert unless bound
            "gliner_topic": "classification",
            # RM-99: opt-in sensitivity detector; active whenever bound, offline only.
            "gliner_sensitivity": "entities",
        }
        llm_roles = set(PROMPT_ROLES) | set(CATALOGUE_ROLE)
        valid_roles = llm_roles | set(gliner_roles)
        tasks_by_role: dict[str, set[str]] = {}
        for profile_name in self.models.profiles:
            if not profile_name.strip() or profile_name != profile_name.strip():
                raise ConfigError(
                    "models.profiles keys must be non-empty names without surrounding whitespace"
                )
        for role, profile_name in self.models.bindings.items():
            if role not in valid_roles:
                raise ConfigError(f"models.bindings has unknown role {role!r}")
            profile = self.models.profiles.get(profile_name)
            if profile is None:
                raise ConfigError(
                    f"models.bindings.{role} references unknown profile {profile_name!r}"
                )
            expected_kind = "gliner" if role in gliner_roles else "llm"
            if profile.kind != expected_kind:
                raise ConfigError(
                    f"models.bindings.{role} requires a {expected_kind} profile; "
                    f"{profile_name!r} is {profile.kind}"
                )
            if role in self.llm.roles:
                raise ConfigError(
                    f"LLM role {role!r} cannot be configured in both llm.roles and models.bindings"
                )
            if profile.kind == "gliner":
                tasks_by_role[role] = set(GlinerSettings.model_validate(profile.settings).tasks)

        for role, policy in self.models.escalation.items():
            if role not in llm_roles:
                raise ConfigError(f"models.escalation has unknown LLM role {role!r}")
            primary_name = self.models.bindings.get(role)
            if primary_name is None:
                raise ConfigError(
                    f"models.escalation.{role} requires a primary LLM profile in models.bindings"
                )
            primary = self.models.profiles[primary_name]
            if primary.kind != "llm":
                raise ConfigError(f"models.escalation.{role} primary profile must be kind llm")
            for target in policy.targets:
                target_profile = self.models.profiles.get(target)
                if target_profile is None:
                    raise ConfigError(
                        f"models.escalation.{role} references unknown target profile {target!r}"
                    )
                if target_profile.kind != "llm":
                    raise ConfigError(
                        f"models.escalation.{role} target {target!r} must be kind llm"
                    )
                if target == primary_name:
                    raise ConfigError(
                        f"models.escalation.{role} target {target!r} is already the primary profile"
                    )

        semantic = self.memories.get("semantic")
        semantic_mode = (
            semantic.policies.get("entity_extraction", "off") if semantic is not None else "off"
        )
        active_consumers = {
            "gliner_decision": self.decision.provider == "gliner2",
            "gliner_semantic": semantic_mode == "gliner",
            "gliner_prepass": self.write.prepass == "gliner",
            "gliner_sensitivity": "gliner_sensitivity" in self.models.bindings,
        }
        sensitivity_profile = self.models.bindings.get("gliner_sensitivity")
        if (
            sensitivity_profile is not None
            and not GlinerSettings.model_validate(
                self.models.profiles[sensitivity_profile].settings
            ).local_files_only
        ):
            raise ConfigError(
                "models.bindings.gliner_sensitivity is offline only: its GLiNER profile "
                "needs local_files_only: true"
            )
        for role, active in active_consumers.items():
            if active:
                tasks = tasks_by_role.get(role)
                required_task = gliner_roles[role]
                if tasks is None or required_task not in tasks:
                    raise ConfigError(
                        f"active GLiNER consumer {role!r} needs a bound GLiNER profile "
                        f"with task {required_task!r}"
                    )
                profile_name = self.models.bindings[role]
                if self.models.profiles[profile_name].provider != "gliner2":
                    raise ConfigError(
                        f"active GLiNER consumer {role!r} requires provider 'gliner2'"
                    )
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


class _FrozenConfigDict(dict[Any, Any]):
    """Dict-compatible immutable container used in detached effective snapshots."""

    def __setitem__(self, key: Any, value: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def __delitem__(self, key: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def clear(self) -> None:
        raise TypeError("effective configuration is immutable")

    def pop(self, key: Any, *default: Any) -> Any:
        raise TypeError("effective configuration is immutable")

    def popitem(self) -> tuple[Any, Any]:
        raise TypeError("effective configuration is immutable")

    def setdefault(self, key: Any, default: Any = None) -> Any:
        raise TypeError("effective configuration is immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def __ior__(self, other: Any) -> _FrozenConfigDict:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")


class _FrozenConfigList(list[Any]):
    """List-compatible immutable container so Pydantic keeps its normal serializers."""

    def __setitem__(self, index: Any, value: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def __delitem__(self, index: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def append(self, value: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def clear(self) -> None:
        raise TypeError("effective configuration is immutable")

    def extend(self, values: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def insert(self, index: SupportsIndex, value: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def pop(self, index: SupportsIndex = -1) -> Any:
        raise TypeError("effective configuration is immutable")

    def remove(self, value: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def reverse(self) -> None:
        raise TypeError("effective configuration is immutable")

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        raise TypeError("effective configuration is immutable")

    def __iadd__(self, values: Iterable[Any]) -> Self:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")

    def __imul__(self, count: SupportsIndex) -> Self:
        raise TypeError("effective configuration is immutable")


class _FrozenFieldsSet(set[str]):
    """Read-only field-presence metadata that Pydantic can copy for model_copy()."""

    def add(self, element: str) -> None:
        raise TypeError("effective configuration is immutable")

    def clear(self) -> None:
        raise TypeError("effective configuration is immutable")

    def difference_update(self, *others: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def discard(self, element: object) -> None:
        raise TypeError("effective configuration is immutable")

    def intersection_update(self, *others: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def pop(self) -> str:
        raise TypeError("effective configuration is immutable")

    def remove(self, element: str) -> None:
        raise TypeError("effective configuration is immutable")

    def symmetric_difference_update(self, other: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def update(self, *others: Any) -> None:
        raise TypeError("effective configuration is immutable")

    def __copy__(self) -> set[str]:
        # Pydantic copies field-set metadata while constructing model_copy() results.
        return set(self)

    def __ior__(self, other: Any) -> Any:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")

    def __iand__(self, other: Any) -> Any:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")

    def __isub__(self, other: Any) -> Any:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")

    def __ixor__(self, other: Any) -> Any:  # type: ignore[misc]
        raise TypeError("effective configuration is immutable")


@cache
def _frozen_config_model(model_type: type[BaseModel]) -> type[BaseModel]:
    config: ConfigDict = ConfigDict(**model_type.model_config)
    config["frozen"] = True
    # Snapshot fields have already been validated. Most nested snapshot types
    # are only read, so defer their redundant validator/serializer construction
    # until a caller explicitly validates or serializes that frozen type.
    config["defer_build"] = True

    def same_config_value(self: BaseModel, other: object) -> bool:
        if not isinstance(other, model_type):
            return False
        return self.model_dump(mode="python") == other.model_dump(mode="python")

    return type(
        f"Frozen{model_type.__name__}",
        (model_type,),
        {
            "model_config": ConfigDict(**config),
            "__module__": model_type.__module__,
            "__eq__": same_config_value,
        },
    )


def _freeze_config_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        frozen_type = _frozen_config_model(type(value))
        fields = {
            name: _freeze_config_value(getattr(value, name))
            for name in value.__class__.model_fields
        }
        frozen = frozen_type.model_construct(
            _fields_set=set(value.model_fields_set),
            **fields,
        )
        # Pydantic's model_copy() calls set.copy(), which returns a mutable built-in
        # set before applying updates; the snapshot itself keeps immutable metadata.
        object.__setattr__(
            frozen, "__pydantic_fields_set__", _FrozenFieldsSet(value.model_fields_set)
        )
        if value.__pydantic_extra__ is not None:
            object.__setattr__(
                frozen,
                "__pydantic_extra__",
                _freeze_config_value(value.__pydantic_extra__),
            )
        return frozen
    if isinstance(value, dict):
        return _FrozenConfigDict({key: _freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, Mapping):
        return _FrozenConfigDict({key: _freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenConfigList(_freeze_config_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_config_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_config_value(item) for item in value)
    return value


def freeze_effective_config(config: MemspineConfig) -> MemspineConfig:
    """Return a detached, recursively immutable snapshot of a validated config.

    Dynamic frozen subclasses preserve the validated model interfaces and
    validators without changing global Pydantic schema metadata. Mutable input
    objects and their field-presence metadata remain untouched.
    """
    validate_config_input(config, MemspineConfig)
    # Callers may edit typed input models before startup. Recheck constraints and
    # cross-field rules, but retain the original explicit-field metadata below.
    try:
        MemspineConfig.model_validate(config.model_dump(mode="python"))
    except ValidationError as exc:
        failures = ", ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['type']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        raise ConfigInputError(f"Invalid effective config ({failures})") from None
    frozen = _freeze_config_value(config)
    assert isinstance(frozen, MemspineConfig)
    return frozen
