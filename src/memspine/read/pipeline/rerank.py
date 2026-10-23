"""Read path: static prefilter before rerank, moved out of the engine (F-10a)."""

from __future__ import annotations

import math
from typing import Any

from memspine.config import constants
from memspine.exceptions import MissingServiceError
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.providers.rerank import RerankSettings, build_reranker
from memspine.read.pipeline.fusion import _minmax_normalize, _minmax_quiet
from memspine.read.pipeline.state import _FORENSICS, _RAW_SCORES, _RERANK_SUPPORT
from memspine.services.rerank.base import concat_background, gated_rerank
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
            from memspine.services.embedding.static import StaticEmbedder

            resources = getattr(eng, "_provider_resources", None)
            if resources is None:
                eng._static_embedder = StaticEmbedder()
            else:
                # RM-73: the same registry key as a configured static embedder of the same
                # model, so the prefilter never loads a second copy; this Engine's lifecycle
                # releases its lease.
                from memspine.providers.lifecycle import ResourceOwnershipError
                from memspine.providers.rerank import static_prefilter_embedder
                from memspine.providers.shared import shared_registry

                registry = getattr(eng, "_resource_registry", None) or shared_registry()
                embedder = static_prefilter_embedder(registry)
                try:
                    resources.own("embedding.weights:static-prefilter", embedder.aclose)
                except ResourceOwnershipError:
                    return candidates  # the Engine is stopping: no new weight lease
                eng._static_embedder = embedder
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


# --- ADR-082 / R3-07: rerank through the ``model.rerank.chain`` fallback chain (LIB-42)
# ---------------------------

#: LIB-42 order (Q5/Q6): Jina v3.5, Qwen3-Reranker-0.6B, MiniLM on CPU, then none (unscored).
#: The tier plan (``model.placement.tiers_v2``) names these providers; this table only documents
#: the model each one stands for.
CHAIN_MODELS: dict[str, str] = {
    "jina": "jina-v3.5",
    "qwen3": "qwen3-reranker-0.6b",
    "fastembed": "minilm",
}

#: Providers whose device the tier plan may set (the torch rerankers).
_PLANNED_DEVICE = frozenset({"jina", "qwen3"})


def stage_settings(stage: Any, rerank_cfg: Any, device: str | None = None) -> RerankSettings:
    """The :class:`RerankSettings` of one ``model.rerank.chain`` entry (its own ``settings``,
    the chain's ``chunk_chars`` / ``chunk_overlap``; ``device`` is a planned device, MD-01)."""
    options = dict(stage.settings)
    planned = device if device and stage.provider in _PLANNED_DEVICE else None
    return RerankSettings(
        mode=stage.provider,
        model=options.get("model"),
        api_base=options.get("api_base"),
        api_key=options.get("api_key"),
        aws_region=options.get("aws_region"),
        request_timeout_s=options.get("request_timeout_s"),
        instruction=options.get("instruction"),
        device=planned or options.get("device"),
        quant=options.get("quant"),
        batch_size=options.get("batch_size"),
        max_length=options.get("max_length"),
        max_documents=options.get("max_documents"),
        chunk_chars=rerank_cfg.chunk_chars,
        chunk_overlap=rerank_cfg.chunk_overlap,
    )


def _planned_chain(eng: Any) -> list[tuple[Any, str]] | None:
    """MD-01 (``model.placement.tiers_v2``): the planned chain as ``(stage, device)``, computed
    once per engine; None when the flag is off (the chain is then ``model.rerank.chain``).

    A planned provider takes the settings of the first configured entry of that provider (none:
    empty settings); configured entries the plan does not name follow, in configured order."""
    cfg_of = getattr(eng, "_config", None)
    cfg = cfg_of() if callable(cfg_of) else None
    if cfg is None or not cfg.model.placement.tiers_v2:
        return None
    cached = eng.__dict__.get("_rerank_planned_chain")
    if cached is None:
        from memspine.config.schema.model import RerankStage
        from memspine.models.resolve import embedder_preference, rerank_chain, resolve_models

        configured = list(cfg.model.rerank.chain)
        planned = rerank_chain(
            resolve_models(
                cfg.model.placement,
                rerank=bool(configured),
                embedder_profile=embedder_preference(cfg.model.embedding.preset),
            )
        )
        by_provider: dict[str, Any] = {}
        for stage in configured:
            by_provider.setdefault(stage.provider, stage)
        cached = [
            (by_provider.get(mode) or RerankStage(provider=mode), device)
            for mode, device in planned
        ]
        named = {mode for mode, _ in planned}
        cached.extend((stage, "") for stage in configured if stage.provider not in named)
        eng.__dict__["_rerank_planned_chain"] = cached
        tap.event(
            "models.rerank_chain",
            modes=[s.provider for s, _ in cached],
            devices=[d for _, d in cached],
        )
    return cached  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _stage_key(stage: Any, device: str | None) -> tuple[str, str, str | None]:
    import orjson

    return (
        stage.provider,
        orjson.dumps(dict(stage.settings), option=orjson.OPT_SORT_KEYS).decode(),
        device,
    )


