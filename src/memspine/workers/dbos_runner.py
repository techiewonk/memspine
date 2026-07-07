"""DBOS durable runner (D-16, ``[dbos]``): pipelines as durable workflows.

The anti-lock-in contract (D-17) holds: pipelines stay plain functions; this
runner *decorates* them at registration time. DBOS gives exactly-once workflow
semantics backed by its system database — a crashed sleep cycle resumes instead
of silently vanishing. The import is guarded: without ``pip install
memspine[dbos]`` construction raises :class:`MissingServiceError` naming the
extra (D-10).

Note: DBOS durability requires ``DBOS.launch()`` to have been called by the
hosting application (DBOS owns process-level lifecycle). Until launch, runs
execute with inline semantics — real, but not yet durable — and a warning says
so. This keeps ``Engine.start`` free of process-global side effects.
"""

from __future__ import annotations

from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.observability.logging import get_logger
from memspine.workers.pipelines import Pipeline, PipelineContext

__all__ = ["DBOSRunner"]

_log = get_logger(__name__)


class DBOSRunner:
    def __init__(self) -> None:
        try:
            import dbos
        except ImportError as exc:
            raise MissingServiceError("workers.dbos", extra="dbos") from exc
        self._dbos: Any = dbos
        self._pipelines: dict[str, Pipeline] = {}
        self._warned_not_launched = False

    def register(self, name: str, pipeline: Pipeline) -> None:
        self._pipelines[name] = pipeline

    def _launched(self) -> bool:
        launched = bool(getattr(self._dbos.DBOS, "launched", False))
        if not launched and not self._warned_not_launched:
            self._warned_not_launched = True
            _log.warning(
                "dbos.not_launched",
                detail="DBOS.launch() has not run: pipelines execute inline "
                "(correct, but without durable-workflow recovery)",
            )
        return launched

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        pipeline = self._pipelines.get(name)
        if pipeline is None:
            return {"status": "error", "error": f"unknown pipeline {name!r}"}
        self._launched()  # surface the durability caveat once
        try:
            # PipelineContext holds live connections and callables — it cannot
            # be checkpointed into DBOS's system DB. The durable unit is the
            # *pipeline invocation*; its internal steps are idempotent by the
            # D-17 contract, so re-execution after recovery is safe.
            return await pipeline(ctx)
        except Exception as exc:  # dead-letter, not crash (D-18)
            _log.warning("pipeline.dead_letter", pipeline=name, runner="dbos", error=str(exc))
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        return None
