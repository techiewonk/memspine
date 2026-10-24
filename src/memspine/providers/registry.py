"""The single registry owner for built-in and pinned provider descriptors."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, ItemsView, Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel, JsonValue, ValidationError

from memspine.exceptions import ConfigError
from memspine.providers.contracts import (
    Category,
    DependencyRequirement,
    ProjectionPlacement,
    ProviderBuilder,
    ProviderRef,
)
from memspine.services.base import CapabilityManifest as ServiceManifest

__all__ = [
    "CACHE_BACKENDS",
    "DATA_CATEGORIES",
    "DECIDERS",
    "DOCUMENT_STORES",
    "EMBEDDERS",
    "EXTRACTORS",
    "GRAPH_STORES",
    "KINDS",
    "LEXICAL_INDEXES",
    "LLM_PROVIDERS",
    "LOCALITIES",
    "RERANKERS",
    "STORAGE_BACKENDS",
    "VECTOR_STORES",
    "Locality",
    "LocalityResolver",
    "ProviderDescriptor",
    "ProviderRegistryError",
    "Registry",
    "get_registry",
    "placed_locality",
    "provider_kinds",
    "provider_projection_settings",
    "provider_target_identity",
    "records_locality",
]

# Plugin extensions which are not providers continue to use this same registry owner.
KINDS: dict[str, str] = {
    "storage_backends": "memspine.providers.registry",
    "vector_stores": "memspine.providers.registry",
    "graph_stores": "memspine.providers.registry",
    "lexical_indexes": "memspine.providers.registry",
    "cache_backends": "memspine.providers.registry",
    "document_stores": "memspine.providers.registry",
    "llm_providers": "memspine.providers.registry",
    "embedders": "memspine.providers.registry",
    "rerankers": "memspine.providers.registry",
    "extractors": "memspine.providers.registry",
    "deciders": "memspine.providers.registry",
    "read_legs": "memspine.read.registry",
    "derive_stages": "memspine.derive.registry",
    "memory_types": "memspine.memory.registry",
    "prompt_sets": "memspine.prompts.registry",
}

KIND_TO_CATEGORY: dict[str, Category] = {
    "storage_backends": "storage",
    "vector_stores": "vector",
    "graph_stores": "graph",
    "lexical_indexes": "lexical",
    "cache_backends": "cache",
    "document_stores": "documents",  # ADR-081: storage.documents
    "llm_providers": "llm",
    "embedders": "embedding",
    # RM-113 (owner naming 2026-10-23): the model slots are model.embedding, model.llm,
    # model.rerank, model.extraction and model.decider.
    "rerankers": "rerank",
    "extractors": "extraction",
    "deciders": "decider",
}
_CATEGORY_TO_KIND = {category: kind for kind, category in KIND_TO_CATEGORY.items()}

#: ADR-081: where a data provider's state lives. ``process`` = inside this process (memory,
#: ``:memory:``); ``host`` = files on this machine (sqlite, lancedb, tantivy, ladybug, disk
#: cache, filesystem documents); ``cluster`` = a network service every node can reach
#: (postgres, pgvector, opensearch, weaviate, neo4j, redis, valkey). The
#: ``deployment.locality`` rules that use it live in :mod:`memspine.providers.locality`.
Locality = Literal["process", "host", "cluster"]
LOCALITIES: tuple[Locality, ...] = ("process", "host", "cluster")
#: Categories whose providers hold data and therefore declare a locality.
DATA_CATEGORIES: frozenset[Category] = frozenset(
    {"storage", "vector", "graph", "lexical", "cache", "documents"}
)
#: ``(settings, records slot locality) -> effective locality`` (ADR-081 WP6). The records
#: slot itself is resolved with its own locality as the second argument.
LocalityResolver = Callable[[BaseModel, Locality], Locality]


def placed_locality(_settings: BaseModel, records: Locality) -> Locality:
    """A host-local store placed beside the records store (LanceDB, Tantivy, LadybugDB): it
    lives in RAM when the records store does (``:memory:``), else in files on this host."""
    return "process" if records == "process" else "host"


def records_locality(_settings: BaseModel, records: Locality) -> Locality:
    """A store inside the records database (``sql_adjacency``): the records slot's locality."""
    return records


class ProviderRegistryError(ConfigError):
    """Provider registration or resolution failed."""


class UnknownProviderError(ProviderRegistryError):
    """No provider is registered under the requested kind and name."""


