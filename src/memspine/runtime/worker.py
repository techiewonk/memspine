"""Worker entry point for the taskiq transport (RT-01, server mode).

``python -m memspine.runtime.worker [--template T] [--config file.yaml]`` opens the engine on the
configured store (so the window handlers are registered on the outbox runner), re-sends every
unfinished row, then consumes the outbox stream until interrupted. Needs
``workers.outbox=true``, ``workers.transport="taskiq"`` and ``workers.transport_broker="redis"``.
Several workers may share one store: the job lease and its epoch keep a commit single.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from memspine.exceptions import ConfigError
from memspine.observability import tap
from memspine.runtime.transport import TransportRunner

__all__ = ["main", "serve", "transport_runner"]


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

    runner = transport_runner(eng)
    finish = finish_event or asyncio.Event()
    await runner.start_broker()
    resent = await runner.resend_unfinished()
    tap.event("runtime.transport.worker", phase="listen", resent=resent)
    receiver = Receiver(
        runner.broker,
        executor=ThreadPoolExecutor(max_workers=1),
        run_startup=False,
        max_async_tasks=4,
    )
    try:
        await receiver.listen(finish)
    finally:
        tap.event("runtime.transport.worker", phase="stopped")


async def _run(template: str | None, config: str | None) -> None:
    # the process entry point builds the public Engine (api sits above runtime), so it is loaded
    # by name: a static import here would make runtime import api
    Engine = importlib.import_module("memspine").Engine
    eng = Engine(template=template, user_config=config)
    await eng.start()
    try:
        await serve(eng)
    finally:
        await eng.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m memspine.runtime.worker")
    parser.add_argument("--template", "-t", default=None)
    parser.add_argument("--config", "-c", default=None, help="user config YAML path")
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.template, args.config))


if __name__ == "__main__":  # pragma: no cover
    main()
