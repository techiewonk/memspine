"""fastembed ONNX cross-encoder reranker (E8/D-51).

fastembed is a core dependency (D-08), but ``TextCrossEncoder`` only exists in
recent releases — building the weights connector raises :class:`MissingServiceError`
on older installs and the engine logs one skip instead of failing retrieval. The
weights load on first use (or :meth:`FastembedReranker.warm`) through the injected
connector (:mod:`memspine.clients.weights.fastembed`); Engine-built instances share
them per process, keyed by the model (ADR-079 / RM-73).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.weights.fastembed import rerank_weights
from memspine.clients.weights.shared import SharedWeights
from memspine.config import constants

__all__ = ["FastembedReranker"]


class FastembedReranker:
    def __init__(
        self,
        model: str = constants.RERANK_FASTEMBED_MODEL,
        *,
        weights: SharedWeights[Any] | None = None,
    ) -> None:
        # The factory injects the shared connector; standalone use builds a private one.
        self._weights: SharedWeights[Any] = (
            weights if weights is not None else rerank_weights(model)
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
