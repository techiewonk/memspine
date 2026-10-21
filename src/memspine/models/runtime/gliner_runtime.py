"""Engine-owned, bounded GLiNER extractor lifecycle over process-shared weights.

This module has no GLiNER, Hugging Face, or Torch import at import time. Each Engine
constructs one ``GLiNERModelPool`` and injects it into the optional consumers.

Weights are keyed by checkpoint, revision, device and offline policy only (ADR-079 /
RM-73). They load through the process ``ResourceRegistry``: identical weights are loaded
once per process and shared by every consumer and every Engine, and the last holder's
release disposes them. ``tasks`` and ``inference_concurrency`` are per-lease limits: profiles
that differ only in them share one load, and each distinct limit gets its own concurrency
bound inside the owning Engine. Extraction results are never shared.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from memspine.providers.shared import Lease, ResourceRegistry, resource_key, shared_registry

__all__ = [
    "GLiNERModelBinding",
    "GLiNERModelLease",
    "GLiNERModelPool",
    "GLiNERPoolClosedError",
    "GLiNERPoolFullError",
]

T = TypeVar("T")


class GlinerSettingsLike(Protocol):
    """Structural subset of the canonical config model (keeps optional runtime lazy)."""

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


class GlinerWeightSettingsLike(Protocol):
    """What a loader receives: only the weight-affecting settings."""

    @property
    def model(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def device(self) -> str: ...

    @property
    def local_files_only(self) -> bool: ...


class GLiNERPoolClosedError(RuntimeError):
    """Raised when a closed pool or lease receives new work."""


class GLiNERPoolFullError(RuntimeError):
    """Raised when capacity has no releasable slot for another model."""


@dataclass(frozen=True, slots=True)
class _WeightKey:
    """Everything that changes the loaded weights; also the frozen loader input."""

    model: str
    revision: str | None
    device: str
    local_files_only: bool


@dataclass(frozen=True, slots=True)
class _LimitKey:
    """A lease's per-call limits on top of shared weights (never part of the weight key)."""

    weights: _WeightKey
    tasks: tuple[str, ...]
    inference_concurrency: int


@dataclass(slots=True)
class _LoadSlot:
    waiters: int = 0
    task: asyncio.Task[_Entry] | None = None


@dataclass(slots=True)
class _Entry:
    key: _WeightKey
    model: Any | None
    shared: Lease[Any] | None = None
    references: int = 0
    reservations: int = 0
    active_calls: int = 0


ModelLoader = Callable[[GlinerWeightSettingsLike], Any]


