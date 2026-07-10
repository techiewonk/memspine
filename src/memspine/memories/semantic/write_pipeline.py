"""C3: optional synchronous graphiti-style write pipeline (ADR-026).

The v0.1 semantic write is single-pass (``write_pipeline: single``). Opting into
``write_pipeline: graph`` runs, at write time, an extra relationship-extraction
pass over the same content: each extracted edge ``(src, rel, dst, fact)`` is
written as a fact-keyed semantic record ``(entity=src, attribute=rel)`` — through
the *same* ``_write_locked`` path, so it gets dedup (M5) and the conflict ladder
(M4) for free. That is exactly the "invalidate_edges" step: a new edge that
supersedes an existing one climbs the ladder and archives it, using the ``judge``
LLM when configured, no separate mechanism needed.

Edge records carry ``channel="write_pipeline"`` provenance; the memory guards on
it so an edge record never re-triggers the pipeline (bounded, depth-1 recursion).
E1: a derived edge fact never out-trusts its source and keeps injection framing.

The whole stage is off unless the engine injects a pipeline (policy ``graph`` +
an ``extract_edges`` LLM role), so ``profile="simple"`` is byte-identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from memspine.core.firewall import instruction_shaped
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.observability.logging import get_logger
from memspine.prompts.models import ExtractedEdge

__all__ = ["EDGE_CHANNEL", "ExtractEdges", "GraphWritePipeline", "ResolveEntity", "WritePipeline"]

_log = get_logger(__name__)

#: Provenance channel stamped on records the pipeline writes; the memory skips
#: the pipeline for these so an edge record never recurses into more extraction.
EDGE_CHANNEL = "write_pipeline"

#: LLM edge extraction: source text -> reflexion-merged relationship edges.
ExtractEdges = Callable[[str], Awaitable[list[ExtractedEdge]]]
#: Optional entity canonicalization: a mention -> its canonical name.
ResolveEntity = Callable[[str], Awaitable[str]]
#: The memory's own write-a-fact entry point (``SemanticMemory._write_locked``).
WriteFact = Callable[[MemoryRecord], Awaitable[object]]


class WritePipeline(Protocol):
    async def run(self, record: MemoryRecord, write_fact: WriteFact) -> int:
        """Emit derived edge facts for ``record``; returns how many were written."""
        ...


class GraphWritePipeline:
    """extract_edges → (resolve_entity) → write-through-the-ladder (C3)."""

    def __init__(
        self,
        extract_edges: ExtractEdges,
        resolve_entity: ResolveEntity | None = None,
        min_confidence: float = 0.0,
    ) -> None:
        self._extract_edges = extract_edges
        self._resolve_entity = resolve_entity
        self._min_confidence = min_confidence

    async def run(self, record: MemoryRecord, write_fact: WriteFact) -> int:
        try:
            edges = await self._extract_edges(record.content)
        except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
            _log.warning(
                "write_pipeline.extract_failed", record_id=record.record_id, error=str(exc)
            )
            return 0
        written = 0
        for edge in edges:
            if edge.confidence < self._min_confidence:
                continue
            entity = edge.src_entity
            if self._resolve_entity is not None:
                try:
                    entity = await self._resolve_entity(edge.src_entity) or edge.src_entity
                except Exception as exc:  # canonicalization is best-effort (N6)
                    _log.warning("write_pipeline.resolve_failed", error=str(exc))
            fact = MemoryRecord(
                namespace=record.namespace,
                memory_type="semantic",
                content=edge.fact,
                entity=entity,
                attribute=edge.rel,
                # E1/D-47 §5: derived trust never exceeds the source; echoed
                # injection framing stays flagged.
                trust=record.trust,
                instruction_flag=instruction_shaped(edge.fact),
                source=SourceInfo(role="system", channel=EDGE_CHANNEL, message_id=record.record_id),
            )
            await write_fact(fact)  # full M5 dedup + M4 ladder (invalidate_edges)
            written += 1
        return written
