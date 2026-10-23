"""ONNX cross-encoder adapter (ADR-084)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from memspine.clients.weights.onnx import rerank_weights
from memspine.clients.weights.onnx_catalogue import OnnxModelSpec, model_reranker_id
from memspine.clients.weights.shared import SharedWeights


class OnnxReranker:
    def __init__(
        self,
        model: str = "Xenova/ms-marco-MiniLM-L-6-v2",
        *,
        weights: SharedWeights[Any] | None = None,
        model_spec: dict[str, Any] | OnnxModelSpec | None = None,
        batch_size: int = 64,
        threads: int = 0,
        providers: Sequence[str] | None = None,
        local_files_only: bool = False,
    ) -> None:
        if batch_size < 1:
            raise ValueError("ONNX batch_size must be positive")
        self._batch_size = batch_size
        self._weights = (
            weights
            if weights is not None
            else rerank_weights(
                model,
                model_spec=model_spec,
                threads=threads,
                providers=providers,
                local_files_only=local_files_only,
            )
        )
        self.reranker_id = model_reranker_id(model, model_spec)

    async def warm(self) -> None:
        await self._weights.warm()

    async def aclose(self) -> None:
        await self._weights.aclose()

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = await self._weights.get()
        return await asyncio.to_thread(
            lambda: list(model.rerank(query, documents, batch_size=self._batch_size))
        )
