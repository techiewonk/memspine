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

Retention (RM-78): an entry acquired with ``kind="weights"`` (thread-scoped model weights) may
outlive its last holder. While any registered :class:`RetentionPolicy` has ``keep_warm`` the
entry stays loaded at zero references, until ``idle_ttl_s`` expires, ``memory_budget_mb`` or
``max_models_per_category`` evicts it (least recently used idle entry first, never one in
use), or :meth:`ResourceRegistry.evict_idle` drains it. Connections always close on their last
release.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import itertools
import json
import os
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from memspine.observability.logging import get_logger

if TYPE_CHECKING:
    from memspine.providers.contracts import LifecycleManager, ResourceRef

__all__ = [
    "Lease",
    "ResourceKey",
    "ResourceRegistry",
    "RetentionPolicy",
    "SharedResourceStats",
    "current_rss_mb",
    "own_shared",
    "path_identity",
    "resource_key",
    "shared_registry",
]

Scope = Literal["loop", "thread"]
Kind = Literal["connection", "weights"]
_log = get_logger(__name__)


class _BuildCancelled(Exception):
    """The holder that was building (or warming) an entry was cancelled; waiters retry."""


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


def path_identity(path: str | os.PathLike[str]) -> str:
    """Canonical identity of a local file or directory path (absolute, case-folded where
    the platform folds case), so two spellings of one path share one resource."""
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def current_rss_mb() -> float | None:
    """Resident set size of this process in MiB, or None where it cannot be read cheaply."""
    try:
        import psutil  # optional; present with most model extras

        return float(psutil.Process().memory_info().rss) / (1024 * 1024)
    except Exception:
        pass
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        page_size = float(getattr(os, "sysconf")("SC_PAGE_SIZE"))  # POSIX only  # noqa: B009
        return pages * page_size / (1024 * 1024)
    except Exception:
        return None


def _loosest[N: (int, float)](values: list[N | None]) -> N | None:
    """An unset limit wins; otherwise the largest."""
    present = [v for v in values if v is not None]
    return None if len(present) != len(values) or not present else max(present)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long released model weights stay loaded (``runtime.shared_resources``, RM-78)."""

    keep_warm: bool = False
    idle_ttl_s: float | None = None
    memory_budget_mb: int | None = None
    max_models_per_category: int | None = None

    @staticmethod
    def merge(policies: list[RetentionPolicy]) -> RetentionPolicy:
        """The most retaining combination, so Engines sharing a registry never shorten each
        other's retention. Only ``keep_warm`` policies contribute limits."""
        keeping = [p for p in policies if p.keep_warm]
        if not keeping:
            return RetentionPolicy()
        return RetentionPolicy(
            True,
            _loosest([p.idle_ttl_s for p in keeping]),
            _loosest([p.memory_budget_mb for p in keeping]),
            _loosest([p.max_models_per_category for p in keeping]),
        )


@dataclass(frozen=True, slots=True)
class SharedResourceStats:
    key: ResourceKey
    refs: int
    warm: bool
    warm_error: str | None
    kind: Kind = "connection"
    #: retained model weights with no holder (``keep_warm``)
    idle: bool = False
    #: measured RSS growth of the load (an estimate; None when unknown)
    size_mb: float | None = None


