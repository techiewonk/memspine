"""Worker entry point for the taskiq transport (RT-01, server mode).

``python -m memspine.api.worker [--template T] [--config file.yaml]`` (the entry lives in ``api``,
since ``runtime`` may not import the engine facade) opens the engine on the
configured store (so the window handlers are registered on the outbox runner), re-sends every
unfinished row, then consumes the outbox stream until interrupted. Needs
``workers.outbox=true``, ``workers.transport="taskiq"`` and ``workers.transport_broker="redis"``.
Several workers may share one store: the job lease and its epoch keep a commit single.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from memspine.exceptions import ConfigError
from memspine.observability import bindings as _bindings
from memspine.observability import tap
from memspine.runtime.transport import TransportRunner

__all__ = ["serve", "transport_runner"]


def transport_runner(eng: Any) -> TransportRunner:
    """The engine's outbox runner, which must be the taskiq one."""
    pipeline = getattr(eng, "_windows", None)
    runner = getattr(pipeline, "_outbox", None)
    if not isinstance(runner, TransportRunner):
        raise ConfigError(
            "the worker needs workers.outbox=true and workers.transport='taskiq' "
            "(no transport runner on this engine)"
        )
    return runner


async def serve(eng: Any, finish_event: asyncio.Event | None = None) -> None:
    """Resend unfinished rows, then listen on the broker until ``finish_event`` is set."""
    from taskiq.receiver import Receiver

    binding = getattr(eng, "_binding", None)
    if binding is not None:  # RM-75: the receiver's tasks inherit this Engine's binding
        with _bindings.bound(binding):
            await _serve(eng, finish_event, Receiver)
        return
    await _serve(eng, finish_event, Receiver)


async def _serve(eng: Any, finish_event: asyncio.Event | None, receiver_cls: Any) -> None:
    runner = transport_runner(eng)
    finish = finish_event or asyncio.Event()
    await runner.start_broker()
    resent = await runner.resend_unfinished()
    tap.event("runtime.transport.worker", phase="listen", resent=resent)
    receiver = receiver_cls(
        runner.broker,
        executor=ThreadPoolExecutor(max_workers=1),
        run_startup=False,
        max_async_tasks=4,
    )
    try:
        await receiver.listen(finish)
    finally:
        tap.event("runtime.transport.worker", phase="stopped")
