"""Read path: static prefilter before rerank, moved out of the engine (F-10a)."""

from __future__ import annotations

from typing import Any

from memspine.config import constants
from memspine.exceptions import (
    MissingServiceError,
)
from memspine.observability.logging import get_logger
from memspine.store.records import (
    MemoryRecord,
)

_log = get_logger(__name__)


def _static_prefilter(
    query: str, candidates: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """E8 optional first stage (opt-in): a cheap lexical-overlap gate over the
    candidate set. Applied *after* the vector leg (a true pre-vector
    static-embedding prefilter is the E4 model2vec track); when nothing
    overlaps it keeps the original set — a precision stage must never turn a
    working retrieval into an empty one."""
    query_tokens = set(query.lower().split())
    kept = [
        (record, relevance)
        for record, relevance in candidates
        if query_tokens & set(record.content.lower().split())
    ]
    return kept or candidates


async def _static_embedding_prefilter(
    eng: Any, query: str, candidates: list[tuple[MemoryRecord, float]], top_k: int
) -> list[tuple[MemoryRecord, float]]:
    """E4 model2vec gate (ADR-020): rank the candidates by cheap static
    cosine and keep the strongest ``top_k * STATIC_PREFILTER_KEEP_MULTIPLIER``
    before the expensive rerank/score. The embedder loads lazily.

    Two failure modes, deliberately different: a genuinely absent ``[static]``
    extra (MissingServiceError/ImportError at construction) STICKY-disables
    the stage for the process — it can never work, so stop trying. A transient
    embed/count error (weight download, a model2vec vector-count mismatch)
    skips the prefilter for THIS search ONLY and is NOT sticky, so a later
    search retries. Either way retrieval degrades to the unfiltered set."""
    if eng._static_embedder is None:
        try:
            from memspine.models.encoders.embedding.static_local import StaticEmbedder

            eng._static_embedder = StaticEmbedder()
        except (MissingServiceError, ImportError) as exc:
            # The extra is genuinely absent — disable permanently.
            eng._static_unavailable = True
            _log.info(
                "static_prefilter.unavailable",
                detail=f"E4 static-embedding prefilter disabled — {exc}",
            )
            return candidates
    keep = max(top_k * constants.STATIC_PREFILTER_KEEP_MULTIPLIER, top_k)
    if len(candidates) <= keep:
        return candidates  # nothing to narrow — skip the embed cost entirely
    try:
        vectors = await eng._static_embedder.embed(
            [query, *(record.content for record, _ in candidates)]
        )
        # Unpack + zip INSIDE the try: a model2vec count mismatch (vectors not
        # 1 + len(candidates)) must degrade to the unfiltered set, not raise.
        query_vec, doc_vecs = vectors[0], vectors[1:]
        scored = sorted(
            zip(candidates, doc_vecs, strict=True),
            key=lambda pair: sum(a * b for a, b in zip(query_vec, pair[1], strict=True)),
            reverse=True,
        )
    except Exception as exc:
        # Transient (OSError weight download, RuntimeError, a length mismatch):
        # skip THIS search only — not sticky, so a later search tries again.
        _log.warning(
            "static_prefilter.skipped",
            detail=f"E4 static-embedding prefilter skipped this search — {exc}",
        )
        return candidates
    return [candidate for candidate, _ in scored[:keep]]
