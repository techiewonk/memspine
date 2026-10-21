"""Per-process keyed registry of shared, warmed provider resources (ADR-079 / D-102).

Connected clients and model weights with identical effective settings are built once per
process and shared by every internal consumer and every Engine in the process. Each holder
takes a :class:`Lease`; the last release closes the resource. An Engine registers its
lease release with its own ``ProviderLifecycleManager``, so stopping one Engine never
closes a resource another Engine still holds.

Keys include the event loop for loop-bound async clients (``scope="loop"``), so an async
client is never used from another loop; thread-safe native handles use ``scope="thread"``.
Keys also include the process id, and the registry resets in a forked child: native
handles never cross a fork. Results (caches, per-namespace state) are never registered
here; only resources whose sharing is safe by construction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import itertools
import json
import os
import threading
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from memspine.observability.logging import get_logger

__all__ = [
    "Lease",
    "ResourceKey",
    "ResourceRegistry",
    "SharedResourceStats",
    "resource_key",
    "shared_registry",
]

Scope = Literal["loop", "thread"]
_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResourceKey:
    """Identity of one shareable resource. ``identity`` is a digest, never raw settings."""

    category: str
    identity: str
    scope: str
    pid: int


_LOOP_TOKENS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, int] = (
    weakref.WeakKeyDictionary()
)
_LOOP_COUNTER = itertools.count(1)
_LOOP_TOKENS_LOCK = threading.Lock()


def _loop_token(loop: asyncio.AbstractEventLoop) -> int:
    """A never-reused token per event loop (``id()`` can repeat after a loop is freed)."""
    with _LOOP_TOKENS_LOCK:
        token = _LOOP_TOKENS.get(loop)
        if token is None:
            token = _LOOP_TOKENS[loop] = next(_LOOP_COUNTER)
        return token


def _digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, default=repr, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def resource_key(
    category: str,
    target: Mapping[str, Any],
    settings: Mapping[str, Any] | None = None,
    *,
    scope: Scope,
) -> ResourceKey:
    """Build a key from a credential-free target identity and the effective settings.

    ``scope="loop"`` binds the key to the running event loop (call from async code).
    Secrets must not be passed in; include an auth digest in ``target`` when credentials
    distinguish otherwise identical targets.
    """
    if not category or category.strip() != category:
        raise ValueError("category must be a non-empty trimmed identifier")
    if scope == "loop":
        scope_id = f"loop:{_loop_token(asyncio.get_running_loop())}"
    elif scope == "thread":
        scope_id = "thread"
    else:  # pragma: no cover - Literal guards callers
        raise ValueError(f"unknown scope {scope!r}")
    identity = _digest({"target": dict(target), "settings": dict(settings or {})})
    return ResourceKey(category, identity, scope_id, os.getpid())


@dataclass(frozen=True, slots=True)
class SharedResourceStats:
    key: ResourceKey
    refs: int
    warm: bool
    warm_error: str | None


@dataclass(slots=True)
class _Entry[T]:
    key: ResourceKey
    built: concurrent.futures.Future[T]
    close: Callable[[T], Awaitable[None]] | None
    warm_fn: Callable[[T], Awaitable[None]] | None
    loop: asyncio.AbstractEventLoop | None
    refs: int = 0
    warm_done: concurrent.futures.Future[None] | None = None
    warm_error: str | None = None
    closing: bool = field(default=False)


class Lease[T]:
    """One holder's reference to a shared resource. ``aclose`` is idempotent."""

    __slots__ = ("_entry", "_registry", "_released")

    def __init__(self, registry: ResourceRegistry, entry: _Entry[T]) -> None:
        self._registry = registry
        self._entry = entry
        self._released = False

    @property
    def key(self) -> ResourceKey:
        return self._entry.key

    @property
    def value(self) -> T:
        if self._released:
            raise RuntimeError("lease already released")
        return self._entry.built.result()

    async def warm(self) -> None:
        """Run the resource's warm-up once per shared entry; concurrent callers wait."""
        await self._registry._warm(self._entry)

    async def aclose(self) -> None:
        if self._released:
            return
        self._released = True
        await self._registry._release(self._entry)