async def _chain_provider(eng: Any, stage: Any, rerank_cfg: Any, device: str | None = None) -> Any:
    """The reranker of one chain ``stage``, built once per engine; None when it cannot be built
    (kept, not retried). ``device`` is the planned device (MD-01); None keeps the stage's own."""
    cache: dict[Any, Any] = eng.__dict__.setdefault("_rerank_chain_cache", {})
    key = _stage_key(stage, device)
    if key in cache:
        return cache[key]
    settings = stage_settings(stage, rerank_cfg, device)
    build = getattr(eng, "_build_reranker", None)
    built = await build(settings) if callable(build) else await build_reranker(settings)
    if built is not None and hasattr(built, "unscored_tail"):
        built.unscored_tail = True  # the Jina window tail is unscored (None), never 0.0
    cache[key] = built
    return built


async def chain_score(
    eng: Any, query: str, documents: list[str], rerank_cfg: Any
) -> tuple[list[float | None], str | None, str | None, list[dict[str, str]]]:
    """Score ``documents`` with the first working reranker of ``model.rerank.chain``.

    Returns ``(scores, model_id, fallback_used, tried)``. ``scores`` has one entry per document,
    None where the reranker did not score it; with no working reranker every entry is None
    (``model_id`` None). ``fallback_used`` names the provider that answered when it is not the
    first of the chain. The first configured entry is the engine's own reranker
    (``eng._rerank_provider``), so the read gates and the chain share one instance.
    """
    tried: list[dict[str, str]] = []
    planned = _planned_chain(eng)
    chain = planned if planned is not None else [(s, "") for s in rerank_cfg.chain]
    first = getattr(eng, "_rerank_provider", None)
    for pos, (stage, device) in enumerate(chain):
        mode = stage.provider
        if pos == 0 and planned is None and callable(first):
            reranker = await first()
        else:
            reranker = await _chain_provider(eng, stage, rerank_cfg, device or None)
        if reranker is None:
            tried.append({"mode": mode, "outcome": "unavailable"})
            continue
        try:
            eng._rerank_calls += 1
            raw = list(await gated_rerank(reranker, query, documents))
            if len(raw) != len(documents):
                raise ValueError(f"{len(raw)} scores for {len(documents)} documents")
        except (MissingServiceError, ConnectionError, TimeoutError) as exc:
            eng._rerank_failures += 1
            _log.warning(
                "rerank.failed",
                reranker=getattr(reranker, "reranker_id", mode),
                error=type(exc).__name__,
            )
            tried.append({"mode": mode, "outcome": "failed"})
            continue
        scores: list[float | None] = [
            float(s) if s is not None and not isinstance(s, bool) and math.isfinite(s) else None
            for s in raw
        ]
        model_id = getattr(reranker, "reranker_id", mode)
        return scores, model_id, (mode if pos > 0 else None), tried
    return [None] * len(documents), None, None, tried


async def rerank_stage(
    eng: Any,
    ns: str,
    rerank_query: str,
    candidates: list[tuple[MemoryRecord, float]],
    rerank_cfg: Any,
) -> tuple[list[tuple[MemoryRecord, float]], bool]:
    """ADR-082 (R3-07 semantics): rerank the first ``model.rerank.pool`` candidates once.

    Scored candidates take the min-max normalised reranker score (optionally blended with the
    retrieval score by ``model.rerank.blend``). A candidate the reranker did not score (beyond the
    pool cap, or None from the reranker) is NOT given 0.0: it is recorded as unscored and its
    relevance is its retrieval score scaled below the lowest scored one, so it follows every
    scored candidate. With no working reranker, or below ``model.rerank.gate``, the candidates
    come back unchanged and ``reranked`` is False. Returns ``(candidates, reranked)``.
    """
    head = candidates[: rerank_cfg.pool]
    documents = [concat_background(record) for record, _ in head]
    if rerank_cfg.date_prefix:
        # Hindsight: the reranker sees when each candidate happened.
        documents = [
            doc
            if constants.UNDATED_TAG in record.tags
            else f"[Date: {record.valid_from:%Y-%m-%d}] {doc}"
            for (record, _), doc in zip(head, documents, strict=True)
        ]
    if rerank_cfg.context:
        documents = await eng._with_session_neighbours(ns, head, documents, rerank_cfg.context)
    scores, model_id, fallback, tried = await chain_score(eng, rerank_query, documents, rerank_cfg)
    scored_idx = [i for i, s in enumerate(scores) if s is not None]
    if (fx := _FORENSICS.get()) is not None:
        fx["reranker"] = model_id
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
            "pool_cap": rerank_cfg.pool,
        }
        tap.event("read.rerank.scored", **payload)
        if (fx := _FORENSICS.get()) is not None:
            fx["rerank_v2"] = payload  # forensic data key, kept for the eval tooling
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
    if rerank_cfg.gate is not None and max(vals) < rerank_cfg.gate:
        eng._rerank_gated += 1  # N41: not confident, keep the retrieval order
        return candidates, False
    rel = _minmax_normalize(vals)
    prior = _minmax_quiet([score for _, score in candidates])
    if rerank_cfg.blend is not None:
        w = rerank_cfg.blend
        rel = [w * r + (1.0 - w) * prior[i] for r, i in zip(rel, scored_idx, strict=True)]
    floor = min(rel)
    new_rel: dict[int, float] = dict(zip(scored_idx, rel, strict=True))
    out: list[tuple[MemoryRecord, float]] = []
    for i, (record, _) in enumerate(candidates):
        out.append((record, new_rel[i] if i in new_rel else floor * prior[i]))
    return out, True
