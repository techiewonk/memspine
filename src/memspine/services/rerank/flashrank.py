"""flashrank cross-encoder reranker (E8, ``[rerank]`` extra, D-51).

The ranker loads on first use (or :meth:`FlashrankReranker.warm`) through the injected
connector (:mod:`memspine.clients.weights.flashrank`); Engine-built instances share it per
process, keyed by the model (ADR-079 / RM-73).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.weights.flashrank import ranker_weights
from memspine.clients.weights.shared import SharedWeights

__all__ = ["FlashrankReranker"]


class FlashrankReranker:
    def __init__(
        self, model: str | None = None, *, weights: SharedWeights[Any] | None = None
    ) -> None:
        # The factory injects the shared connector; standalone use builds a private one.
        self._weights: SharedWeights[Any] = (
            weights if weights is not None else ranker_weights(model)
        )
        self.reranker_id = f"flashrank:{model or 'default'}"

    async def warm(self) -> None:
        """Load the shared ranker and score one fixed pair (ADR-079 warm hook)."""
        await self._weights.warm()

    async def aclose(self) -> None:
        await self._weights.aclose()

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        from flashrank import RerankRequest

        ranker = await self._weights.get()
        request = RerankRequest(
            query=query,
            passages=[{"id": index, "text": text} for index, text in enumerate(documents)],
        )

        def _score() -> list[float]:
            scores = [0.0] * len(documents)
            for hit in ranker.rerank(request):
                scores[int(hit["id"])] = float(hit["score"])
            return scores

        return await asyncio.to_thread(_score)