class Registry[T]:
    """Name map whose instance is unique for each extension kind."""

    def __new__(cls, kind: str) -> Registry[Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown registry kind {kind!r} (known: {sorted(KINDS)})")
        current = _REGISTRIES.get(kind)
        if current is not None:
            return current
        instance = super().__new__(cls)
        _REGISTRIES[kind] = instance
        return instance

    def __init__(self, kind: str) -> None:
        if hasattr(self, "_items"):
            return
        self.kind = kind
        self._items: dict[str, T] = {}
        self._builtin: set[str] = set()
        self._lifetime: dict[str, str] = {}

    def register(self, name: str, provider: T, *, builtin: bool = True, replace: bool = False) -> T:
        if not name or name.strip() != name:
            raise ValueError("provider name must be a non-empty trimmed identifier")
        category = KIND_TO_CATEGORY.get(self.kind)
        if category is not None:
            if not isinstance(provider, ProviderDescriptor):
                raise ProviderRegistryError(
                    f"{self.kind}: providers must register a typed ProviderDescriptor"
                )
            if provider.ref.contract_version != 1:
                raise ProviderRegistryError(
                    "provider contract version is unsupported; expected version 1"
                )
            if provider.ref.category != category or provider.ref.name != name:
                raise ProviderRegistryError(
                    f"{self.kind}: descriptor identity must be {category}:{name}"
                )
        if name in self._items and not replace:
            raise ProviderRegistryError(f"{self.kind}: {name!r} is already registered")
        self._items[name] = provider
        if builtin:
            self._builtin.add(name)
        else:
            self._builtin.discard(name)
        return provider

    def unregister(self, name: str) -> None:
        self._items.pop(name, None)
        self._builtin.discard(name)
        self._lifetime.pop(name, None)

    def get(self, name: str, *default: Any) -> T:
        try:
            return self._items[name]
        except KeyError:
            if default:
                return cast(T, default[0])
            raise UnknownProviderError(
                f"unknown {self.kind} provider {name!r} (valid: {', '.join(self._items)})"
            ) from None

    def find(self, name: str) -> T | None:
        return self._items.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def builtin_names(self) -> tuple[str, ...]:
        return tuple(name for name in self._items if name in self._builtin)

    def is_builtin(self, name: str) -> bool:
        return name in self._builtin

    def lifetime_of(self, name: str) -> str:
        return self._lifetime.get(name, "app")

    def __getitem__(self, name: str) -> T:
        return self._items[name]

    def __setitem__(self, name: str, provider: T) -> None:
        self.register(name, provider, builtin=False, replace=True)

    def __delitem__(self, name: str) -> None:
        if name not in self._items:
            raise KeyError(name)
        self.unregister(name)

    def pop(self, name: str, *default: T | None) -> T | None:
        if name in self._items:
            value = self._items[name]
            self.unregister(name)
            return value
        if default:
            return default[0]
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> ItemsView[str, T]:
        return self._items.items()


_REGISTRIES: dict[str, Registry[Any]] = {}
_BUILTIN_MODULES: dict[str, tuple[str, ...]] = {
    "llm_providers": ("memspine.providers.builtin.models",),
    "embedders": ("memspine.providers.builtin.models",),
    "rerankers": ("memspine.providers.builtin.rerank",),
    "extractors": ("memspine.providers.builtin.extraction",),
    "deciders": ("memspine.providers.builtin.decider",),
    "storage_backends": (
        "memspine.providers.builtin.storage",
        "memspine.providers.builtin.mongodb",
    ),
    "vector_stores": (
        "memspine.providers.builtin.vector",
        "memspine.providers.builtin.pgvector",
        "memspine.providers.builtin.weaviate",
    ),
    "graph_stores": ("memspine.providers.builtin.graph",),
    "lexical_indexes": (
        "memspine.providers.builtin.lexical",
        "memspine.providers.builtin.opensearch",
        "memspine.providers.builtin.weaviate",
    ),
    "cache_backends": ("memspine.providers.builtin.cache",),
    "document_stores": (
        "memspine.providers.builtin.documents",
        "memspine.providers.builtin.mongodb",
    ),
}
_BUILTINS_LOADED: set[str] = set()
_BUILTINS_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Stateless provider metadata and typed engine-local builder."""

    ref: ProviderRef
    settings_model: type[BaseModel]
    declared_capabilities: frozenset[str]
    requirements: tuple[DependencyRequirement, ...]
    builder: ProviderBuilder[Any, Any, Any]
    service_manifest: ServiceManifest | None = None
    #: ``(settings, owner_id, deployment.data_dir) -> placement`` (ADR-081).
    placement: Callable[[BaseModel, str, str | None], ProjectionPlacement] | None = None
    requires_deployment_id: bool = False
    target_identity: Callable[[BaseModel], Mapping[str, JsonValue]] | None = field(
        default=None, kw_only=True
    )
    projection_settings: Callable[[BaseModel], Mapping[str, JsonValue]] | None = field(
        default=None, kw_only=True
    )
    #: PA-046: modules that are side-effect free to import before build(). When this
    #: provider is selected, startup imports them on a background thread so a slow
    #: third-party import overlaps config/storage work instead of blocking build().
    prewarm_modules: tuple[str, ...] = field(default=(), kw_only=True)
    #: ADR-081 / RM-111 WP2: ``process`` | ``host`` | ``cluster``. Every built-in data
    #: provider declares it; model providers (llm, embedding, reranker) leave it None.
    locality: Locality | None = field(default=None, kw_only=True)
    #: ADR-081 WP6: the effective locality from the settings and the records slot's locality
    #: (``:memory:`` SQLite is ``process``; ``sql_adjacency`` follows the records slot). None =
    #: the static ``locality``. Pure: never opens anything.
    resolve_locality: LocalityResolver | None = field(default=None, kw_only=True)
    #: ADR-081 WP6: one writer process per host path (Tantivy, LadybugDB, filesystem
    #: documents). Under ``deployment.locality: multi_process`` only the process with
    #: ``deployment.writer: true`` may open it.
    single_writer: bool = field(default=False, kw_only=True)
    #: ADR-081 WP6: the provider builds its files from the records placement base
    #: (``<base>.lance`` etc.), so it needs ``deployment.data_dir`` when the records store is not
    #: a file. A callable decides from the settings (an explicit path needs no base).
    needs_placement: bool | Callable[[BaseModel], bool] = field(default=False, kw_only=True)
    #: ADR-083: ``(settings, deployment.locality) -> fix text`` when these settings make the
    #: provider run a host-local server of its own (a managed ``mongod``), else None. A non-None
    #: result is printed by the ``multi_node`` rejection and makes the slot writer-only under
    #: ``multi_process``. Pure: never opens anything.
    host_local_fix: Callable[[BaseModel, str], str | None] | None = field(
        default=None, kw_only=True
    )

    def __post_init__(self) -> None:
        expected_kind = _CATEGORY_TO_KIND.get(self.ref.category)
        if expected_kind is None:
            raise ValueError(f"unsupported provider category {self.ref.category!r}")
        if not isinstance(self.settings_model, type) or not issubclass(
            self.settings_model, BaseModel
        ):
            raise TypeError("settings_model must be a Pydantic BaseModel type")
        if not callable(getattr(self.builder, "build", None)):
            raise TypeError("provider builder must expose async build(settings, ctx)")
        if self.service_manifest is not None and self.service_manifest.name != self.ref.name:
            raise ValueError("service manifest name must match provider name")
        if not isinstance(self.requires_deployment_id, bool):
            raise TypeError("requires_deployment_id must be a bool")
        if self.target_identity is not None and not callable(self.target_identity):
            raise TypeError("target_identity must be callable")
        if self.projection_settings is not None and not callable(self.projection_settings):
            raise TypeError("projection_settings must be callable")
        if not isinstance(self.prewarm_modules, tuple) or not all(
            isinstance(name, str) and name and name.strip() == name for name in self.prewarm_modules
        ):
            raise TypeError("prewarm_modules must be a tuple of non-empty module names")
        if self.locality is not None and self.locality not in LOCALITIES:
            raise ValueError(f"locality must be one of {', '.join(LOCALITIES)}")
        if self.locality is not None and self.ref.category not in DATA_CATEGORIES:
            raise ValueError("locality is declared only by data-store providers")
        if self.resolve_locality is not None and not callable(self.resolve_locality):
            raise TypeError("resolve_locality must be callable")
        if not isinstance(self.single_writer, bool):
            raise TypeError("single_writer must be a bool")
        if self.host_local_fix is not None and not callable(self.host_local_fix):
            raise TypeError("host_local_fix must be callable")

    def effective_locality(self, settings: BaseModel, records: Locality) -> Locality | None:
        """This provider's locality for ``settings`` (ADR-081 WP6); None = undeclared."""
        if self.resolve_locality is not None:
            return self.resolve_locality(settings, records)
        return self.locality

    def placement_needed(self, settings: BaseModel) -> bool:
        """Whether this provider builds its files from the records placement base."""
        if callable(self.needs_placement):
            return bool(self.needs_placement(settings))
        return bool(self.needs_placement)


def provider_target_identity(
    descriptor: ProviderDescriptor, settings: BaseModel
) -> Mapping[str, JsonValue] | None:
    """Resolve and freeze safe target identity metadata for one provider instance.

    This callback is separate from the provider adapter so projection ownership can
    fingerprint the resolved endpoint without coupling generic runtime code to a
    provider vendor or reading secrets from settings itself.
    """
    if descriptor.target_identity is None:
        return None
    import json

    raw = descriptor.target_identity(settings)
    # Round-trip through JSON to copy caller-owned structures and reject secret-like
    # non-JSON values or non-finite floats before they enter a target fingerprint.
    normalized = json.loads(json.dumps(dict(raw), sort_keys=True, allow_nan=False))
    if not isinstance(normalized, dict) or not all(isinstance(key, str) for key in normalized):
        raise ProviderRegistryError("provider target identity must be a JSON object")
    return MappingProxyType(normalized)


def provider_projection_settings(
    descriptor: ProviderDescriptor, settings: BaseModel
) -> Mapping[str, JsonValue] | None:
    """Return descriptor-defined target settings, excluding operational flags."""
    if descriptor.projection_settings is None:
        return None
    import json

    raw = descriptor.projection_settings(settings)
    normalized = json.loads(json.dumps(dict(raw), sort_keys=True, allow_nan=False))
    if not isinstance(normalized, dict) or not all(isinstance(key, str) for key in normalized):
        raise ProviderRegistryError("provider projection settings must be a JSON object")
    return MappingProxyType(normalized)


def get_registry(kind: str) -> Registry[Any]:
    """Return the unique registry for a plugin kind."""
    if kind not in KINDS:
        raise ProviderRegistryError(f"unknown plug-in kind {kind!r} (known: {sorted(KINDS)})")
    with _BUILTINS_LOCK:
        for module in _BUILTIN_MODULES.get(kind, ()):
            if module not in _BUILTINS_LOADED and importlib.util.find_spec(module) is not None:
                builtin_module = importlib.import_module(module)
                register = getattr(builtin_module, "register", None)
                if callable(register):
                    register()
                _BUILTINS_LOADED.add(module)
    return Registry(kind)


def provider_kinds() -> tuple[str, ...]:
    return tuple(KIND_TO_CATEGORY)


STORAGE_BACKENDS: Registry[ProviderDescriptor] = Registry("storage_backends")
VECTOR_STORES: Registry[ProviderDescriptor] = Registry("vector_stores")
GRAPH_STORES: Registry[ProviderDescriptor] = Registry("graph_stores")
LEXICAL_INDEXES: Registry[ProviderDescriptor] = Registry("lexical_indexes")
CACHE_BACKENDS: Registry[ProviderDescriptor] = Registry("cache_backends")
DOCUMENT_STORES: Registry[ProviderDescriptor] = Registry("document_stores")
LLM_PROVIDERS: Registry[ProviderDescriptor] = Registry("llm_providers")
EMBEDDERS: Registry[ProviderDescriptor] = Registry("embedders")
RERANKERS: Registry[ProviderDescriptor] = Registry("rerankers")
EXTRACTORS: Registry[ProviderDescriptor] = Registry("extractors")
DECIDERS: Registry[ProviderDescriptor] = Registry("deciders")


def validate_settings(descriptor: ProviderDescriptor, settings: Mapping[str, Any]) -> BaseModel:
    """Validate one JSON settings object without echoing possibly secret values."""
    if not isinstance(settings, Mapping):
        raise ProviderRegistryError("provider settings must be an object")
    try:
        return descriptor.settings_model.model_validate(dict(settings))
    except ValidationError as exc:
        paths = sorted(
            {".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()}
        )
        raise ProviderRegistryError(
            f"invalid settings for {descriptor.ref.category}.{descriptor.ref.name}: "
            f"check fields {', '.join(paths)}"
        ) from None


def requirements_available(
    descriptor: ProviderDescriptor,
    *,
    available_services: frozenset[str] = frozenset(),
    available_capabilities: frozenset[str] = frozenset(),
) -> tuple[bool, tuple[str, ...]]:
    """Check package requirements only; never imports a driver or opens a connection."""
    missing: list[str] = []
    for requirement in descriptor.requirements:
        satisfied = (
            importlib.util.find_spec(requirement.name) is not None
            if requirement.kind == "package"
            else requirement.name
            in (available_services if requirement.kind == "service" else available_capabilities)
        )
        if not satisfied and not requirement.optional:
            missing.append(requirement.name)
    return not missing, tuple(missing)
