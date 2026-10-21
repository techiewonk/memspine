"""Default embedder (D-08): fastembed — ONNX, CPU-friendly, no torch.

The model loads lazily on first use (it may download weights); inference runs
in a worker thread because fastembed is synchronous. Engine-built instances share
the loaded weights per process through the resource registry (ADR-079 / RM-73).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.config.schema.providers import FASTEMBED_MODEL_DIMS
from memspine.models.encoders.embedding.base import EmbedderManifest
from memspine.models.runtime.shared_weights import SharedWeights
from memspine.providers.shared import ResourceRegistry

__all__ = ["FastembedEmbedding"]

_KNOWN_DIMS = FASTEMBED_MODEL_DIMS
#: Fixed text the warm hooks embed once per shared model (never cached as a result).
WARM_TEXT = "warm-up"


class FastembedEmbedding:
    def __init__(
        self,
        model: str = "BAAI/bge-small-en-v1.5",
        query_instruction: str | None = None,
        dim: int | None = None,
        *,
        registry: ResourceRegistry | None = None,
    ) -> None:
        self._model_name = model
        #: N64: prepended to queries only (``embed_queries``); None = symmetric.
        self._query_instruction = query_instruction or None
        self._model: Any = None
        resolved_dim = _KNOWN_DIMS.get(model, dim)
        if dim is not None and resolved_dim is not None and dim != resolved_dim:
            raise ValueError("configured fastembed dimension conflicts with known model metadata")
        if resolved_dim is None:
            raise ValueError(
                "fastembed dimension must be configured for models without known metadata"
            )
        self._dim = resolved_dim
        self._expected_dim = resolved_dim
        # Weight key: the model only. The query instruction and dimension check are per holder.
        self._weights: SharedWeights[Any] = SharedWeights(
            "model.embedding.fastembed",
            {"model": model},
            None,
            self._load,
            warm=lambda loaded: list(loaded.embed([WARM_TEXT])),
            registry=registry,
        )

    @property
    def embedder_id(self) -> str:
        return f"fastembed:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        """E4: no capability declared until per-model metadata lands — an
        unclaimed capability is safer than a guessed one (D-10 spirit), so the
        default fastembed path stays exact float32 (ADR-020). A deployer who
        knows their model tolerates int8 opts in via ``vector.quantization``."""
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    def _load(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(model_name=self._model_name)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = await self._weights.get()
        return self._model

    async def warm(self) -> None:
        """Load the shared weights and embed a fixed short text once (ADR-079 warm hook)."""
        await self._weights.warm()

    async def aclose(self) -> None:
        """Release this holder's weight lease; another Engine's lease keeps them loaded."""
        self._model = None
        await self._weights.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._ensure_model()

        def _run() -> list[list[float]]:
            return [list(map(float, vec)) for vec in model.embed(texts)]

        vectors = await asyncio.to_thread(_run)
        if vectors:
            actual_dim = len(vectors[0])
            if any(len(vector) != actual_dim for vector in vectors):
                raise ValueError("fastembed returned vectors with inconsistent dimensions")
            if self._expected_dim is not None and actual_dim != self._expected_dim:
                raise ValueError(
                    "fastembed output dimension differs from configured model dimension"
                )
            self._dim = actual_dim
        return vectors

    @property
    def query_variant(self) -> str:
        """N64: a cache-key tag for the query instruction ("" when none)."""
        if self._query_instruction is None:
            return ""
        import xxhash

        return ":" + xxhash.xxh64_hexdigest(self._query_instruction.encode())

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """N64: queries with the model's retrieval instruction, when one is set."""
        if self._query_instruction is None:
            return await self.embed(texts)
        return await self.embed([self._query_instruction + t for t in texts])
