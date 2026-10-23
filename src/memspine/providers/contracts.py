"""Shared provider construction contracts.

Provider descriptors are stateless. A builder creates resources for one engine and
registers every owned resource with its lifecycle manager before opening it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

Category = Literal[
    "storage",
    "vector",
    "graph",
    "lexical",
    "cache",
    "documents",
    "llm",
    "embedding",
    "rerank",
    "extraction",
    "decider",
]
Ownership = Literal["owned", "borrowed"]
DependencyKind = Literal["package", "service", "capability"]


@dataclass(frozen=True, slots=True)
class ProviderRef:
    """Stable identity and contract version for a provider implementation."""

    category: Category
    name: str
    contract_version: int = 1

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("provider name must be a non-empty trimmed identifier")
        if self.contract_version < 1:
            raise ValueError("contract_version must be positive")


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Reference to an owned resource or a resource borrowed from another owner."""

    resource_id: str
    ownership: Ownership

    def __post_init__(self) -> None:
        if not self.resource_id or self.resource_id.strip() != self.resource_id:
            raise ValueError("resource_id must be a non-empty trimmed identifier")


@dataclass(frozen=True, slots=True)
class ProjectionPlacement:
    """Filesystem and ownership scope for a rebuildable projection."""

    base_path: Path | None
    in_memory: bool
    owner_id: str

    def __post_init__(self) -> None:
        if not self.owner_id or self.owner_id.strip() != self.owner_id:
            raise ValueError("owner_id must be a non-empty trimmed identifier")


PortT = TypeVar("PortT")


@dataclass(frozen=True, slots=True)
class BuildResult[PortT]:
    """A constructed service and the resources already registered for cleanup."""

    service: PortT
    provider: ProviderRef
    resources: tuple[ResourceRef, ...]
    effective_capabilities: frozenset[str]
    health_probe: Callable[[], Awaitable[bool]] | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class StorageBuildResult[PortT](BuildResult[PortT]):
    """Storage (``storage.records``) result exposing its SQL access capability as a typed
    field; None for a records store without SQL (MongoDB, RM-112), whose service is the whole
    ``RecordsStore`` port and which declares no ``sql`` capability."""

    sql: SqlAccess | None


class LifecycleManager(Protocol):
    """Engine-local owner of provider resources."""

    def own(
        self,
        resource_id: str,
        close: Callable[[], Awaitable[None]],
        *,
        depends_on: tuple[str, ...] = (),
    ) -> ResourceRef: ...

    def borrow(self, resource_id: str) -> ResourceRef: ...

    async def close_all(self) -> object: ...


class ProviderBuilder[SettingsT: BaseModel, ContextT, PortT](Protocol):
    async def build(self, settings: SettingsT, ctx: ContextT) -> BuildResult[PortT]: ...


@dataclass(frozen=True, slots=True)
class DependencyRequirement:
    kind: DependencyKind
    name: str
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise ValueError("dependency name must be a non-empty trimmed identifier")


class SqlAccess(Protocol):
    """SQL capability exposed by storage, without reaching through adapters."""

    @property
    def engine(self) -> AsyncEngine: ...

    @property
    def is_memory(self) -> bool: ...

    @property
    def capabilities(self) -> frozenset[str]: ...

    async def checkpoint(self) -> None: ...

    def sync_lease(self, resources: LifecycleManager, purpose: str) -> SyncSqlLease: ...

    async def acquire_sync(self, resources: LifecycleManager, purpose: str) -> SyncSqlLease: ...


@dataclass(frozen=True, slots=True)
class SyncSqlLease:
    engine: Engine
    resource: ResourceRef
