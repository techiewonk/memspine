"""Inline runner (D-16 default): pipelines execute immediately, in-process.

Zero infrastructure — the runner every core install boots with. Failures are
logged with D-18 severity (consolidation-class = warning; the M7 hard-delete
cascade escalates to alert in Phase 4) and returned as an error stat rather
than raised, so one failing background pipeline never kills the engine.
"""

from __future__ import annotations

from memspine.observability.logging import get_logger
from memspine.workers.pipelines import Pipeline, PipelineContext

__all__ = ["InlineRunner"]

_log = get_logger(__name__)


class InlineRunner:
    def __init__(self) -> None:
        self._pipelines: dict[str, Pipeline] = {}

    def register(self, name: str, pipeline: Pipeline) -> None:
        self._pipelines[name] = pipeline

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        pipeline = self._pipelines.get(name)
        if pipeline is None:
            return {"status": "error", "error": f"unknown pipeline {name!r}"}
        try:
            return await pipeline(ctx)
        except Exception as exc:  # dead-letter, not crash (D-18)
            _log.warning("pipeline.dead_letter", pipeline=name, error=str(exc))
            return {"status": "error", "error": str(exc)}

    async def close(self) -> None:
        return None  # inline work is always already flushed
