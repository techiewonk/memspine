"""Taskiq transport over the durable outbox (RT-01; G-19, W-13).

Owner note: "outbox = truth, taskiq = transport".

A taskiq message carries ONE thing: an outbox ``job_id``. The row, its items, its lease and its
watermark stay in the outbox (``runtime.outbox``); the message only wakes a drainer. So a lost,
duplicated or reordered message cannot lose or repeat work: the handler looks the job up, and the
lease epoch and the contiguous watermark (``runtime.jobs``) are what make a commit happen once.

``TransportRunner`` is an ``InlineRunner`` that, after persisting an item, also sends the job id:

- ``workers.transport_broker = "memory"`` (library mode): taskiq's ``InMemoryBroker`` runs the task
  in this process, so the library still drains in-process. The inline drains the host already calls
  (after a write, at flush, at the read barrier) stay on, and race safely with the task through
  the job lease.
- ``workers.transport_broker = "redis"`` (server mode): a ``RedisStreamBroker`` on
  ``workers.broker_url``. The producing process only enqueues and kicks (``local_drain`` is off, so
  its own ``drain`` is a no-op and the read barrier polls); ``python -m memspine.runtime.worker``
  opens the same store, registers the same stage handlers and consumes the stream.

On ``start()`` every unfinished row is sent again (``resend_unfinished``): jobs still queued, and
jobs whose lease ran out (a crashed worker's, reclaimed through the book first). A failed send is
logged and tapped, never raised: the row is durable and the next start re-sends it.

Tap events: ``runtime.transport.{kick,kick_failed,receive,skip,resend,start,stop}``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal

from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.runtime.jobs import Clock, JobStore, RetryBudget
from memspine.runtime.runner import DrainReport, InlineRunner, NamespaceLock, Sleeper, utc_now

__all__ = [
    "DEFAULT_QUEUE",
    "TASK_NAME",
    "TransportRunner",
    "make_broker",
]

_log = get_logger(__name__)

TASK_NAME = "memspine.outbox.drain"
DEFAULT_QUEUE = "memspine-outbox"
BrokerKind = Literal["memory", "redis"]


def make_broker(
    kind: BrokerKind, url: str | None = None, *, queue_name: str = DEFAULT_QUEUE
) -> Any:
    """The taskiq broker for ``kind``. ``redis`` needs the ``[taskiq]`` extra (``taskiq-redis``) and
    builds a client lazily: no connection is opened until the first send or the worker's listen."""
    if kind == "memory":
        from taskiq import InMemoryBroker

        return InMemoryBroker()
    if kind == "redis":
        try:
            from taskiq_redis import RedisStreamBroker
        except ImportError as exc:  # pragma: no cover - depends on the extra
            from memspine.exceptions import MissingServiceError

            raise MissingServiceError("workers.transport", extra="taskiq") from exc
        return RedisStreamBroker(url or "redis://localhost:6379/0", queue_name=queue_name)
    from memspine.exceptions import ConfigError

    raise ConfigError(f"unknown workers.transport_broker {kind!r} (valid: memory, redis)")


class TransportRunner(InlineRunner):
    """``InlineRunner`` whose enqueue also sends the job id over a taskiq broker."""

    def __init__(
        self,
        store: JobStore,
        *,
        broker: Any,
        local_drain: bool = True,
        clock: Clock = utc_now,
        owner: str = "inline",
        lease_ttl: timedelta = timedelta(minutes=5),
        budget: RetryBudget | None = None,
        commit_lock: NamespaceLock | None = None,
        sleeper: Sleeper | None = None,
        poll_s: float = 0.02,
    ) -> None:
        super().__init__(
            store,
            clock=clock,
            owner=owner,
            lease_ttl=lease_ttl,
            budget=budget,
            commit_lock=commit_lock,
            sleeper=sleeper,
            poll_s=poll_s,
        )
        self.broker = broker
        self.local_drain = local_drain
        self._broker_started = False
        self._kick_failures = 0
        self._task: Any = broker.register_task(self._make_handler(), task_name=TASK_NAME)

    # ------------------------------------------------------------------ the task
    def _make_handler(self) -> Callable[[str], Awaitable[dict[str, Any]]]:
        runner = self

        async def drain_outbox_job(job_id: str) -> dict[str, Any]:
            return await runner.handle(job_id)

        return drain_outbox_job

    async def handle(self, job_id: str) -> dict[str, Any]:
        """What a received message does: look the row up and drain its scope. A job that is gone,
        finished, coalesced away or leased to someone else is skipped (the row is the truth)."""
        job = self.store.get_job(job_id)
        if job is None or job.state != "queued":
            tap.event(
                "runtime.transport.skip",
                job_id=job_id,
                reason="unknown" if job is None else f"state_{job.state}",
            )
            return {"job_id": job_id, "drained": False}
        tap.event("runtime.transport.receive", job_id=job_id, stage=job.stage, epoch=job.epoch)
        report = await InlineRunner.drain(self, job.scope)
        return {"job_id": job_id, "drained": True, "items": report.items, "jobs": report.jobs}

    # ------------------------------------------------------------------ sending
    async def start_broker(self) -> None:
        """Client-side startup of the broker (idempotent)."""
        if not self._broker_started:
            self._broker_started = True
            await self.broker.startup()

    async def kick(self, job_id: str) -> bool:
        """Send one job id. Returns False (never raises) when the broker refused it."""
        try:
            await self.start_broker()
            await self._task.kiq(job_id)
        except Exception as exc:  # broker down: the row is durable, the next start re-sends
            self._kick_failures += 1
            tap.event("runtime.transport.kick_failed", job_id=job_id, error=type(exc).__name__)
            _log.warning("transport.kick_failed", job_id=job_id, error=str(exc)[:200])
            return False
        tap.event("runtime.transport.kick", job_id=job_id)
        return True

    async def enqueue(self, scope: Any, stage: str, payload: dict[str, Any]) -> Any:
        handle = await super().enqueue(scope, stage, payload)
        # a coalesced handle names the job that already covers the offset: wake that one
        await self.kick(handle.coalesced_into or handle.job_id)
        return handle

    async def resend_unfinished(self) -> int:
        """Send every unfinished row again: queued jobs, and jobs whose lease expired (reclaimed
        through the book first, which spends an attempt as a lost lease always does)."""
        await self._io(self.book.expire_leases)
        sent = 0
        for job in self.store.jobs():
            if job.state != "queued":
                continue
            tap.event("runtime.transport.resend", job_id=job.job_id, stage=job.stage)
            if await self.kick(job.job_id):
                sent += 1
        return sent

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> DrainReport:
        self._stopping = False
        sent = await self.resend_unfinished()
        tap.event("runtime.transport.start", resent=sent, local_drain=self.local_drain)
        if not self.local_drain:
            return DrainReport()
        return await InlineRunner.drain(self)

    async def drain(self, scope: Any = None) -> DrainReport:
        """Producer-only runners (server mode) leave draining to the workers."""
        if not self.local_drain:
            return DrainReport()
        return await InlineRunner.drain(self, scope)

    async def stop(self, deadline_s: float = 5.0) -> DrainReport:
        report = await super().stop(deadline_s)
        wait_all = getattr(self.broker, "wait_all", None)  # InMemoryBroker: finish in-flight tasks
        if wait_all is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(wait_all(), timeout=max(deadline_s, 0.1))
        tap.event("runtime.transport.stop", released=report.released)
        return report

    @property
    def kick_failures(self) -> int:
        return self._kick_failures
