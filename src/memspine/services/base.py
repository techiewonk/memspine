"""ServiceAdapter ABC + CapabilityManifest (D-22/M14).

A service is a capability port (storage, vector, graph, llm, …). Its manifest
declares what it provides and which extra installs it, so a missing service can
hard-fail with an actionable message (D-10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = ["CapabilityManifest", "ServiceAdapter"]


@dataclass(frozen=True)
class CapabilityManifest:
    """Declares a service implementation to the registry (D-15)."""

    name: str
    capability: str
    extra: str | None = None
    provides: tuple[str, ...] = field(default_factory=tuple)


class ServiceAdapter(ABC):
    """Lifecycle contract every service implements.

    Connections are injected by the lifecycle manager via a ``clients/`` client
    (D-24); ``start``/``stop`` prepare and release capability state only.
    """

    @property
    @abstractmethod
    def manifest(self) -> CapabilityManifest: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def health(self) -> bool:
        return True
