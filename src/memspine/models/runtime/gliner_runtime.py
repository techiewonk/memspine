"""Engine-owned, bounded GLiNER extractor lifecycle.

This module has no GLiNER, Hugging Face, or Torch import at import time. Construct
``GLiNERModelPool`` explicitly and inject its leases into the optional adapters;
there is intentionally no process-global model cache.
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

__all__ = [
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


class GLiNERPoolClosedError(RuntimeError):
    """Raised when a closed pool or lease receives new work."""


class GLiNERPoolFullError(RuntimeError):
    """Raised when capacity has no releasable slot for another model."""


@dataclass(frozen=True, slots=True)
class _ModelKey:
    model: str
    revision: str | None
    device: str
    local_files_only: bool
    tasks: tuple[str, ...]
    inference_concurrency: int


@dataclass(frozen=True, slots=True)
class _FrozenSettings:
    model: str
    revision: str | None
    device: str
    local_files_only: bool
    tasks: tuple[str, ...]
    inference_concurrency: int


@dataclass(slots=True)
class _LoadSlot:
    waiters: int = 0
    task: asyncio.Task[_Entry] | None = None


@dataclass(slots=True)
class _Entry:
    key: _ModelKey
    model: Any | None
    semaphore: asyncio.Semaphore
    references: int = 0
    reservations: int = 0
    active_calls: int = 0


ModelLoader = Callable[[GlinerSettingsLike], Any]


class GLiNERModelPool:
    """Own a finite set of models and serialize work according to each setting.

    Concurrent acquires for the same key share a single worker-thread load. Calls
    are also run in worker threads, while an async semaphore bounds concurrency.
    Closing rejects new work, waits for pending loads/calls, and disposes each
    loaded model once. Instances belong to one engine/runtime owner.
    """

    def __init__(self, *, max_models: int = 1, loader: ModelLoader | None = None) -> None:
        if isinstance(max_models, bool) or not isinstance(max_models, int) or max_models < 1:
            raise ValueError("max_models must be at least one")
        self._max_models = max_models
        self._loader = loader or _load_local_gliner
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)
        self._entries: OrderedDict[_ModelKey, _Entry] = OrderedDict()
        self._loading: dict[_ModelKey, _LoadSlot] = {}
        self._disposals: set[asyncio.Task[None]] = set()
        self._calls: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def acquire(self, settings: GlinerSettingsLike) -> GLiNERModelLease:
        """Acquire an explicit lease, coalescing a first load for identical settings."""
        key = _key(settings)
        async with self._lock:
            self._ensure_open()
            entry = self._entries.get(key)
            if entry is not None:
                entry.references += 1
                self._entries.move_to_end(key)
                return GLiNERModelLease(self, entry)

            slot = self._loading.get(key)
            if slot is None:
                await self._make_capacity()
                slot = _LoadSlot()
                self._loading[key] = slot
                frozen = _FrozenSettings(
                    model=key.model,
                    revision=key.revision,
                    device=key.device,
                    local_files_only=key.local_files_only,
                    tasks=key.tasks,
                    inference_concurrency=key.inference_concurrency,
                )
                slot.task = asyncio.create_task(self._load_entry(frozen, key, slot))
                # A sole waiter may cancel while the worker load continues. Retrieve
                # a detached failure without changing what remaining waiters see.
                slot.task.add_done_callback(_consume_task_result)
            slot.waiters += 1

        try:
            assert slot.task is not None
            # A shield keeps one cancelled waiter from cancelling the shared load.
            entry = await asyncio.shield(slot.task)
            async with self._lock:
                self._ensure_open()
                current = self._entries.get(key)
                if current is not entry:
                    raise GLiNERPoolClosedError("GLiNER pool closed before lease acquisition")
                entry.reservations -= 1
                entry.references += 1
                self._entries.move_to_end(key)
                return GLiNERModelLease(self, entry)
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_reservation(key, slot))
            raise

    async def aclose(self) -> None:
        """Close once, drain loads/calls and release loaded model resources."""
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

    async def _load_entry(
        self, settings: _FrozenSettings, key: _ModelKey, slot: _LoadSlot
    ) -> _Entry:
        try:
            model = await asyncio.to_thread(self._loader, settings)
            if inspect.isawaitable(model):
                raise TypeError("GLiNER model loader must be synchronous")
            entry = _Entry(
                key=key,
                model=model,
                semaphore=asyncio.Semaphore(settings.inference_concurrency),
            )
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
                await _dispose(model)
                entry.model = None
                raise GLiNERPoolClosedError("GLiNER pool closed while model was loading")
            return entry
        except BaseException:
            async with self._changed:
                self._loading.pop(key, None)
                self._changed.notify_all()
            raise

    async def _make_capacity(self) -> None:
        while len(self._entries) + len(self._loading) + len(self._disposals) >= self._max_models:
            evict_key = next(
                (
                    key
                    for key, entry in self._entries.items()
                    if entry.references == 0 and entry.reservations == 0 and entry.active_calls == 0
                ),
                None,
            )
            if evict_key is None and self._disposals:
                await asyncio.shield(next(iter(self._disposals)))
                continue
            if evict_key is None:
                raise GLiNERPoolFullError(
                    f"GLiNER pool capacity {self._max_models} is leased, active, loading, or disposing"
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

    async def _cancel_reservation(self, key: _ModelKey, slot: _LoadSlot) -> None:
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

    async def _begin_call(self, entry: _Entry, released: bool) -> None:
        async with self._changed:
            self._ensure_open()
            if released or self._entries.get(entry.key) is not entry:
                raise GLiNERPoolClosedError("GLiNER model lease has been released")
            entry.active_calls += 1
            self._changed.notify_all()

    async def _end_call(self, entry: _Entry) -> None:
        async with self._changed:
            entry.active_calls -= 1
            self._changed.notify_all()

    async def _execute_call(self, entry: _Entry, callback: Callable[[Any], T]) -> T:
        try:
            async with entry.semaphore:
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
    """A reference-counted handle to one model; release it with ``aclose``."""

    def __init__(self, pool: GLiNERModelPool, entry: _Entry) -> None:
        self._pool = pool
        self._entry = entry
        self._released = False
        self._release_lock = asyncio.Lock()

    async def call(self, callback: Callable[[Any], T]) -> T:
        """Run a complete synchronous SDK operation off-loop under its call bound."""
        await self._pool._begin_call(self._entry, self._released)
        task = asyncio.create_task(self._pool._execute_call(self._entry, callback))
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
    model = entry.model
    if model is None:
        return
    try:
        await _dispose(model)
    finally:
        # Clear the strong reference held by leases as well as the pool entry.
        entry.model = None


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve detached call failures so cancellation does not leak warnings."""
    if not task.cancelled():
        task.exception()


def _key(settings: GlinerSettingsLike) -> _ModelKey:
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
    return _ModelKey(model, revision, device, settings.local_files_only, tasks, concurrency)


def _load_local_gliner(settings: GlinerSettingsLike) -> Any:
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
