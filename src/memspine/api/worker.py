"""``python -m memspine.api.worker``: the taskiq outbox worker process (RT-01, server mode).

Opens the engine on the configured store, then hands it to :func:`memspine.runtime.worker.serve`
(re-send unfinished rows, consume the outbox stream). The entry sits in ``api`` because ``runtime``
may not import the engine facade (import-linter layering).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from memspine.api.engine import Engine
from memspine.runtime.worker import serve

__all__ = ["main"]


async def _run(template: str | None, config: str | None) -> None:
    eng = Engine(template=template, user_config=config)
    await eng.start()
    try:
        await serve(eng)
    finally:
        await eng.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m memspine.api.worker")
    parser.add_argument("--template", "-t", default=None)
    parser.add_argument("--config", "-c", default=None, help="user config YAML path")
    args = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(args.template, args.config))


if __name__ == "__main__":  # pragma: no cover
    main()
