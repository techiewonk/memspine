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
from memspine.config.schema.llm import PromptsConfig, WorkersConfig
from memspine.config.schema.model import (
    OFFLINE_ONLY,
    REQUIRED_TASK,
    GlinerSettings,
    ModelConfig,
    ModelSlot,
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
    DeploymentConfig,
    EventLogConfig,
    GraphConfig,
    StorageConfig,
)
from memspine.config.schema.write import DataShapeConfig, IngestConfig, WriteConfig
from memspine.core.registry import validate_types
from memspine.exceptions import ConfigError
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
    #: ADR-081: deployment identity, locality and the host-local data directory.
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    #: ADR-081: one slot per data category (records, vector, lexical, graph, cache,
    #: documents). Model providers live in ``model``.
    storage: StorageConfig = Field(default_factory=StorageConfig)
    #: ADR-082: one slot per model category (embedding, llm.<role>, rerank,
    #: extraction.<consumer>, decider.<consumer>, vision) plus device placement.
    model: ModelConfig = Field(default_factory=ModelConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    write: WriteConfig = Field(default_factory=WriteConfig)
    offline: OfflineConfig = Field(default_factory=OfflineConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
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

    @model_validator(mode="before")
    @classmethod
    def _required_slots(cls, value: Any) -> Any:
        """ADR-081: ``vector`` and ``cache`` moved under ``storage``; ADR-082: ``embedding``,
        ``llm``, ``models`` and ``decision`` moved under ``model``. No aliases (ADR-068)."""
        if isinstance(value, Mapping) and isinstance(value.get("model"), Mapping):
            ModelConfig.reject_null(value["model"])
        if isinstance(value, Mapping) and isinstance(value.get("storage"), Mapping):
            StorageConfig.reject_null(value["storage"])
        return value

    @model_validator(mode="after")
    def _asset_keys_agree(self) -> MemspineConfig:
        """E04: ``read.asset_evidence`` needs the identities ``ingest.assets`` registers, and
        ``fetch`` needs ``storage.documents`` to keep the downloaded bytes by content hash
        (RM-113 item 18)."""
        if self.read.asset_evidence != "off" and self.ingest.assets != "on":
            raise ConfigError("read.asset_evidence needs ingest.assets: on")
        if self.read.asset_evidence == "fetch" and self.storage.documents is None:
            raise ConfigError(
                "read.asset_evidence: fetch needs storage.documents (the downloaded bytes are "
                "kept there by content hash)"
            )
        if self.runtime.environment == "production" and self.deployment.id is None:
            raise ConfigError("runtime.environment=production requires deployment.id")
        return self

    @model_validator(mode="after")
    def _requirements_hold(self) -> MemspineConfig:
        """FU-REQS: a flag that needs another switch raises, naming it, instead of being
        silently inert (``config/requires.py``; the table is also ``FlagSpec.requires``)."""
        check_requires(self)
        return self

    @model_validator(mode="after")
    def _model_slots_agree(self) -> MemspineConfig:
        """ADR-082 / plan section 2: each consumer's slot exposes the task it needs, the
        offline-only consumers load local files only, and an active consumer has its slot."""
        extraction = self.model.extraction
        decider = self.model.decider
        slots: dict[str, tuple[str, ModelSlot | None]] = {
            "semantic": ("model.extraction.semantic", extraction.semantic),
            "prepass": ("model.extraction.prepass", extraction.prepass),
            "topic": ("model.extraction.topic", extraction.topic),
            "sensitivity": ("model.extraction.sensitivity", extraction.sensitivity),
            "route": ("model.decider.route", decider.route),
            "read": ("model.decider.read", decider.read),
        }
        for consumer, (path, slot) in slots.items():
            if slot is None:
                continue
            if slot.provider == "gliner2":
                gliner = GlinerSettings.model_validate(slot.settings)
                task = REQUIRED_TASK[consumer]
                if task not in gliner.tasks:
                    raise ConfigError(f"{path} needs a gliner2 model with task {task!r}")
                local_only = gliner.local_files_only
            else:
                local_only = slot.settings.get("local_files_only") is True
            if consumer in OFFLINE_ONLY and not local_only:
                raise ConfigError(
                    f"{path} is offline only: it needs settings.local_files_only: true"
                )

        semantic = self.memories.get("semantic")
        semantic_mode = (
            semantic.policies.get("entity_extraction", "off") if semantic is not None else "off"
        )
        active = {
            "semantic": (
                semantic_mode == "gliner",
                "memories.semantic.policies.entity_extraction: gliner",
            ),
            "prepass": (self.write.prepass == "gliner", "write.prepass: gliner"),
        }
        for consumer, (on, switch) in active.items():
            path, slot = slots[consumer]
            if not on:
                continue
            if slot is None:
                raise ConfigError(f"{switch} needs {path} (a gliner2 slot)")
            if slot.provider != "gliner2":
                raise ConfigError(f"{switch} needs {path}.provider: gliner2")
        if self.read.planner == "decision" and decider.route is None:
            raise ConfigError("read.planner: decision needs model.decider.route")
        if self.read.sensitivity_gate == "decider" and decider.read is None:
            raise ConfigError("read.sensitivity_gate: decider needs model.decider.read")
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
