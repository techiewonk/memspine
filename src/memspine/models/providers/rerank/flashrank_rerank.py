"""flashrank cross-encoder reranker (E8, ``[rerank]`` extra, D-51)."""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["FlashrankReranker"]


class FlashrankReranker:
    def __init__(self, model: str | None = None) -> None:
        try:
            from flashrank import Ranker
        except ImportError as exc:
            raise MissingServiceError("services.rerank.flashrank", extra="rerank") from exc
        self._ranker: Any = Ranker(model_name=model) if model else Ranker()
        self.reranker_id = f"flashrank:{model or 'default'}"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        from flashrank import RerankRequest

        request = RerankRequest(
            query=query,
            passages=[{"id": index, "text": text} for index, text in enumerate(documents)],
        )

        def _score() -> list[float]:
            scores = [0.0] * len(documents)
            for hit in self._ranker.rerank(request):
                scores[int(hit["id"])] = float(hit["score"])
            return scores

        return await asyncio.to_thread(_score)
