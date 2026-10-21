"""fastembed ONNX cross-encoder reranker (E8/D-51).

fastembed is a core dependency (D-08), but ``TextCrossEncoder`` only exists in
recent releases — construction raises :class:`MissingServiceError` on older
installs and the engine logs one skip instead of failing retrieval. The weights
load on first use (or :meth:`FastembedReranker.warm`); Engine-built instances
share them per process, keyed by the model (ADR-079 / RM-73).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.config import constants
from memspine.exceptions import MissingServiceError
from memspine.models.runtime.shared_weights import SharedWeights
from memspine.providers.shared import ResourceRegistry

__all__ = ["FastembedReranker"]


class FastembedReranker:
    def __init__(
        self,
        model: str = constants.RERANK_FASTEMBED_MODEL,
        *,
        registry: ResourceRegistry | None = None,
    ) -> None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # fastembed too old for rerankers
            # CMP-2/ADR-018: fastembed IS a core dep, so the remedy is an
            # UPGRADE (TextCrossEncoder ships only in recent releases), not a
            # new extra. Name it — plus the flashrank alternative — so the skip
            # log is actionable, not a bare "service unavailable".
            raise MissingServiceError(
                "fastembed cross-encoder reranker (upgrade fastembed to a version "
                "shipping TextCrossEncoder, or set read.rerank=flashrank)",
                extra="rerank",
            ) from exc
        self._weights: SharedWeights[Any] = SharedWeights(
            "model.reranker.fastembed",
            {"model": model},
            None,
            lambda: TextCrossEncoder(model_name=model),
            warm=lambda encoder: list(encoder.rerank("warm-up", ["warm-up"])),
            registry=registry,
        )
        self.reranker_id = f"fastembed:{model}"

    async def warm(self) -> None:
        """Load the shared weights and score one fixed pair (ADR-079 warm hook)."""
        await self._weights.warm()

    async def aclose(self) -> None:
        await self._weights.aclose()

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        encoder = await self._weights.get()
        # ONNX inference is CPU-bound and sync — keep the event loop free.
        return await asyncio.to_thread(
            lambda: [float(score) for score in encoder.rerank(query, documents)]
        )
