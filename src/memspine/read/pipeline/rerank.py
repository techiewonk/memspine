# ruff: noqa: E501
"""Read path: static prefilter before rerank, moved out of the engine (F-10a)."""

from __future__ import annotations

import math
from typing import Any

from memspine.config import constants
from memspine.exceptions import (
    MissingServiceError,
)
from memspine.models.providers.rerank.base import concat_background, gated_rerank
from memspine.models.providers.rerank.factory import RerankSettings, build_reranker
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.read.pipeline.fusion import _minmax_normalize, _minmax_quiet
from memspine.read.pipeline.state import _FORENSICS, _RAW_SCORES, _RERANK_SUPPORT
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


# --- R3-07: rerank on every read through a licence fallback chain (LIB-42)
# ---------------------------

#: LIB-42 order (Q5/Q6): Jina v3.5, Qwen3-Reranker-0.6B, MiniLM on CPU, then none (unscored).
#: ``read.rerank_v2_chain`` holds the factory modes; this table only documents the model each one
#: stands for.
CHAIN_MODELS: dict[str, str] = {
    "jina": "jina-v3.5",
    "qwen3": "qwen3-reranker-0.6b",
    "fastembed": "minilm",
}


def _planned_chain(eng: Any) -> list[tuple[str, str]] | None:
    """MD-01 (``models.tiers_v2``): the planned reranker chain as ``(mode, device)``, computed once
    per engine; None when the flag is off (the chain is then ``read.rerank_v2_chain``)."""
    cfg_of = getattr(eng, "_config", None)
    models = cfg_of().models if callable(cfg_of) else None
    if models is None or not models.tiers_v2:
        return None
    cached = eng.__dict__.get("_rerank_planned_chain")
    if cached is None:
        from memspine.models.resolve import rerank_chain, resolve_models

        cached = rerank_chain(resolve_models(models))
        eng.__dict__["_rerank_planned_chain"] = cached
        tap.event(
            "models.rerank_chain", modes=[m for m, _ in cached], devices=[d for _, d in cached]
        )
    return cached


def _chain_provider(eng: Any, mode: str, read_cfg: Any, device: str | None = None) -> Any:
    """The reranker of ``mode``, built once per engine; None when it cannot be built (kept, not
    retried). ``device`` is the planned device (MD-01); None keeps ``read.rerank_device``."""
    cache: dict[str, Any] = eng.__dict__.setdefault("_rerank_v2_cache", {})
    if mode in cache:
        return cache[mode]
    built = build_reranker(
        RerankSettings(
            mode=mode,
            instruction=read_cfg.rerank_instruction if mode == "qwen3" else None,
            device=(device or read_cfg.rerank_device) if mode in ("jina", "qwen3") else None,
            quant=read_cfg.rerank_quant if mode == "qwen3" else None,
        )
    )
    if built is not None and hasattr(built, "unscored_tail"):
        built.unscored_tail = True  # the Jina window tail is unscored (None), never 0.0
    cache[mode] = built
    return built


async def chain_score(
    eng: Any, query: str, documents: list[str], read_cfg: Any
) -> tuple[list[float | None], str | None, str | None, list[dict[str, str]]]:
    """Score ``documents`` with the first working reranker of the chain.

    Returns ``(scores, model_id, fallback_used, tried)``. ``scores`` has one entry per document,
    None where the reranker did not score it; with no working reranker every entry is None (``model_id``
    None). ``fallback_used`` names the mode that answered when it is not the first of the chain.
    """
    tried: list[dict[str, str]] = []
    planned = _planned_chain(eng)
    chain = planned if planned is not None else [(m, "") for m in read_cfg.rerank_v2_chain]
    for pos, (mode, device) in enumerate(chain):
        reranker = _chain_provider(eng, mode, read_cfg, device or None)
        if reranker is None:
            tried.append({"mode": mode, "outcome": "unavailable"})
            continue
        try:
            eng._rerank_calls += 1
            raw = list(await gated_rerank(reranker, query, documents))
            if len(raw) != len(documents):
                raise ValueError(f"{len(raw)} scores for {len(documents)} documents")
        except Exception as exc:
            eng._rerank_failures += 1
            _log.warning("rerank.failed", reranker=getattr(reranker, "reranker_id", mode), error=str(exc))
            tried.append({"mode": mode, "outcome": "failed"})
            continue
        scores: list[float | None] = [
            float(s) if s is not None and not isinstance(s, bool) and math.isfinite(s) else None
            for s in raw
        ]
        model_id = getattr(reranker, "reranker_id", mode)
        return scores, model_id, (mode if pos > 0 else None), tried
    return [None] * len(documents), None, None, tried


