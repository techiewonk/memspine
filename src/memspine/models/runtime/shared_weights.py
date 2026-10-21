"""Lazy, refcounted leases on process-shared model weights (ADR-079 / RM-73).

An adapter describes its weights by a key of everything that changes the loaded model (model
id or path, revision, device, dtype/backend, offline policy). Per-call limits, instructions,
batch sizes and result caches are never part of the key and never shared. Adapters built by
an Engine pass the process registry, so identical weights load once per process and every
Engine holds its own lease; directly constructed adapters (scripts, tests) get a private
registry and behave as before. The weights load on first use or on :meth:`warm`; the last
release drops them (and runs the optional ``close`` hook).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from memspine.providers.shared import Lease, ResourceRegistry, resource_key

__all__ = ["SharedWeights"]


class SharedWeights[T]:
    """One holder's lazy lease on shared weights. ``aclose`` is idempotent and final."""

    __slots__ = (
        "_category",
        "_close",
        "_closed",
        "_lease",
        "_loader",
        "_lock",
        "_registry",
        "_settings",
        "_target",
        "_warm",
    )

    def __init__(
        self,
        category: str,
        target: Mapping[str, Any],
        settings: Mapping[str, Any] | None,
        loader: Callable[[], T],
        *,
        close: Callable[[T], object] | None = None,
        warm: Callable[[T], object] | None = None,
        registry: ResourceRegistry | None = None,
    ) -> None:
        self._category = category
        self._target = dict(target)
        self._settings = dict(settings or {})
        self._loader = loader
        self._close = close
        self._warm = warm
        # No registry: a private one, so a standalone adapter never pins process memory.
        self._registry = registry if registry is not None else ResourceRegistry()
        self._lease: Lease[T] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def loaded(self) -> bool:
        return self._lease is not None

    async def get(self) -> T:
        """The loaded weights, acquiring (and loading once per key) on first use."""
        lease = self._lease
        if lease is None:
            async with self._lock:
                if self._closed:
                    raise RuntimeError(f"{self._category} weights were released")
                if self._lease is None:
                    # The key is built here, not at construction: it carries the process id.
                    key = resource_key(self._category, self._target, self._settings, scope="thread")
                    self._lease = await self._registry.acquire(
                        key,
                        self._build,
                        close=self._close_value,
                        warm=self._warm_value,
                        kind="weights",  # RM-78: may stay loaded at zero holders (keep_warm)
                    )
                lease = self._lease
        return lease.value

    async def warm(self) -> None:
        """Load the weights and run the warm hook once per shared entry (RM-74 calls this)."""
        await self.get()
        lease = self._lease
        if lease is not None:
            await lease.warm()

    async def aclose(self) -> None:
        """Release this holder's lease; the weights free when no holder remains."""
        async with self._lock:
            self._closed = True
            lease, self._lease = self._lease, None
        if lease is not None:
            await lease.aclose()

    async def _build(self) -> T:
        return await asyncio.to_thread(self._loader)

    async def _close_value(self, value: T) -> None:
        if self._close is not None:
            await asyncio.to_thread(self._close, value)

    async def _warm_value(self, value: T) -> None:
        if self._warm is not None:
            await asyncio.to_thread(self._warm, value)
