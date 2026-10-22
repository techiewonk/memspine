"""fastembed weights connector (RM-113): ONNX text embedder and cross-encoder loads.

fastembed downloads on first load (its own cache); the load runs in a worker thread through
:class:`~memspine.clients.weights.shared.SharedWeights`, keyed by the model only.
"""

from __future__ import annotations

from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import MissingServiceError

__all__ = ["WARM_TEXT", "cross_encoder_class", "rerank_weights", "text_weights"]

#: Fixed text the warm hooks embed once per shared model (never cached as a result).
WARM_TEXT = "warm-up"


def text_weights(model: str, *, registry: WeightRegistry | None = None) -> SharedWeights[Any]:
    """A lease on ``fastembed.TextEmbedding(model)``."""

    def load() -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=model)

    return SharedWeights(
        "model.embedding.fastembed",
        {"model": model},
        None,
        load,
        warm=lambda loaded: list(loaded.embed([WARM_TEXT])),
        registry=registry,
    )


def cross_encoder_class() -> Any:
    """``TextCrossEncoder``, or :class:`MissingServiceError` on a fastembed without it."""
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError as exc:  # fastembed too old for rerankers
        # CMP-2/ADR-018: fastembed IS a core dep, so the remedy is an UPGRADE
        # (TextCrossEncoder ships only in recent releases), not a new extra.
        raise MissingServiceError(
            "fastembed cross-encoder reranker (upgrade fastembed to a version "
            "shipping TextCrossEncoder, or set read.rerank=flashrank)",
            extra="rerank",
        ) from exc
    return TextCrossEncoder


def rerank_weights(model: str, *, registry: WeightRegistry | None = None) -> SharedWeights[Any]:
    """A lease on ``fastembed`` ``TextCrossEncoder(model)`` (checked at construction)."""
    encoder = cross_encoder_class()
    return SharedWeights(
        "model.reranker.fastembed",
        {"model": model},
        None,
        lambda: encoder(model_name=model),
        warm=lambda loaded: list(loaded.rerank(WARM_TEXT, [WARM_TEXT])),
        registry=registry,
    )
