"""``python -m memspine.api.worker``: the taskiq outbox worker process (RT-01, server mode).

Its start-up hook (RM-76) builds and starts this process's Engine through the per-process
pool (:mod:`memspine.api.process`): the process ResourceRegistry is filled and warmed here,
in the worker process, per ``runtime.shared_resources`` (``warmup: blocking`` fails the
worker start; ``background`` starts consuming while the weights load). It then hands the
Engine to :func:`memspine.runtime.worker.serve` (re-send unfinished rows, consume the outbox
stream). Run one such process per worker; each has its own registry and warm-up. The entry
sits in ``api`` because ``runtime`` may not import the engine facade (import-linter layering).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from memspine.api.process import EngineSpec, process_engine, worker_readiness
from memspine.observability.logging import get_logger
from memspine.runtime.worker import serve

__all__ = ["main"]

ROLE = "taskiq-worker"
_log = get_logger(__name__)


async def _run(template: str | None, config: str | None) -> None:
    async with process_engine(EngineSpec(template, config), role=ROLE) as eng:
        ready = worker_readiness()
        _log.info("runtime.worker_ready", role=ROLE, pid=ready["pid"], ready=ready["ready"])
        await serve(eng)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m memspine.api.worker")
    parser.add_argument("--template", "-t", default=None)
    parser.add_argument("--config", "-c", default=None, help="user config YAML path")
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.template, args.config))


if __name__ == "__main__":  # pragma: no cover
    main()
