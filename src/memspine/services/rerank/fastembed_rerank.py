"""fastembed ONNX cross-encoder reranker (E8/D-51).

fastembed is a core dependency (D-08), but ``TextCrossEncoder`` only exists in
recent releases — construction raises :class:`MissingServiceError` on older
installs and the engine logs one skip instead of failing retrieval.
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.config import constants
from memspine.exceptions import MissingServiceError

__all__ = ["FastembedReranker"]


class FastembedReranker:
    def __init__(self, model: str = constants.RERANK_FASTEMBED_MODEL) -> None:
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
        self._encoder: Any = TextCrossEncoder(model_name=model)
        self.reranker_id = f"fastembed:{model}"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        # ONNX inference is CPU-bound and sync — keep the event loop free.
        return await asyncio.to_thread(
            lambda: [float(score) for score in self._encoder.rerank(query, documents)]
        )