@dataclass(slots=True)
class _Entry[T]:
    key: ResourceKey
    built: concurrent.futures.Future[T]
    close: Callable[[T], Awaitable[None]] | None
    warm_fn: Callable[[T], Awaitable[None]] | None
    loop: asyncio.AbstractEventLoop | None
    kind: Kind = "connection"
    refs: int = 0
    warm_done: concurrent.futures.Future[None] | None = None
    warm_error: str | None = None
    closing: bool = field(default=False)
    last_used: float = 0.0
    size_mb: float | None = None

    def usable(self) -> bool:
        return self.built.done() and self.built.exception() is None


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
    """Thread-safe keyed registry; see the module docstring for the sharing contract.

    ``clock`` (monotonic seconds) and ``rss_mb`` are injectable for deterministic tests."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        rss_mb: Callable[[], float | None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[ResourceKey, _Entry[Any]] = {}
        self._clock = clock or time.monotonic
        self._rss_mb = rss_mb or current_rss_mb
        self._policies: dict[int, RetentionPolicy] = {}
        self._policy_ids = itertools.count(1)
        self._sweeps: set[asyncio.Task[int]] = set()

    # -- retention policy ---------------------------------------------------------------

    def add_retention(self, policy: RetentionPolicy) -> int:
        """Register one holder's retention policy; the token undoes it."""
        with self._lock:
            token = next(self._policy_ids)
            self._policies[token] = policy
            return token

    def remove_retention(self, token: int) -> None:
        """Drop a policy (idempotent). Weights already retained stay until evicted: with
        ``keep_warm`` they live for the process (ADR-079 addendum)."""
        with self._lock:
            self._policies.pop(token, None)

    @property
    def retention(self) -> RetentionPolicy:
        with self._lock:
            return RetentionPolicy.merge(list(self._policies.values()))

    # -- acquisition --------------------------------------------------------------------

    async def acquire[T](
        self,
        key: ResourceKey,
        factory: Callable[[], Awaitable[T]],
        *,
        close: Callable[[T], Awaitable[None]] | None = None,
        warm: Callable[[T], Awaitable[None]] | None = None,
        kind: Kind = "connection",
    ) -> Lease[T]:
        """Return a lease on the resource for ``key``, building it once if needed.

        Concurrent first acquisitions share one build. A failed build is not cached: every
        waiter sees the error and the next acquire builds again. A cancelled builder (for
        example a warm-up that timed out) never fails its waiters: they retry the build.
        """
        if key.pid != os.getpid():
            raise RuntimeError("resource key was created in another process")
        loop = asyncio.get_running_loop() if key.scope.startswith("loop:") else None
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and (
                    entry.closing or (entry.loop is not None and entry.loop.is_closed())
                ):
                    self._entries.pop(key, None)
                    entry = None
                builder = entry is None
                if entry is None:
                    entry = _Entry(key, concurrent.futures.Future(), close, warm, loop, kind)
                    self._entries[key] = entry
                entry.refs += 1
                entry.last_used = self._clock()
            if builder:
                before = self._rss_mb() if kind == "weights" else None
                try:
                    value = await factory()
                except BaseException as exc:
                    with self._lock:
                        if self._entries.get(key) is entry:
                            self._entries.pop(key)
                    entry.built.set_exception(
                        _BuildCancelled() if isinstance(exc, asyncio.CancelledError) else exc
                    )
                    raise
                if before is not None:
                    after = self._rss_mb()
                    if after is not None:
                        entry.size_mb = max(0.0, after - before)
                entry.built.set_result(value)
                _log.debug("provider.shared_resource_built", category=key.category, scope=key.scope)
                if kind == "weights":
                    await self._close_all(self._select_victims(self.retention, None))
                return Lease(self, entry)
            try:
                await asyncio.shield(asyncio.wrap_future(entry.built))
            except _BuildCancelled:
                await asyncio.shield(self._release(entry))
                continue  # the builder was cancelled: build it here instead
            except BaseException:
                # Cancelled waiter or failed build: drop this reference; if it was the
                # last one on a built resource, _release closes it (no leak).
                await asyncio.shield(self._release(entry))
                raise
            return Lease(self, entry)

    async def _warm(self, entry: _Entry[Any]) -> None:
        if entry.warm_fn is None:
            return
        while True:
            with self._lock:
                runner = entry.warm_done is None
                if runner:
                    entry.warm_done = concurrent.futures.Future()
                done = entry.warm_done
            assert done is not None
            if not runner:
                try:
                    await asyncio.shield(asyncio.wrap_future(done))
                except _BuildCancelled:
                    continue  # the warming holder was cancelled: warm here instead
                return
            try:
                await entry.warm_fn(entry.built.result())
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                if not cancelled:
                    entry.warm_error = type(exc).__name__
                with self._lock:
                    entry.warm_done = None  # a later warm() may retry
                done.set_exception(_BuildCancelled() if cancelled else exc)
                raise
            entry.warm_error = None
            done.set_result(None)
            return

    async def _release(self, entry: _Entry[Any]) -> None:
        policy = self.retention
        with self._lock:
            entry.refs -= 1
            entry.last_used = self._clock()
            last = entry.refs <= 0
            retain = (
                last
                and policy.keep_warm
                and entry.kind == "weights"
                and entry.loop is None
                and self._entries.get(entry.key) is entry
                and entry.usable()
            )
            if last and not retain:
                entry.closing = True
                if self._entries.get(entry.key) is entry:
                    self._entries.pop(entry.key)
        if retain:
            if policy.idle_ttl_s is not None:
                self._schedule_sweep(policy.idle_ttl_s)
            await self._close_all(self._select_victims(policy, None))
            return
        if last and entry.close is not None and entry.usable():
            await entry.close(entry.built.result())

    # -- eviction -----------------------------------------------------------------------

    def _select_victims(self, policy: RetentionPolicy, now: float | None) -> list[_Entry[Any]]:
        """Pick idle retained weights to close (never one with a holder) and unmap them."""
        with self._lock:
            weights = [e for e in self._entries.values() if e.kind == "weights" and e.usable()]
            idle = sorted((e for e in weights if e.refs <= 0), key=lambda e: e.last_used)
            victims: list[_Entry[Any]] = []
            if now is not None and policy.idle_ttl_s is not None:
                victims.extend(e for e in idle if now - e.last_used >= policy.idle_ttl_s)
            cap = policy.max_models_per_category
            if cap is not None:
                counts: dict[str, int] = {}
                for e in weights:
                    if e not in victims:
                        counts[e.key.category] = counts.get(e.key.category, 0) + 1
                for e in idle:
                    if e not in victims and counts[e.key.category] > cap:
                        victims.append(e)
                        counts[e.key.category] -= 1
            budget = policy.memory_budget_mb
            if budget is not None:
                total = sum(e.size_mb or 0.0 for e in weights if e not in victims)
                for e in idle:
                    if total <= budget:
                        break
                    if e in victims or not e.size_mb:
                        continue  # an unmeasured load frees nothing the budget can count
                    victims.append(e)
                    total -= e.size_mb
            for e in victims:
                e.closing = True
                if self._entries.get(e.key) is e:
                    self._entries.pop(e.key)
            return victims

    async def _close_all(self, victims: list[_Entry[Any]]) -> None:
        for e in victims:
            _log.debug("provider.shared_resource_evicted", category=e.key.category)
            if e.close is None:
                continue
            try:
                await e.close(e.built.result())
            except Exception as exc:  # one failed close must not keep the others loaded
                _log.warning(
                    "provider.shared_resource_evict_failed",
                    category=e.key.category,
                    error=type(exc).__name__,
                )

    def _schedule_sweep(self, delay_s: float) -> None:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            task = loop.create_task(self.sweep())
            self._sweeps.add(task)
            task.add_done_callback(self._sweeps.discard)

        loop.call_later(delay_s, fire)

    async def sweep(self) -> int:
        """Close idle retained weights past ``idle_ttl_s`` and enforce the memory budget and
        per-category cap. Returns how many closed (the idle timer runs this too)."""
        victims = self._select_victims(self.retention, self._clock())
        await self._close_all(victims)
        return len(victims)

    async def evict_idle(self, category: str | None = None) -> int:
        """Close every retained idle weight entry (optionally of one category)."""
        with self._lock:
            victims = [
                e
                for e in self._entries.values()
                if e.kind == "weights"
                and e.refs <= 0
                and e.usable()
                and (category is None or e.key.category == category)
            ]
            for e in victims:
                e.closing = True
                self._entries.pop(e.key)
        await self._close_all(victims)
        return len(victims)

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
                e.kind,
                e.refs <= 0 and e.usable(),
                e.size_mb,
            )
            for e in entries
        )

    def reset_after_fork(self) -> None:
        """Forget every entry without closing it: inherited native handles are invalid
        in a forked child and must not be closed or reused there."""
        self._lock = threading.Lock()
        self._entries = {}
        self._sweeps = set()


