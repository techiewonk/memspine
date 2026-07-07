"""Rerank port (E8, opt-in): cross-encoder scores over (query, document) pairs.

Adapters: ``fastembed_rerank`` (ONNX ``TextCrossEncoder``, rides the core
fastembed dependency when the installed version ships rerankers) and
``flashrank_rerank`` (``[rerank]`` extra). The engine treats an unavailable
adapter as a skipped stage (one info log), never a failed retrieval.

``concat_background`` is the D-42 §5 strategy-rerank: the text a cross-encoder
scores is the record content *prefixed with its structural context* (entity /
attribute / source), so a bare pronoun-heavy chunk still ranks on what it is
about.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from memspine.core.records import MemoryRecord

__all__ = ["Reranker", "concat_background"]


@runtime_checkable
class Reranker(Protocol):
    reranker_id: str

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """One relevance score per document, same order as ``documents``."""
        ...


def concat_background(record: MemoryRecord) -> str:
    """The strategy-rerank document text (D-42 §5, ``concat_background``)."""
    parts: list[str] = [f"type: {record.memory_type}"]
    if record.entity:
        parts.append(f"entity: {record.entity}")
    if record.attribute:
        parts.append(f"attribute: {record.attribute}")
    if record.source.channel != "internal":
        parts.append(f"channel: {record.source.channel}")
    if record.source.doc_path:
        parts.append(f"doc: {record.source.doc_path}")
    return "[" + " | ".join(parts) + "]\n" + record.content
