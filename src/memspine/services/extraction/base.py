"""Extraction port (RM-113, slot ``model.extraction``): leased access to span-extraction models.

Consumers (semantic entity extraction, the write prepass, topic and sensitivity taggers, the
GLiNER decider) depend on this port, never on the concrete pool. One Engine builds one
extractor pool through the ``extraction`` descriptor; each consumer takes a lazy
:meth:`ExtractionModels.binding` for its own settings, and :meth:`call` runs one complete
synchronous SDK operation off the event loop under the binding's concurrency bound.
Extraction results are never shared; the weights are (ADR-079).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

__all__ = ["ExtractionBinding", "ExtractionLease", "ExtractionModels", "ExtractionSettings"]

T = TypeVar("T")


class ExtractionSettings(Protocol):
    """The structural subset of a GLiNER slot's settings a consumer binds with."""

    @property
    def model(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def device(self) -> str: ...

    @property
    def local_files_only(self) -> bool: ...

    @property
    def tasks(self) -> tuple[str, ...]: ...

    @property
    def inference_concurrency(self) -> int: ...


class ExtractionBinding(Protocol):
    """A consumer's Engine-lifetime lease, acquired on first call."""

    async def call(self, callback: Callable[[Any], T]) -> T: ...


class ExtractionLease(ExtractionBinding, Protocol):
    """An explicit lease; ``aclose`` releases it (the pool may keep the weights resident)."""

    async def aclose(self) -> None: ...


@runtime_checkable
class ExtractionModels(Protocol):
    """The engine-owned extraction resource."""

    def binding(self, settings: Any) -> ExtractionBinding: ...

    async def acquire(self, settings: Any) -> ExtractionLease: ...

    async def aclose(self) -> None: ...