async def rerank_v2_stage(
    eng: Any,
    ns: str,
    rerank_query: str,
    candidates: list[tuple[MemoryRecord, float]],
    read_cfg: Any,
) -> tuple[list[tuple[MemoryRecord, float]], bool]:
    """R3-07 (``read.rerank_v2``): rerank the first ``rerank_v2_pool`` candidates once.

    Scored candidates take the min-max normalised reranker score (optionally blended with the
    retrieval score by ``rerank_blend``). A candidate the reranker did not score (beyond the pool
    cap, or None from the reranker) is NOT given 0.0: it is recorded as unscored and its relevance
    is its retrieval score scaled below the lowest scored one, so it follows every scored
    candidate. With no working reranker the candidates come back unchanged and ``reranked`` is
    False. Returns ``(candidates, reranked)``.
    """
    head = candidates[: read_cfg.rerank_v2_pool]
    documents = [concat_background(record) for record, _ in head]
    if read_cfg.rerank_date_prefix:
        documents = [
            doc
            if constants.UNDATED_TAG in record.tags
            else f"[Date: {record.valid_from:%Y-%m-%d}] {doc}"
            for (record, _), doc in zip(head, documents, strict=True)
        ]
    if read_cfg.rerank_context:
        documents = await eng._with_session_neighbours(ns, head, documents, read_cfg.rerank_context)
    scores, model_id, fallback, tried = await chain_score(eng, rerank_query, documents, read_cfg)
    scored_idx = [i for i, s in enumerate(scores) if s is not None]
    if tap.enabled() or _FORENSICS.get() is not None:
        scored_ids = [head[i][0].record_id for i in scored_idx]
        unscored_ids = [r.record_id for r, _ in candidates if r.record_id not in set(scored_ids)]
        payload = {
            "scored": len(scored_ids),
            "unscored": unscored_ids,
            "model": model_id,
            "fallback_used": fallback,
            "tried": tried,
            "pool": len(head),
            "pool_cap": read_cfg.rerank_v2_pool,
        }
        tap.event("read.rerank.scored", **payload)
        if (fx := _FORENSICS.get()) is not None:
            fx["rerank_v2"] = payload
    if not scored_idx:
        return candidates, False
    vals = [float(scores[i]) for i in scored_idx]  # type: ignore[arg-type]
    if len(vals) > 1:
        _RERANK_SUPPORT.set(sorted(vals, reverse=True)[1])
    if (raw_sink := _RAW_SCORES.get()) is not None:
        for i in scored_idx:
            rid = head[i][0].record_id
            raw_sink[rid] = max(float(scores[i]), raw_sink.get(rid, float("-inf")))  # type: ignore[arg-type]
    if (fx := _FORENSICS.get()) is not None:
        fx["rerank_scores"] = [(head[i][0].record_id, float(scores[i])) for i in scored_idx]  # type: ignore[arg-type]
    if read_cfg.rerank_gate is not None and max(vals) < read_cfg.rerank_gate:
        eng._rerank_gated += 1  # N41: not confident, keep the retrieval order
        return candidates, False
    rel = _minmax_normalize(vals)
    prior = _minmax_quiet([score for _, score in candidates])
    if read_cfg.rerank_blend is not None:
        w = read_cfg.rerank_blend
        rel = [w * r + (1.0 - w) * prior[i] for r, i in zip(rel, scored_idx, strict=True)]
    floor = min(rel)
    new_rel: dict[int, float] = dict(zip(scored_idx, rel, strict=True))
    out: list[tuple[MemoryRecord, float]] = []
    for i, (record, _) in enumerate(candidates):
        out.append((record, new_rel[i] if i in new_rel else floor * prior[i]))
    return out, True
