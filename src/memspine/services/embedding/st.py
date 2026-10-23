"""Local torch embedder over sentence-transformers (``model.embedding.provider: st``, ``[st]`` extra).

For models onnx cannot load, e.g. Qwen3-Embedding-0.6B (BF16 on the GPU). Queries
take the model's retrieval instruction (``embedding.query_instruction``), documents
are embedded bare. The weights load on first use inside a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.weights.sentence_transformers import SentenceTransformerSpec, st_weights
from memspine.clients.weights.shared import SharedWeights
from memspine.services.embedding.base import EmbedderManifest

__all__ = ["SentenceTransformersEmbedding"]


class SentenceTransformersEmbedding:
    def __init__(
        self,
        model: str,
        dim: int,
        *,
        query_instruction: str | None = None,
        device: str | None = None,
        dtype: str | None = None,
        batch_size: int = 32,
        query_prompt_name: str | None = None,
        document_prompt_name: str | None = None,
        trust_remote_code: bool = False,
        weights: SharedWeights[Any] | None = None,
    ) -> None:
        # The factory injects the shared connector; standalone use builds a private one
        # (``device``, ``dtype`` and ``trust_remote_code`` only describe that private load).
        # Prompts, instructions and the batch size are per holder.
        self._weights: SharedWeights[Any] = (
            weights
            if weights is not None
            else st_weights(SentenceTransformerSpec(model, device, dtype, trust_remote_code))
        )
        self._model_name = model
        self._dim = dim
        self._query_instruction = query_instruction or None
        self._batch_size = max(1, batch_size)
        self._query_prompt_name = query_prompt_name
        self._document_prompt_name = document_prompt_name
        self._model: Any = None

    @property
    def embedder_id(self) -> str:
        return f"st:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

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

    async def embed(self, texts: list[str], *, prompt_name: str | None = None) -> list[list[float]]:
        model = await self._ensure_model()
        prompt_name = prompt_name or self._document_prompt_name

        def _run() -> list[list[float]]:
            vecs = model.encode(
                texts,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                **({"prompt_name": prompt_name} if prompt_name else {}),
            )
            if any(len(v) < self._dim for v in vecs):
                raise ValueError(
                    "sentence-transformers output dimension is smaller than model.embedding.settings.dim"
                )
            return [[float(x) for x in v[: self._dim]] for v in vecs]

        return await asyncio.to_thread(_run)

    @property
    def query_variant(self) -> str:
        tag = (self._query_instruction or "") + "|" + (self._query_prompt_name or "")
        if tag == "|":
            return ""
        import xxhash

        return ":" + xxhash.xxh64_hexdigest(tag.encode())

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        if self._query_prompt_name:
            return await self.embed(texts, prompt_name=self._query_prompt_name)
        if self._query_instruction is None:
            return await self.embed(texts)
        return await self.embed([self._query_instruction + t for t in texts])
