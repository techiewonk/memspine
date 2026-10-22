"""REST protocol factory (D-06, ``[rest]``): the lazy entry point.

Only this function is safe to import without fastapi installed — the actual
app module (``memspine.api.rest.app``) imports fastapi at the top, so it
must never be imported from engine-side code (D-10 guard lives here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memspine.exceptions import MissingServiceError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from memspine.api.engine import Engine
    from memspine.api.process import EngineSpec
    from memspine.config.schema import RestConfig

__all__ = ["create_app", "create_worker_app", "worker_app"]


def create_app(engine: Engine, rest: RestConfig | None = None) -> FastAPI:
    """Wrap ONE engine (D-06) in a FastAPI app.

    The engine's lifecycle is owned by the CALLER: start it before serving,
    stop it after (see ``examples/04``). Raises :class:`MissingServiceError`
    naming the ``rest`` extra when fastapi is not installed (D-10).

    ``rest`` (#51) overrides the engine's ``rest`` config (auth mode, API keys,
    rate limit); None reads it from the engine once it is started.
    """
    try:
        from memspine.api.rest.app import build_app
    except ImportError as exc:
        raise MissingServiceError("api.rest", extra="rest") from exc
    return build_app(engine, rest)


def create_worker_app(spec: EngineSpec, rest: RestConfig | None = None) -> FastAPI:
    """RM-76: an app that OWNS this process's Engine for ``spec``. Its lifespan (run in
    each server worker process) starts and warms the Engine; ``GET /ready`` reports the
    worker's readiness. See :mod:`memspine.api.rest.worker`."""
    try:
        from memspine.api.rest.worker import build_worker_app
    except ImportError as exc:
        raise MissingServiceError("api.rest", extra="rest") from exc
    return build_worker_app(spec, rest)


def worker_app() -> FastAPI:
    """``uvicorn --factory memspine.api.rest:worker_app --workers N``: template and user
    config come from ``MEMSPINE_TEMPLATE`` / ``MEMSPINE_CONFIG``."""
    try:
        from memspine.api.rest.worker import worker_app as factory
    except ImportError as exc:
        raise MissingServiceError("api.rest", extra="rest") from exc
    return factory()
