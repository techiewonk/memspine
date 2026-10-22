"""Weights connector: a lazy, refcounted lease on process-shared model weights (ADR-079 / RM-73,
RM-113).

A connector describes its weights by a key of everything that changes the loaded model (model
id or path, revision, device, dtype/backend, offline policy). Per-call limits, instructions,
batch sizes and result caches are never part of the key and never shared. The factory of an
Engine build passes the process registry (any object with ``acquire_weights``, normally
``providers.shared.ResourceRegistry``), so identical weights load once per process and every
Engine holds its own lease; a connector built without one (scripts, tests) gets a private
registry that never shares. The weights load on first use, :meth:`open` or :meth:`warm`; the
last release drops them (and runs the optional ``close`` hook).

The connector members follow the client standard (docs/CLIENT_ADAPTER_FACTORY_STANDARD.md
section 1): ``kind``, ``scope``, ``identity()``, ``locality()``, ``open()``, ``health()``,
``close()`` and ``handle``. This module imports no memspine layer above ``clients``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

__all__ = ["PrivateWeightRegistry", "SharedWeights", "WeightLease", "WeightRegistry"]


class WeightLease[T](Protocol):
    """One holder's reference to loaded weights."""

    @property
    def value(self) -> T: ...

    async def warm(self) -> None: ...

    async def aclose(self) -> None: ...


class WeightRegistry(Protocol):
    """The sharing seam a factory injects (``providers.shared.ResourceRegistry``)."""

    async def acquire_weights[T](
        self,
        category: str,
        target: Mapping[str, Any],
        settings: Mapping[str, Any] | None,
        factory: Callable[[], Awaitable[T]],
        *,
        close: Callable[[T], Awaitable[None]] | None = None,
        warm: Callable[[T], Awaitable[None]] | None = None,
    ) -> WeightLease[T]: ...


def _digest(category: str, target: Mapping[str, Any], settings: Mapping[str, Any] | None) -> str:
    raw = json.dumps(
        {"category": category, "target": dict(target), "settings": dict(settings or {})},
        sort_keys=True,
        default=repr,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class _PrivateEntry:
    __slots__ = ("close", "refs", "task", "value", "warm_fn", "warm_task")

    def __init__(self) -> None:
        self.refs = 0
        self.task: asyncio.Task[Any] | None = None
        self.value: Any = None
        self.close: Callable[[Any], Awaitable[None]] | None = None
        self.warm_fn: Callable[[Any], Awaitable[None]] | None = None
        self.warm_task: asyncio.Task[None] | None = None


class _PrivateLease[T]:
    __slots__ = ("_entry", "_key", "_registry", "_released")

    def __init__(self, registry: PrivateWeightRegistry, key: str, entry: _PrivateEntry) -> None:
        self._registry = registry
        self._key = key
        self._entry = entry
        self._released = False

    @property
    def value(self) -> T:
        if self._released:
            raise RuntimeError("lease already released")
        value: T = self._entry.value
        return value

    async def warm(self) -> None:
        entry = self._entry
        if entry.warm_fn is None:
            return
        if entry.warm_task is None or (
            entry.warm_task.done()
            and (entry.warm_task.cancelled() or entry.warm_task.exception() is not None)
        ):
            entry.warm_task = asyncio.ensure_future(entry.warm_fn(entry.value))
        await asyncio.shield(entry.warm_task)

    async def aclose(self) -> None:
        if self._released:
            return
        self._released = True
        await self._registry._release(self._key, self._entry)


class PrivateWeightRegistry:
    """A registry for one holder set (no process sharing): load once per key, close on the
    last release, warm once per entry, a failed load is not cached."""

    def __init__(self) -> None:
        self._entries: dict[str, _PrivateEntry] = {}

    async def acquire_weights[T](
        self,
        category: str,
        target: Mapping[str, Any],
        settings: Mapping[str, Any] | None,
        factory: Callable[[], Awaitable[T]],
        *,
        close: Callable[[T], Awaitable[None]] | None = None,
        warm: Callable[[T], Awaitable[None]] | None = None,
    ) -> WeightLease[T]:
        key = _digest(category, target, settings)
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = _PrivateEntry()
            entry.close, entry.warm_fn = close, warm
            entry.task = asyncio.ensure_future(factory())
        entry.refs += 1
        assert entry.task is not None
        try:
            entry.value = await asyncio.shield(entry.task)
        except BaseException:
            entry.refs -= 1
            if self._entries.get(key) is entry and entry.task.done():
                self._entries.pop(key, None)  # never cache a failed load
            raise
        return _PrivateLease(self, key, entry)

    async def _release(self, key: str, entry: _PrivateEntry) -> None:
        entry.refs -= 1
        if entry.refs > 0:
            return
        if self._entries.get(key) is entry:
            self._entries.pop(key)
        if entry.close is not None:
            await entry.close(entry.value)


class SharedWeights[T]:
    """One holder's lazy lease on shared weights: the weights connector adapters receive.
    ``aclose`` / ``close`` is idempotent and final."""

    kind = "weights"
    scope = "thread"

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
        registry: WeightRegistry | None = None,
    ) -> None:
        self._category = category
        self._target = dict(target)
        self._settings = dict(settings or {})
        self._loader = loader
        self._close = close
        self._warm = warm
        # No registry: a private one, so a standalone connector never pins process memory.
        self._registry: WeightRegistry = (
            registry if registry is not None else PrivateWeightRegistry()
        )
        self._lease: WeightLease[T] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    # -- connector protocol ---------------------------------------------------------------

    def identity(self) -> dict[str, Any]:
        """Credential-free key: category, target and the weight-affecting settings."""
        return {"category": self._category, "target": self._target, "settings": self._settings}

    def locality(self) -> str:
        """Weights live in this process."""
        return "process"

    async def open(self) -> None:
        """Load (or lease the already loaded) weights."""
        await self.get()

    async def health(self, deadline_s: float | None = None) -> bool:
        """Loaded and not released; never runs inference."""
        del deadline_s
        return self._lease is not None and not self._closed

    async def close(self) -> None:
        await self.aclose()

    @property
    def handle(self) -> T:
        """The loaded native model (only after :meth:`open`, :meth:`get` or :meth:`warm`)."""
        lease = self._lease
        if lease is None:
            raise RuntimeError(f"{self._category} weights are not loaded")
        return lease.value

    # -- lease ------------------------------------------------------------------------------

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
                    self._lease = await self._registry.acquire_weights(
                        self._category,
                        self._target,
                        self._settings,
                        self._build,
                        close=self._close_value,
                        warm=self._warm_value,
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