class ResourceRegistry:
    """Thread-safe keyed registry; see the module docstring for the sharing contract."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[ResourceKey, _Entry[Any]] = {}

    async def acquire[T](
        self,
        key: ResourceKey,
        factory: Callable[[], Awaitable[T]],
        *,
        close: Callable[[T], Awaitable[None]] | None = None,
        warm: Callable[[T], Awaitable[None]] | None = None,
    ) -> Lease[T]:
        """Return a lease on the resource for ``key``, building it once if needed.

        Concurrent first acquisitions share one build. A failed build is not cached: every
        waiter sees the error and the next acquire builds again.
        """
        if key.pid != os.getpid():
            raise RuntimeError("resource key was created in another process")
        loop = asyncio.get_running_loop() if key.scope.startswith("loop:") else None
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and (
                entry.closing or (entry.loop is not None and entry.loop.is_closed())
            ):
                self._entries.pop(key, None)
                entry = None
            builder = entry is None
            if entry is None:
                entry = _Entry(key, concurrent.futures.Future(), close, warm, loop)
                self._entries[key] = entry
            entry.refs += 1
        if builder:
            try:
                value = await factory()
            except BaseException as exc:
                with self._lock:
                    if self._entries.get(key) is entry:
                        self._entries.pop(key)
                entry.built.set_exception(exc)
                raise
            entry.built.set_result(value)
            _log.debug("provider.shared_resource_built", category=key.category, scope=key.scope)
        else:
            try:
                await asyncio.shield(asyncio.wrap_future(entry.built))
            except BaseException:
                # Cancelled waiter or failed build: drop this reference; if it was the
                # last one on a built resource, _release closes it (no leak).
                await asyncio.shield(self._release(entry))
                raise
        return Lease(self, entry)

    async def _warm(self, entry: _Entry[Any]) -> None:
        if entry.warm_fn is None:
            return
        with self._lock:
            runner = entry.warm_done is None
            if runner:
                entry.warm_done = concurrent.futures.Future()
            done = entry.warm_done
        assert done is not None
        if runner:
            try:
                await entry.warm_fn(entry.built.result())
            except Exception as exc:
                entry.warm_error = type(exc).__name__
                with self._lock:
                    entry.warm_done = None  # a later warm() may retry
                done.set_exception(exc)
                raise
            done.set_result(None)
        else:
            await asyncio.shield(asyncio.wrap_future(done))

    async def _release(self, entry: _Entry[Any]) -> None:
        with self._lock:
            entry.refs -= 1
            last = entry.refs <= 0
            if last:
                entry.closing = True
                if self._entries.get(entry.key) is entry:
                    self._entries.pop(entry.key)
        if (
            last
            and entry.close is not None
            and entry.built.done()
            and entry.built.exception() is None
        ):
            await entry.close(entry.built.result())

    def stats(self) -> tuple[SharedResourceStats, ...]:
        """Snapshot for readiness: references and warm state per shared resource."""
        with self._lock:
            entries = list(self._entries.values())
        return tuple(
            SharedResourceStats(
                e.key,
                e.refs,
                e.warm_fn is None
                or (
                    e.warm_done is not None
                    and e.warm_done.done()
                    and e.warm_done.exception() is None
                ),
                e.warm_error,
            )
            for e in entries
        )

    def reset_after_fork(self) -> None:
        """Forget every entry without closing it: inherited native handles are invalid
        in a forked child and must not be closed or reused there."""
        self._lock = threading.Lock()
        self._entries = {}


_REGISTRY = ResourceRegistry()


def shared_registry() -> ResourceRegistry:
    """The process-wide registry (one per process; reset in a forked child)."""
    return _REGISTRY


if hasattr(os, "register_at_fork"):  # POSIX only; Windows spawns fresh processes
    os.register_at_fork(after_in_child=_REGISTRY.reset_after_fork)
