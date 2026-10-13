"""memspine: a cognitive memory engine for AI agents.

Public surface (RESTRUCTURE section 1; import stability is promised for ``__all__`` only):

* :func:`open`: ``open(config) -> Engine``, a started engine.
* :class:`Engine`: lifecycle and admin (``engine.scope(...)`` returns a :class:`Memory`).
* :class:`Memory`: the scoped handle that carries the loop verbs.
"""

from __future__ import annotations

from typing import Any

from memspine.api.engine import Engine
from memspine.api.handle import Memory

__version__ = "0.0.1"

__all__ = ["Engine", "Memory", "__version__", "open"]


async def open(
    config: str | Any | None = None,
    *,
    template: str | None = None,
    dotenv_path: Any = ".env",
    **overrides: Any,
) -> Engine:
    """Build and start an :class:`Engine`.

    ``config`` is a user-config path or dict (``Engine(user_config=...)``); ``template`` names a
    base template. The caller owns the result and awaits ``engine.stop()`` (or uses ``async with``).
    """
    engine = Engine(template, config, dotenv_path, **overrides)
    return await engine.start()
