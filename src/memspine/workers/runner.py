"""TaskRunner seam: all background runners implement this one protocol.

``inline`` runs locally, ``temporal`` runs durable workflows with an embedded
Resonate step journal, and ``taskiq`` dispatches over Redis/Valkey Streams.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from memspine.derive.pipeline.stages.context import Pipeline, PipelineContext

__all__ = ["TaskRunner"]


@runtime_checkable
class TaskRunner(Protocol):
    def register(self, name: str, pipeline: Pipeline) -> None: ...

    async def start(self) -> None:
        """Initialize remote workers after every pipeline has been registered."""
        ...

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        """Execute one pipeline by name (immediately or via the broker)."""
        ...

    async def close(self) -> None:
        """Flush pending work on shutdown (inline: nothing pending by design)."""
        ...
