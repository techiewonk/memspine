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
N2: edge facts are LLM-authored, so they carry the non-privileged
``DERIVED_ROLE`` and name their source as a parent; the memory screens each one
through the engine's firewall before the ladder. An instruction-flagged source
yields no edges.

GP-1: an edge carries ``kind``. A ``state`` edge (lives_in, works_at) keeps the
``(entity=src, attribute=rel)`` key and supersedes through the ladder; an ``event``
edge (read, visited) drops the attribute, so the ladder ADDs it beside the
person's other events instead of archiving them (the G1a rule, applied to edges).
A protected ``src.rel`` key keeps its attribute whatever the extractor called it.
Every edge fact is tagged ``kind:<kind>``, ``rel:<rel>`` and ``dst:<dst_entity>``,
so the destination entity survives on the record.

#32 (GR-16): ``max_rounds`` > 1 re-asks the extractor and merges the rounds (the
reflexion pass). ``memories.semantic.policies.write.reflexion: false`` drops those
extra rounds (one call per source), for the ablation Graphiti ran when it removed
reflexion; the default ``true`` keeps today's behaviour. See :func:`extraction_rounds`.

The whole stage is off unless the engine injects a pipeline (policy ``graph`` +
an ``extract_edges`` LLM role), so ``profile="simple"`` is byte-identical.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from memspine.config.constants import DERIVED_ROLE
from memspine.core.firewall import instruction_shaped
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.observability.logging import get_logger
from memspine.prompts.models import ExtractedEdge

__all__ = [
    "EDGE_CHANNEL",
    "EdgeContext",
    "ExtractEdges",
    "GraphWritePipeline",
    "ResolveEntity",
    "ScreenDerived",
    "SemanticWriteOptions",
    "WritePipeline",
    "edge_fact_key",
    "extraction_rounds",
]

_log = get_logger(__name__)

#: Provenance channel stamped on records the pipeline writes; the memory skips
#: the pipeline for these so an edge record never recurses into more extraction.
EDGE_CHANNEL = "write_pipeline"

#: GR-4: at most this many earlier episodes ride along as extraction context.
MAX_PREVIOUS_EPISODES = 10


@dataclass(frozen=True)
class EdgeContext:
    """GR-4: what the edge extractor sees besides the content itself.

    ``reference_time`` anchors relative dates ("yesterday", "last week");
    ``previous`` holds up to :data:`MAX_PREVIOUS_EPISODES` earlier episodes
    (context for pronouns, never a source of edges); ``entities`` lists names
    already known in the namespace so the extractor reuses them verbatim.
    """

    reference_time: datetime | None = None
    previous: Sequence[str] = ()
    entities: Sequence[str] = ()


class SemanticWriteOptions(BaseModel):
    """``memories.semantic.policies.write``: semantic write-path switches."""

    #: #32: run the reflexion rounds (``extract_graph.max_rounds`` > 1). ``false``
    #: makes every edge extraction a single call.
    reflexion: bool = True


def extraction_rounds(policies: dict[str, Any]) -> int:
    """#32: the edge-extraction calls per source for a semantic policy map.

    ``extract_graph.max_rounds`` (default 1, at least 1), or 1 when
    ``write.reflexion`` is false.
    """
    write = policies.get("write")
    options = SemanticWriteOptions.model_validate(write if isinstance(write, dict) else {})
    graph = policies.get("extract_graph")
    rounds = int(graph.get("max_rounds", 1)) if isinstance(graph, dict) else 1
    return max(1, rounds) if options.reflexion else 1


class ExtractEdges(Protocol):
    """LLM edge extraction: source text (+ optional context) -> reflexion-merged edges."""

    def __call__(
        self, content: str, context: EdgeContext | None = None, /
    ) -> Awaitable[list[ExtractedEdge]]: ...


def edge_fact_key(
    edge: ExtractedEdge, entity: str, protected_keys: Collection[str] = ()
) -> tuple[str | None, list[str]]:
    """GP-1: the ``(attribute, tags)`` an edge fact record is written with.

    A ``state`` edge keeps ``attribute=rel`` so a newer ``(src, rel)`` edge
    supersedes it. An ``event`` edge drops the attribute so it is never
    superseded, unless ``entity.rel`` is a protected key (an extractor calling
    it an event must not dodge the protected-key check). The tags persist the
    kind, the relation and the destination entity on the record.
    """
    tags = [f"kind:{edge.kind}", f"rel:{edge.rel}", f"dst:{edge.dst_entity}"]
    if edge.kind == "event" and f"{entity}.{edge.rel}" not in protected_keys:
        return None, tags
    return edge.rel, tags


#: Optional entity canonicalization: a mention -> its canonical name.
ResolveEntity = Callable[[str], Awaitable[str]]
#: The memory's own write-a-fact entry point (``SemanticMemory._write_locked``).
WriteFact = Callable[[MemoryRecord], Awaitable[object]]
#: N2: the engine's firewall for derived records written outside its door:
#: (record, parent trust cap) -> (stamped record, quarantine reasons or []).
ScreenDerived = Callable[[MemoryRecord, list[float]], Awaitable[tuple[MemoryRecord, list[str]]]]


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
        protected_keys: Collection[str] = (),
    ) -> None:
        self._extract_edges = extract_edges
        self._resolve_entity = resolve_entity
        self._min_confidence = min_confidence
        self._protected_keys = frozenset(protected_keys)

    async def run(self, record: MemoryRecord, write_fact: WriteFact) -> int:
        if record.instruction_flag or record.quarantined:
            return 0  # N2: held or instruction-shaped content is not a fact source
        try:
            # GR-4: the write-time pass has the record's own event time and its
            # fact key; earlier episodes are the sleep-time (C2) pass's context.
            context = EdgeContext(
                reference_time=record.valid_from,
                entities=[record.entity] if record.entity else [],
            )
            edges = await self._extract_edges(record.content, context)
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
            attribute, tags = edge_fact_key(edge, entity, self._protected_keys)
            fact = MemoryRecord(
                namespace=record.namespace,
                memory_type="semantic",
                content=edge.fact,
                entity=entity,
                attribute=attribute,
                tags=tags,
                # E1/D-47 §5: derived trust never exceeds the source; echoed
                # injection framing stays flagged.
                trust=record.trust,
                instruction_flag=instruction_shaped(edge.fact),
                source=SourceInfo(
                    role=DERIVED_ROLE,
                    channel=EDGE_CHANNEL,
                    message_id=record.record_id,
                    parents=[record.record_id],
                ),
            )
            await write_fact(fact)  # full M5 dedup + M4 ladder (invalidate_edges)
            written += 1
        return written
