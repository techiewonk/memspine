"""REST app for separate server worker processes (RM-76, ``[rest]``).

``create_app`` leaves the Engine's lifecycle to the caller. A worker app owns it instead:
its ASGI lifespan is the per-process start-up hook. Each server worker process (for example
``uvicorn --factory memspine.api.rest:worker_app --workers 4``, whose workers are spawned
fresh) imports the app, and its lifespan starts this process's Engine through the
per-process pool, so the ResourceRegistry is built and warmed in the worker per
``runtime.shared_resources``. ``GET /ready`` reports the worker's readiness (pid, Engine
start state, warm state per resource; 503 until the Engine is started).

With a pre-forking server that imports the app in the parent (``gunicorn --preload``), the
constructed Engine is not started there and is started in each child. An Engine that was
started before the fork is refused in the child (its handles are the parent's).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from memspine.api.process import EngineSpec, process_engine, process_engines, worker_readiness
from memspine.api.rest.app import ORJSONResponse, build_app
from memspine.config.schema import RestConfig

__all__ = ["ROLE", "build_worker_app", "worker_app"]

ROLE = "rest-worker"
#: Environment variables read by the zero-argument :func:`worker_app` factory.
TEMPLATE_ENV = "MEMSPINE_TEMPLATE"
CONFIG_ENV = "MEMSPINE_CONFIG"


def build_worker_app(spec: EngineSpec, rest: RestConfig | None = None) -> FastAPI:
    """The REST app whose lifespan starts and stops this process's Engine for ``spec``."""
    pool = process_engines()
    engine = pool.engine(spec)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if pool.engine(spec) is not engine:
            raise RuntimeError(
                "the app's Engine was started before this worker process forked; build the "
                "app in the worker (uvicorn --factory) or do not start it in the parent"
            )
        async with process_engine(spec, role=ROLE):
            yield

    app = build_app(engine, rest, lifespan=lifespan)

    @app.get("/ready")
    async def ready() -> Response:
        report = worker_readiness()
        return ORJSONResponse(status_code=200 if report["ready"] else 503, content=report)

    return app


def worker_app() -> FastAPI:
    """Zero-argument factory for ``uvicorn --factory``: the template and user config file
    come from ``MEMSPINE_TEMPLATE`` / ``MEMSPINE_CONFIG`` (both optional)."""
    return build_worker_app(
        EngineSpec(os.environ.get(TEMPLATE_ENV) or None, os.environ.get(CONFIG_ENV) or None)
    )
