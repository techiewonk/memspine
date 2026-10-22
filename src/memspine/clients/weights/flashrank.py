"""flashrank weights connector (RM-113, ``[rerank]`` extra): the ``Ranker`` load."""

from __future__ import annotations

from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import MissingServiceError

__all__ = ["ranker_weights"]


def _warm(ranker: Any) -> None:
    from flashrank import RerankRequest

    ranker.rerank(RerankRequest(query="warm-up", passages=[{"id": 0, "text": "warm-up"}]))


def ranker_weights(
    model: str | None, *, registry: WeightRegistry | None = None
) -> SharedWeights[Any]:
    """A lease on ``flashrank.Ranker`` (the package's default model when ``model`` is None)."""
    try:
        from flashrank import Ranker
    except ImportError as exc:
        raise MissingServiceError("services.rerank.flashrank", extra="rerank") from exc
    return SharedWeights(
        "model.reranker.flashrank",
        {"model": model or "default"},
        None,
        (lambda: Ranker(model_name=model)) if model else Ranker,
        warm=_warm,
        registry=registry,
    )
