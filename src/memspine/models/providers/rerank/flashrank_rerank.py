"""flashrank cross-encoder reranker (E8, ``[rerank]`` extra, D-51).

The ranker loads on first use (or :meth:`FlashrankReranker.warm`); Engine-built instances
share it per process, keyed by the model (ADR-079 / RM-73).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.models.runtime.shared_weights import SharedWeights
from memspine.providers.shared import ResourceRegistry

__all__ = ["FlashrankReranker"]


def _warm(ranker: Any) -> None:
    from flashrank import RerankRequest

    ranker.rerank(RerankRequest(query="warm-up", passages=[{"id": 0, "text": "warm-up"}]))


class FlashrankReranker:
    def __init__(
        self, model: str | None = None, *, registry: ResourceRegistry | None = None
    ) -> None:
        try:
            from flashrank import Ranker
        except ImportError as exc:
            raise MissingServiceError("services.rerank.flashrank", extra="rerank") from exc
        self._weights: SharedWeights[Any] = SharedWeights(
            "model.reranker.flashrank",
            {"model": model or "default"},
            None,
            (lambda: Ranker(model_name=model)) if model else Ranker,
            warm=_warm,
            registry=registry,
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
