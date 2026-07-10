"""Reranker factory (E8, D-51): a registry mapping a rerank mode to a lazy
constructor, so "multiple support" is one registry entry — not an engine
if/elif. Each spec imports its adapter INSIDE the thunk (nothing heavy loads at
module import), and construction failures swallow to ``None`` so an unavailable
reranker degrades to vector order exactly as before (COR-3/ADR-018).

``rerank_modes()`` is the single source the engine's startup validator reads, so
a config typo fails fast at start while the valid set stays in lock-step with
the registry — including any deployment-registered provider (``register_reranker``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from memspine.exceptions import MissingServiceError
from memspine.observability.logging import get_logger
from memspine.services.rerank.base import Reranker

__all__ = [
    "RerankSettings",
    "build_reranker",
    "register_reranker",
    "rerank_modes",
]

_log = get_logger(__name__)


@dataclass(frozen=True)
class RerankSettings:
    """The resolved rerank config a spec needs (from ``read.rerank*``)."""

    mode: str
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None


#: A spec lazily constructs one Reranker from settings (imports its adapter here).
RerankSpec = Callable[[RerankSettings], Reranker]


def _build_fastembed(settings: RerankSettings) -> Reranker:
    from memspine.services.rerank.fastembed_rerank import FastembedReranker

    # FastembedReranker takes a required str with a default model; pass through
    # only when configured, else let it pick its default.
    return FastembedReranker(settings.model) if settings.model else FastembedReranker()


def _build_flashrank(settings: RerankSettings) -> Reranker:
    from memspine.services.rerank.flashrank_rerank import FlashrankReranker

    return FlashrankReranker(settings.model)


def _build_litellm(settings: RerankSettings) -> Reranker:
    if not settings.model:
        # A misconfigured cloud rerank must degrade like any unavailable adapter
        # (caught below → skip-log, sticky), not crash retrieval; name the fix.
        raise MissingServiceError(
            "services.rerank.litellm (set read.rerank_model)", extra="litellm"
        )
    from memspine.services.rerank.litellm_rerank import LiteLLMReranker

    return LiteLLMReranker(
        settings.model,
        api_base=settings.api_base,
        api_key=settings.api_key,
        aws_region=settings.aws_region,
    )


_REGISTRY: dict[str, RerankSpec] = {
    "fastembed": _build_fastembed,  # in-core (rides fastembed D-08)
    "flashrank": _build_flashrank,  # [rerank]
    "litellm": _build_litellm,  # [litellm] — Cohere/Voyage/Jina/Bedrock in one adapter
}


def register_reranker(mode: str, spec: RerankSpec) -> None:
    """Deployment seam (D-17 spirit): add a custom reranker without editing core.
    The shipped registry stays curated; new torch backends (e.g. an ``[st]``
    sentence-transformers cross-encoder) register here."""
    _REGISTRY[mode] = spec


def rerank_modes() -> tuple[str, ...]:
    """The valid ``read.rerank`` values — ``off`` plus every registered provider.
    The ONE source for the engine's startup validation, so the check can never
    drift from what ``build_reranker`` actually supports (the A1 bug's root)."""
    return ("off", *_REGISTRY)


def build_reranker(settings: RerankSettings) -> Reranker | None:
    """Construct the reranker for ``settings.mode``, or ``None`` for ``off`` /
    any unavailable adapter. ANY construction failure (ImportError,
    MissingServiceError, OSError model-download, weight-load ValueError/Runtime)
    swallows to ``None`` + one info log — the sticky skip the engine applied
    before, so retrieval degrades to vector order and never crashes."""
    if settings.mode == "off":
        return None
    spec = _REGISTRY.get(settings.mode)
    if spec is None:
        return None  # unknown mode is rejected by startup validation; be safe
    try:
        return spec(settings)
    except Exception as exc:
        _log.info(
            "rerank.unavailable",
            provider=settings.mode,
            detail=f"E8 rerank stage skipped — {exc}",
        )
        return None
