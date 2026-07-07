"""TaskRunner seam (D-16): the three runners implement this one protocol.

``inline`` (default, this phase) · ``dbos`` (durable, P3) · ``taskiq``
(Valkey-Streams brokered, P7, with per-scope keys + priority labels, D-42).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from memspine.workers.pipelines import Pipeline, PipelineContext

__all__ = ["TaskRunner"]


@runtime_checkable
class TaskRunner(Protocol):
    def register(self, name: str, pipeline: Pipeline) -> None: ...

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        """Execute one pipeline by name (immediately or via the broker)."""
        ...

    async def close(self) -> None:
        """Flush pending work on shutdown (inline: nothing pending by design)."""
        ...
