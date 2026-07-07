"""Default embedder (D-08): fastembed — ONNX, CPU-friendly, no torch.

The model loads lazily on first use (it may download weights); inference runs
in a worker thread because fastembed is synchronous.
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.services.embedding.base import EmbedderManifest

__all__ = ["FastembedEmbedding"]

_KNOWN_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


class FastembedEmbedding:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model
        self._model: Any = None
        self._dim = _KNOWN_DIMS.get(model, 384)
        self._lock = asyncio.Lock()

    @property
    def embedder_id(self) -> str:
        return f"fastembed:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        """E4 seam: no capability declared until per-model metadata lands —
        an unclaimed capability is safer than a guessed one (D-10 spirit)."""
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding

                    self._model = await asyncio.to_thread(
                        TextEmbedding, model_name=self._model_name
                    )
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._ensure_model()

        def _run() -> list[list[float]]:
            return [list(map(float, vec)) for vec in model.embed(texts)]

        vectors = await asyncio.to_thread(_run)
        if vectors:
            self._dim = len(vectors[0])
        return vectors
