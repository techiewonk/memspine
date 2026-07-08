"""model2vec static-embedding provider (E4, ``[static]`` extra, ADR-020).

Static embeddings are a distilled lookup table — no transformer forward pass, so
they embed on CPU orders of magnitude faster than fastembed at a quality cost.
E4 uses them as an OPT-IN prefilter: a cheap first pass narrows the candidate
set before the real embedder + float32 rescore do the precise work. Never the
production default (that is fastembed, D-08).

The extra is checked at construction (like the flashrank reranker) so a missing
``[static]`` fails fast with the D-10 fix-hint when chosen as a *provider*; as
an optional prefilter *stage* the engine catches that and skip-logs instead of
crashing (retrieval degrades, never fails). The model itself loads lazily on
first ``embed`` (it may download weights) in a worker thread.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.services.embedding.base import EmbedderManifest

__all__ = ["StaticEmbedder"]

#: Default distilled static model — small, CPU-only, no torch.
_DEFAULT_MODEL = "minishlab/potion-base-8M"


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


class StaticEmbedder:
    def __init__(self, model: str = _DEFAULT_MODEL) -> None:
        try:
            import model2vec  # noqa: F401
        except ImportError as exc:
            raise MissingServiceError("services.embedding.static", extra="static") from exc
        self._model_name = model
        self._model: Any = None
        self._dim = 0
        self._lock = asyncio.Lock()

    @property
    def embedder_id(self) -> str:
        return f"static:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        """The static embedder is itself the cheap stage, so it declares no
        further quantization/truncation — its vectors go straight to prefilter."""
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from model2vec import StaticModel

                    self._model = await asyncio.to_thread(
                        StaticModel.from_pretrained, self._model_name
                    )
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._ensure_model()

        def _run() -> list[list[float]]:
            return [_normalize([float(x) for x in vec]) for vec in model.encode(texts)]

        vectors = await asyncio.to_thread(_run)
        if vectors:
            self._dim = len(vectors[0])
        return vectors
