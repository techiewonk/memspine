"""DBOS runner seam (D-16, ``[dbos]``).

HONESTY NOTE (P3): this runner is the *seam*, not yet the durability. Pipelines
execute with inline semantics plus dead-letter reporting; wrapping each run in
a checkpointed ``@DBOS.workflow`` (so a crashed sleep cycle *resumes*) requires
the hosting application to own ``DBOS.launch()`` and workflow registration at
import time — that integration lands in P7 alongside the taskiq runner. Until
then the runner warns when DBOS is not launched and never claims recovery it
cannot deliver. The import is guarded: without ``pip install memspine[dbos]``
construction raises :class:`MissingServiceError` naming the extra (D-10).
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
            _log.warning(
                "pipeline.dead_letter",
                pipeline=name,
                runner="dbos",
                error=str(exc),
                exc_info=True,
            )
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        return None