class GLiNERModelPool:
    """Bound one Engine's resident GLiNER weights and serialize work per lease limit.

    Concurrent acquires for the same weights share a single worker-thread load (and, through
    the process registry, so do other Engines). Calls run in worker threads; an async
    semaphore per distinct ``(weights, tasks, inference_concurrency)`` bounds concurrency.
    ``max_models`` bounds distinct weights this Engine keeps resident: an idle entry (no
    reservation or active call) may be evicted for another checkpoint even while leased, and
    its leases transparently reload it on their next call. Closing rejects new work, waits
    for pending loads/calls and releases this Engine's hold on each loaded model.
    """

    def __init__(
        self,
        *,
        max_models: int = 1,
        loader: ModelLoader | None = None,
        registry: ResourceRegistry | None = None,
    ) -> None:
        if isinstance(max_models, bool) or not isinstance(max_models, int) or max_models < 1:
            raise ValueError("max_models must be at least one")
        self._max_models = max_models
        self._loader = loader or _load_local_gliner
        self._loader_id = _callable_identity(self._loader)
        self._registry = registry if registry is not None else shared_registry()
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)
        self._entries: OrderedDict[_WeightKey, _Entry] = OrderedDict()
        self._loading: dict[_WeightKey, _LoadSlot] = {}
        self._limits: dict[_LimitKey, asyncio.Semaphore] = {}
        self._disposals: set[asyncio.Task[None]] = set()
        self._calls: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def binding(self, settings: GlinerSettingsLike) -> GLiNERModelBinding:
        """A consumer's lazy, Engine-lifetime lease (acquired on first call, released at
        pool close)."""
        return GLiNERModelBinding(self, settings)

    async def acquire(self, settings: GlinerSettingsLike) -> GLiNERModelLease:
        """Acquire an explicit lease, coalescing a first load for identical weights."""
        limit = _limit_key(settings)
        entry = await self._acquire_entry(limit.weights, begin_call=False)
        semaphore = self._limits.get(limit)
        if semaphore is None:
            semaphore = self._limits[limit] = asyncio.Semaphore(limit.inference_concurrency)
        return GLiNERModelLease(self, entry, limit, semaphore)

    async def _acquire_entry(self, key: _WeightKey, *, begin_call: bool) -> _Entry:
        """Reference (and with ``begin_call`` also start a call on) the entry for ``key``."""
        async with self._lock:
            self._ensure_open()
            entry = self._entries.get(key)
            if entry is not None:
                entry.references += 1
                if begin_call:
                    entry.active_calls += 1
                self._entries.move_to_end(key)
                return entry

            slot = self._loading.get(key)
            if slot is None:
                await self._make_capacity()
                slot = _LoadSlot()
                self._loading[key] = slot
                slot.task = asyncio.create_task(self._load_entry(key, key, slot))
                # A sole waiter may cancel while the worker load continues. Retrieve
                # a detached failure without changing what remaining waiters see.
                slot.task.add_done_callback(_consume_task_result)
            slot.waiters += 1

        try:
            assert slot.task is not None
            # A shield keeps one cancelled waiter from cancelling the shared load.
            entry = await asyncio.shield(slot.task)
            async with self._changed:
                self._ensure_open()
                current = self._entries.get(key)
                if current is not entry:
                    raise GLiNERPoolClosedError("GLiNER pool closed before lease acquisition")
                entry.reservations -= 1
                entry.references += 1
                if begin_call:
                    entry.active_calls += 1
                self._entries.move_to_end(key)
                self._changed.notify_all()
                return entry
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_reservation(key, slot))
            raise

    async def aclose(self) -> None:
        """Close once, drain loads/calls and release this Engine's model holds."""
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_impl())
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _close_impl(self) -> None:
        async with self._changed:
            pending: list[asyncio.Task[Any]] = [
                slot.task for slot in self._loading.values() if slot.task is not None
            ]
            pending.extend(self._disposals)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        async with self._changed:
            await self._changed.wait_for(
                lambda: (
                    not self._loading
                    and all(entry.active_calls == 0 for entry in self._entries.values())
                )
            )
            entries = tuple(self._entries.values())
            self._entries.clear()
            calls = tuple(self._calls)
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        await asyncio.gather(*(_dispose_entry(entry) for entry in entries))

    async def _load_entry(self, settings: _WeightKey, key: _WeightKey, slot: _LoadSlot) -> _Entry:
        shared: Lease[Any] | None = None
        try:
            registry_key = resource_key(
                "model.gliner",
                {"model": settings.model, "revision": settings.revision, "loader": self._loader_id},
                {"device": settings.device, "local_files_only": settings.local_files_only},
                scope="thread",
            )
            shared = await self._registry.acquire(
                registry_key, lambda: self._load_model(settings), close=_dispose
            )
            entry = _Entry(key=key, model=shared.value, shared=shared)
            async with self._changed:
                self._loading.pop(key, None)
                if self._closed:
                    entry.reservations = 0
                else:
                    entry.reservations = slot.waiters
                    self._entries[key] = entry
                    self._entries.move_to_end(key)
                self._changed.notify_all()
            if self._closed:
                await _dispose_entry(entry)
                raise GLiNERPoolClosedError("GLiNER pool closed while model was loading")
            return entry
        except BaseException:
            async with self._changed:
                self._loading.pop(key, None)
                self._changed.notify_all()
            raise

    async def _load_model(self, settings: _WeightKey) -> Any:
        model = await asyncio.to_thread(self._loader, settings)
        if inspect.isawaitable(model):
            close = getattr(model, "close", None)
            if callable(close):
                close()  # never leave an un-awaited coroutine behind
            raise TypeError("GLiNER model loader must be synchronous")
        return model

    async def _make_capacity(self) -> None:
        while len(self._entries) + len(self._loading) + len(self._disposals) >= self._max_models:
            idle = [
                (key, entry)
                for key, entry in self._entries.items()
                if entry.reservations == 0 and entry.active_calls == 0
            ]
            # Unleased entries first, then the least recently used leased one: a leased
            # entry reloads on its next call, so eviction never fails a holder.
            unleased = [key for key, entry in idle if entry.references == 0]
            evict_key = unleased[0] if unleased else (idle[0][0] if idle else None)
            if evict_key is None and self._disposals:
                await asyncio.shield(next(iter(self._disposals)))
                continue
            if evict_key is None:
                raise GLiNERPoolFullError(
                    f"GLiNER pool capacity {self._max_models} is active, loading, or disposing"
                )
            entry = self._entries.pop(evict_key)
            # Keep disposal in the capacity accounting even if this acquire is
            # cancelled; the model is not evictable memory until close completes.
            task = asyncio.create_task(_dispose_entry(entry))
            self._disposals.add(task)
            task.add_done_callback(self._discard_disposal)
            await asyncio.shield(task)

    def _discard_disposal(self, task: asyncio.Task[None]) -> None:
        self._disposals.discard(task)

    def _track_call(self, task: asyncio.Task[Any]) -> None:
        self._calls.add(task)
        task.add_done_callback(self._call_finished)

    def _call_finished(self, task: asyncio.Task[Any]) -> None:
        self._calls.discard(task)
        _consume_task_result(task)

    async def _cancel_reservation(self, key: _WeightKey, slot: _LoadSlot) -> None:
        async with self._changed:
            current_slot = self._loading.get(key)
            if current_slot is slot:
                slot.waiters = max(0, slot.waiters - 1)
            else:
                entry = self._entries.get(key)
                if entry is not None and entry.reservations:
                    entry.reservations -= 1
            self._changed.notify_all()

    async def _release(self, entry: _Entry) -> None:
        async with self._changed:
            if entry.references <= 0:
                return
            entry.references -= 1
            self._changed.notify_all()

    async def _begin_call(self, lease: GLiNERModelLease) -> _Entry:
        """Start a call on the lease's weights, reloading them if they were evicted."""
        async with self._changed:
            self._ensure_open()
            if lease._released:
                raise GLiNERPoolClosedError("GLiNER model lease has been released")
            entry = self._entries.get(lease._limit.weights)
            if entry is not None and entry is lease._entry:
                entry.active_calls += 1
                self._entries.move_to_end(entry.key)
                self._changed.notify_all()
                return entry
        # Evicted for another checkpoint: take a fresh reference and start the call
        # atomically, so it cannot be evicted again before the call begins.
        entry = await self._acquire_entry(lease._limit.weights, begin_call=True)
        lease._entry = entry
        return entry

    async def _end_call(self, entry: _Entry) -> None:
        async with self._changed:
            entry.active_calls -= 1
            self._changed.notify_all()

    async def _execute_call(
        self, entry: _Entry, semaphore: asyncio.Semaphore, callback: Callable[[Any], T]
    ) -> T:
        try:
            async with semaphore:
                model = entry.model
                if model is None:
                    raise GLiNERPoolClosedError("GLiNER model resource has been disposed")
                return await asyncio.to_thread(callback, model)
        finally:
            await self._end_call(entry)

    def _ensure_open(self) -> None:
        if self._closed:
            raise GLiNERPoolClosedError("GLiNER model pool is closed")


