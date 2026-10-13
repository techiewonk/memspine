"""Scheduler (RT-02; P-20, G-19 A): priority classes, one global model gate, encoder executors.

All in-process asyncio (one process per store). Three pieces, each behind a flag (default OFF):

``ModelGate``         ONE async semaphore for every model call (LLM, embedding, rerank), shared
                      across pipelines (``runtime.model_concurrency`` = slots; 0 = no gate).
                      Waiters are served by priority class, foreground read > foreground write >
                      background derive, FIFO within a class. Background derive may hold at most
                      ``slots - background_reserve`` slots (at least 1), so a burst of derived work
                      cannot occupy every slot while a read arrives.
``EncoderExecutor``   a dedicated dispatcher per encoder (documents, queries): concurrent
                      ``embed`` calls are micro-batched (size cap, short linger for background
                      work only, never for a read) and run one batch at a time, highest priority
                      first. Reads never wait to fill a batch; they batch only while a previous
                      batch is running.
``Scheduler``         owns the gate, the executors, the SLO ledger and the backpressure policy, and
                      installs the gate at the ``models.concurrency`` seam.

Backpressure itself is ``derive.backpressure`` (a pure policy); ``InlineRunner`` applies it at
enqueue. Tap events: ``runtime.sched.{grant,batch,install,uninstall}``.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from memspine.derive.backpressure import Backpressure
from memspine.economy.slo import SloLedger, install_ledger
from memspine.models.concurrency import (
    Priority,
    current_priority,
    install_gate,
    installed_gate,
    priority_scope,
)
from memspine.observability import tap

if TYPE_CHECKING:
    from memspine.config.schema.runtime import RuntimeConfig

__all__ = [
    "BatchedEmbedding",
    "EncoderExecutor",
    "ModelGate",
    "Priority",
    "Scheduler",
    "acquire",
    "active",
    "release",
]


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    future: asyncio.Future[None] = field(compare=False)
    kind: str = field(compare=False, default="llm")


class ModelGate:
    """The global model semaphore with priority classes. See the module docstring."""

    def __init__(self, slots: int, *, background_reserve: int = 1) -> None:
        if slots < 1:
            raise ValueError("slots must be >= 1")
        self.slots = slots
        self.background_max = max(1, slots - max(0, background_reserve))
        self._active = 0
        self._background_active = 0
        self._seq = 0
        self._waiters: list[_Waiter] = []
        #: observability: the highest concurrency reached, and grants/waits per class
        self.max_active = 0
        self.grants = {p: 0 for p in Priority}
        self.waits = {p: 0 for p in Priority}

    @property
    def active(self) -> int:
        return self._active

    @property
    def queued(self) -> int:
        return len(self._waiters)

    def _eligible(self, priority: int) -> bool:
        if self._active >= self.slots:
            return False
        return priority != Priority.DERIVE or self._background_active < self.background_max

    def _grant(self, priority: int) -> None:
        self._active += 1
        if priority == Priority.DERIVE:
            self._background_active += 1
        self.max_active = max(self.max_active, self._active)
        self.grants[Priority(priority)] += 1

    def _dispatch(self) -> None:
        """Grant free slots to the best eligible waiters (class first, then arrival order)."""
        i = 0
        while i < len(self._waiters) and self._active < self.slots:
            w = self._waiters[i]
            if w.future.done():  # cancelled while waiting
                del self._waiters[i]
                continue
            if self._eligible(w.priority):
                del self._waiters[i]
                self._grant(w.priority)
                w.future.set_result(None)
                i = 0  # a grant can change eligibility of the others
                continue
            i += 1

    async def acquire(self, priority: Priority = Priority.READ, kind: str = "llm") -> None:
        p = int(priority)
        if not self._waiters and self._eligible(p):
            self._grant(p)
            if tap.enabled():
                tap.event(
                    "runtime.sched.grant",
                    priority=Priority(p).name.lower(),
                    kind=kind,
                    waited_s=0.0,
                    active=self._active,
                )
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        waiter = _Waiter(p, self._seq, fut, kind)
        bisect.insort(self._waiters, waiter)
        self.waits[Priority(p)] += 1
        started = time.perf_counter()
        self._dispatch()  # a slot may be free for this class even though others are queued
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():  # granted just as we were cancelled
                self._release(p)
            elif waiter in self._waiters:
                self._waiters.remove(waiter)
            raise
        if tap.enabled():
            tap.event(
                "runtime.sched.grant",
                priority=Priority(p).name.lower(),
                kind=kind,
                waited_s=round(time.perf_counter() - started, 6),
                active=self._active,
                queued=len(self._waiters),
            )

    def _release(self, priority: int) -> None:
        self._active -= 1
        if priority == Priority.DERIVE:
            self._background_active -= 1
        self._dispatch()

    @contextlib.asynccontextmanager
    async def slot(self, priority: Priority, kind: str = "llm") -> AsyncIterator[None]:
        await self.acquire(priority, kind)
        try:
            yield
        finally:
            self._release(int(priority))


# --------------------------------------------------------------------------- encoder executors
BatchFn = Callable[[list[str]], Awaitable[list[list[float]]]]


@dataclass(order=True)
class _Pending:
    priority: int
    seq: int
    texts: list[str] = field(compare=False)
    future: asyncio.Future[list[list[float]]] = field(compare=False)


class EncoderExecutor:
    """A dedicated dispatcher that micro-batches concurrent encoder calls (one batch at a time)."""

    def __init__(
        self, name: str, fn: BatchFn, *, max_batch: int = 32, max_wait_s: float = 0.005
    ) -> None:
        self.name = name
        self._fn = fn
        self.max_batch = max(1, max_batch)
        self.max_wait_s = max(0.0, max_wait_s)
        self._queue: list[_Pending] = []
        self._seq = 0
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.batches = 0
        self.items = 0
        self.max_batch_seen = 0

    def _ensure(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done() or self._loop is not loop:
            self._loop = loop
            self._wake = asyncio.Event()
            self._task = loop.create_task(self._run(), name=f"encoder-{self.name}")

    async def submit(self, texts: list[str], priority: Priority | None = None) -> list[list[float]]:
        if not texts:
            return []
        self._ensure()
        assert self._wake is not None
        fut: asyncio.Future[list[list[float]]] = asyncio.get_running_loop().create_future()
        self._seq += 1
        p = int(current_priority() if priority is None else priority)
        bisect.insort(self._queue, _Pending(p, self._seq, list(texts), fut))
        self._wake.set()
        return await fut

    def _take(self) -> list[_Pending]:
        batch: list[_Pending] = []
        total = 0
        while self._queue:
            nxt = self._queue[0]
            if nxt.future.done():  # caller went away
                self._queue.pop(0)
                continue
            if batch and total + len(nxt.texts) > self.max_batch:
                break
            batch.append(self._queue.pop(0))
            total += len(nxt.texts)
        return batch

    async def _run(self) -> None:
        assert self._wake is not None
        wake = self._wake
        while True:
            if not self._queue:
                wake.clear()
                await wake.wait()
                continue
            top = self._queue[0]
            queued_texts = sum(len(p.texts) for p in self._queue)
            if top.priority != Priority.READ and queued_texts < self.max_batch and self.max_wait_s:
                await asyncio.sleep(self.max_wait_s)  # linger for background work only
            batch = self._take()
            if not batch:
                continue
            merged = [t for p in batch for t in p.texts]
            lead = Priority(min(p.priority for p in batch))
            self.batches += 1
            self.items += len(batch)
            self.max_batch_seen = max(self.max_batch_seen, len(merged))
            if tap.enabled():
                tap.event(
                    "runtime.sched.batch",
                    encoder=self.name,
                    calls=len(batch),
                    texts=len(merged),
                    priority=lead.name.lower(),
                )
            try:
                with priority_scope(lead):
                    vectors = await self._fn(merged)
                if len(vectors) != len(merged):
                    raise ValueError(f"{len(vectors)} vectors for {len(merged)} texts")
            except Exception as exc:
                for p in batch:
                    if not p.future.done():
                        p.future.set_exception(exc)
                continue
            at = 0
            for p in batch:
                part = vectors[at : at + len(p.texts)]
                at += len(p.texts)
                if not p.future.done():
                    p.future.set_result(part)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await task
        for p in self._queue:
            if not p.future.done():
                p.future.cancel()
        self._queue.clear()


class BatchedEmbedding:
    """An ``EmbeddingService`` whose ``embed`` / ``embed_queries`` run on dedicated executors.

    Documents and queries get separate executors (a read's query embedding never queues behind a
    background document batch beyond the one already running). Everything else is delegated."""

    def __init__(self, inner: Any, *, max_batch: int = 32, max_wait_s: float = 0.005) -> None:
        self._inner = inner
        self.docs = EncoderExecutor(
            "embed", inner.embed, max_batch=max_batch, max_wait_s=max_wait_s
        )
        queries = getattr(inner, "embed_queries", None)
        self.queries = (
            EncoderExecutor("embed_queries", queries, max_batch=max_batch, max_wait_s=max_wait_s)
            if queries is not None
            else None
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.docs.submit(texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if self.queries is None:
            return await self.docs.submit(texts)
        return await self.queries.submit(texts)

    async def close(self) -> None:
        await self.docs.close()
        if self.queries is not None:
            await self.queries.close()

    def __getattr__(self, name: str) -> Any:
        if name == "_inner" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._inner, name)


# --------------------------------------------------------------------------- the scheduler
class Scheduler:
    """Gate + executors + ledger + backpressure for one process (built from ``RuntimeConfig``)."""

    def __init__(self, cfg: RuntimeConfig, ledger: SloLedger | None = None) -> None:
        self.cfg = cfg
        self.gate: ModelGate | None = (
            ModelGate(cfg.model_concurrency, background_reserve=cfg.background_reserve)
            if cfg.model_concurrency > 0
            else None
        )
        self.ledger = ledger or SloLedger(
            read_p95_budget_s=cfg.read_p95_budget_s, lag_p95_budget_s=cfg.lag_p95_budget_s
        )
        self.backpressure: Backpressure | None = (
            Backpressure(
                coalesce_items=cfg.backlog_coalesce_items,
                coalesce_age_s=cfg.backlog_coalesce_age_s,
                max_items=cfg.backlog_max_items,
                max_age_s=cfg.backlog_max_age_s,
                max_total_items=cfg.backlog_max_total_items,
                retry_after_s=cfg.backlog_retry_after_s,
            )
            if cfg.scheduler_v2
            else None
        )
        self._embedders: list[BatchedEmbedding] = []
        self._previous_gate: Any = None
        self._previous_ledger: SloLedger | None = None
        self._installed = False

    def install(self) -> None:
        if self._installed:
            return
        self._previous_gate = installed_gate()
        if self.gate is not None:
            install_gate(self.gate)
        if self.cfg.scheduler_v2:
            self._previous_ledger = install_ledger(self.ledger)
        self._installed = True
        tap.event(
            "runtime.sched.install",
            slots=self.cfg.model_concurrency,
            scheduler_v2=self.cfg.scheduler_v2,
        )

    async def uninstall(self) -> None:
        if not self._installed:
            return
        if self.gate is not None and installed_gate() is self.gate:
            install_gate(self._previous_gate)
        if self.cfg.scheduler_v2:
            install_ledger(self._previous_ledger)
        for e in self._embedders:
            await e.close()
        self._embedders.clear()
        self._installed = False
        tap.event("runtime.sched.uninstall")

    def wrap_embedder(self, embedder: Any) -> Any:
        """Micro-batched executors around an embedder (only with ``runtime.scheduler_v2``)."""
        if not self.cfg.scheduler_v2 or self.cfg.encoder_batch_max <= 1:
            return embedder
        wrapped = BatchedEmbedding(
            embedder,
            max_batch=self.cfg.encoder_batch_max,
            max_wait_s=self.cfg.encoder_batch_wait_ms / 1000.0,
        )
        self._embedders.append(wrapped)
        return wrapped


# --------------------------------------------------------------------------- process-wide handle
_ACTIVE: Scheduler | None = None
_REFS = 0


def active() -> Scheduler | None:
    return _ACTIVE


def acquire(cfg: RuntimeConfig) -> Scheduler | None:
    """Engine start: the shared scheduler, or None when both flags are off. Reference counted, so
    several engines in one process share ONE gate (the semaphore is global by design)."""
    global _ACTIVE, _REFS
    if not (cfg.scheduler_v2 or cfg.model_concurrency > 0):
        return None
    if _ACTIVE is None:
        _ACTIVE = Scheduler(cfg)
        _ACTIVE.install()
    _REFS += 1
    return _ACTIVE


async def release(sched: Scheduler | None) -> None:
    """Engine stop: the last release uninstalls the gate."""
    global _ACTIVE, _REFS
    if sched is None or sched is not _ACTIVE:
        return
    _REFS -= 1
    if _REFS <= 0:
        _ACTIVE, _REFS = None, 0
        await sched.uninstall()
