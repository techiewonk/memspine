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
first ``embed`` in a worker thread.

Loading, the offline artifact policy (RM-38) and :class:`StaticModelUnavailableError`
belong to the weights connector, :mod:`memspine.clients.weights.model2vec` (RM-113).
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from memspine.clients.weights.model2vec import env_offline, static_weights
from memspine.clients.weights.shared import SharedWeights
from memspine.services.embedding.base import EmbedderManifest

__all__ = ["DEFAULT_MODEL", "StaticEmbedder"]

#: Default distilled static model — small, CPU-only, no torch.
DEFAULT_MODEL = "minishlab/potion-base-8M"


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


class StaticEmbedder:
    """``weights`` is the injected connector (the factory passes the process-shared one);
    without it a private connector is made for standalone use (``local_files_only`` only
    describes that private load)."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        dim: int | None = None,
        *,
        local_files_only: bool = False,
        weights: SharedWeights[Any] | None = None,
    ) -> None:
        self._weights: SharedWeights[Any] = (
            weights
            if weights is not None
            else static_weights(model, local_files_only=local_files_only)
        )
        self._model_name = model
        self._model: Any = None
        self._dim = dim or 0
        self._expected_dim = dim
        self._local_files_only = local_files_only

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

    @property
    def offline(self) -> bool:
        """Whether the no-download policy applies (setting or ``HF_HUB_OFFLINE``)."""
        return self._local_files_only or env_offline()

    async def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = await self._weights.get()
        return self._model

    async def warm(self) -> None:
        """Load the shared weights and encode a fixed short text once (ADR-079 warm hook)."""
        await self._weights.warm()

    async def aclose(self) -> None:
        """Release this holder's weight lease; another holder keeps them loaded."""
        self._model = None
        await self._weights.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._ensure_model()

        def _run() -> list[list[float]]:
            return [_normalize([float(x) for x in vec]) for vec in model.encode(texts)]

        vectors = await asyncio.to_thread(_run)
        if vectors:
            actual_dim = len(vectors[0])
            if any(len(vector) != actual_dim for vector in vectors):
                raise ValueError("static embedder returned vectors with inconsistent dimensions")
            if self._expected_dim is not None and actual_dim != self._expected_dim:
                raise ValueError(
                    "static embedder output dimension differs from configured dimension"
                )
            self._dim = actual_dim
        return vectors