class GLiNERModelLease:
    """A reference-counted handle to shared weights plus its per-lease call bound."""

    def __init__(
        self,
        pool: GLiNERModelPool,
        entry: _Entry,
        limit: _LimitKey,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self._pool = pool
        self._entry = entry
        self._limit = limit
        self._semaphore = semaphore
        self._released = False
        self._release_lock = asyncio.Lock()

    async def call(self, callback: Callable[[Any], T]) -> T:
        """Run a complete synchronous SDK operation off-loop under its call bound."""
        entry = await self._pool._begin_call(self)
        task = asyncio.create_task(self._pool._execute_call(entry, self._semaphore, callback))
        self._pool._track_call(task)
        try:
            # Cancellation only detaches this waiter. The independent task owns the
            # active counter until the non-cancellable worker thread has completed.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_task_result)
            raise

    async def aclose(self) -> None:
        async with self._release_lock:
            if not self._released:
                self._released = True
                await self._pool._release(self._entry)

    async def __aenter__(self) -> GLiNERModelLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


class GLiNERModelBinding:
    """A consumer's Engine-lifetime lease: acquired on first call and kept, so per-call
    acquire/release never churns the pool. The pool's close ends it; a failed first
    acquisition is not cached and the next call retries."""

    def __init__(self, pool: GLiNERModelPool, settings: GlinerSettingsLike) -> None:
        self._pool = pool
        self._settings = settings
        self._lease: GLiNERModelLease | None = None
        self._lock = asyncio.Lock()

    async def call(self, callback: Callable[[Any], T]) -> T:
        lease = self._lease
        if lease is None:
            async with self._lock:
                if self._lease is None:
                    self._lease = await self._pool.acquire(self._settings)
                lease = self._lease
        return await lease.call(callback)


async def _dispose(model: Any) -> None:
    """Release a model without importing its optional framework in core."""
    close = getattr(model, "close", None)
    if close is None:
        close = getattr(model, "dispose", None)
    if close is None:
        return
    result = await asyncio.to_thread(close)
    if inspect.isawaitable(result):
        await result


async def _dispose_entry(entry: _Entry) -> None:
    """Drop this Engine's hold; the registry disposes the model on the process's last one."""
    shared, entry.shared = entry.shared, None
    # Clear the strong reference held by leases as well as the pool entry.
    entry.model = None
    if shared is not None:
        await shared.aclose()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve detached call failures so cancellation does not leak warnings."""
    if not task.cancelled():
        task.exception()


def _callable_identity(func: Callable[..., Any]) -> str:
    """Distinguish loaders in the weight key (a test fake never shares a real load)."""
    name = getattr(func, "__qualname__", type(func).__qualname__)
    return f"{getattr(func, '__module__', '')}.{name}#{id(func)}"


def _limit_key(settings: GlinerSettingsLike) -> _LimitKey:
    concurrency = settings.inference_concurrency
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("inference_concurrency must be a positive integer")
    raw_tasks = tuple(settings.tasks)
    if any(not isinstance(task, str) or not task.strip() for task in raw_tasks):
        raise ValueError("tasks must contain non-empty task names")
    tasks = tuple(sorted(set(raw_tasks)))
    if not tasks:
        raise ValueError("tasks must contain at least one task name")
    if not isinstance(settings.local_files_only, bool):
        raise ValueError("local_files_only must be a bool")
    if not isinstance(settings.model, str):
        raise ValueError("model must be a non-empty string")
    model = settings.model.strip()
    if not model:
        raise ValueError("model must not be empty")
    if settings.revision is not None and not isinstance(settings.revision, str):
        raise ValueError("revision must be a string or None")
    revision = settings.revision.strip() if settings.revision else None
    if settings.revision is not None and not revision:
        raise ValueError("revision must be a non-empty string or None")
    if not isinstance(settings.device, str) or not settings.device.strip():
        raise ValueError("device must not be empty")
    device = settings.device.strip()
    weights = _WeightKey(model, revision, device, settings.local_files_only)
    return _LimitKey(weights, tasks, concurrency)


def _load_local_gliner(settings: GlinerWeightSettingsLike) -> Any:
    """Resolve a concrete snapshot then use the SDK loader only with a local path."""
    model_path = Path(settings.model).expanduser()
    if model_path.is_dir():
        snapshot = model_path
        if settings.revision is not None and snapshot.name != settings.revision:
            raise RuntimeError(
                "An explicit revision for a local GLiNER snapshot must match its directory name"
            )
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("GLiNER runtime requires the optional [ner] dependencies") from exc
        snapshot = Path(
            snapshot_download(
                repo_id=settings.model,
                revision=settings.revision,
                local_files_only=settings.local_files_only,
            )
        )
    _validate_snapshot(snapshot)
    try:
        from gliner2 import AutoExtractor
    except ImportError as exc:
        raise RuntimeError("GLiNER runtime requires the optional [ner] dependencies") from exc

    # AutoExtractor drops Hub kwargs on the selected model loader. Passing the
    # resolved local snapshot ensures the requested revision/local-only policy
    # applies to both config and weights. map_location is a model load option.
    return AutoExtractor.from_pretrained(str(snapshot), map_location=settings.device)


def _validate_snapshot(snapshot: Path) -> None:
    """Fail before SDK construction if a local snapshot could need Hub fallback."""
    if not snapshot.is_dir():
        raise RuntimeError(f"GLiNER checkpoint is not a local snapshot directory: {snapshot}")
    required = [
        snapshot / "config.json",
        snapshot / "encoder_config" / "config.json",
        snapshot / "tokenizer_config.json",
    ]
    missing = [str(path.relative_to(snapshot)) for path in required if not path.is_file()]
    has_weights = any(
        (snapshot / filename).is_file() for filename in ("model.safetensors", "pytorch_model.bin")
    )
    tokenizer_files = (
        "tokenizer.json",
        "vocab.txt",
        "spiece.model",
        "sentencepiece.bpe.model",
    )
    has_tokenizer = any((snapshot / filename).is_file() for filename in tokenizer_files)
    has_bpe_files = (snapshot / "vocab.json").is_file() and (snapshot / "merges.txt").is_file()
    if not has_weights:
        missing.append("model.safetensors or pytorch_model.bin")
    if not has_tokenizer and not has_bpe_files:
        missing.append("tokenizer.json or supported tokenizer vocabulary files")
    if missing:
        raise RuntimeError(
            f"Incomplete local GLiNER checkpoint {snapshot}; missing: {', '.join(missing)}"
        )
    try:
        config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        json.loads((snapshot / "encoder_config" / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read GLiNER checkpoint config files in {snapshot}") from exc
    architecture = config.get("architecture")
    if architecture not in {"boundary", "span"}:
        raise RuntimeError(f"Unsupported GLiNER checkpoint architecture: {architecture!r}")