_REGISTRY = ResourceRegistry()


async def own_shared[T](
    resources: LifecycleManager,
    resource_id: str,
    key: ResourceKey,
    factory: Callable[[], Awaitable[T]],
    *,
    close: Callable[[T], Awaitable[None]] | None,
    depends_on: tuple[str, ...] = (),
    registry: ResourceRegistry | None = None,
) -> tuple[T, ResourceRef]:
    """Acquire a shared resource and register its lease release with one Engine.

    The Engine owns only its lease: its ``resource_id`` closer releases that lease, and the
    registry closes the resource when the last holder (in any Engine) releases. The closer
    is registered before the acquisition so a failed build still leaves a valid owner.
    """
    held: list[Lease[T]] = []

    async def release() -> None:
        if held:
            await held.pop().aclose()

    ref = resources.own(resource_id, release, depends_on=depends_on)
    lease = await (registry or _REGISTRY).acquire(key, factory, close=close)
    held.append(lease)
    return lease.value, ref


def shared_registry() -> ResourceRegistry:
    """The process-wide registry (one per process; reset in a forked child)."""
    return _REGISTRY


if hasattr(os, "register_at_fork"):  # POSIX only; Windows spawns fresh processes
    os.register_at_fork(after_in_child=_REGISTRY.reset_after_fork)
