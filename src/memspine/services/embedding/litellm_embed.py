"""Cloud/remote embeddings via LiteLLM (D-08 cloud path): OpenAI, Bedrock,
Vertex, Cohere, etc. behind the same ``EmbeddingService`` port.

``dim`` is **required from config**: a remote embedder's dimension is not
discoverable without a live call, and the vector store needs it to build its
table (LanceDB reads ``embedder.dim`` on first upsert). E4 quantization is off
(``manifest.quantization=None``) — a remote model's tolerance is unknown, so the
exact float32 path is used (never a guessed capability, D-10). litellm is
imported lazily on first ``embed``.
"""

from __future__ import annotations

from typing import Any

from memspine.services.embedding.base import EmbedderManifest

__all__ = ["LiteLLMEmbedding"]


class LiteLLMEmbedding:
    def __init__(
        self,
        model: str,
        dim: int,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        aws_region: str | None = None,
    ) -> None:
        self._model = model
        self._dim = dim
        self._api_base = api_base
        self._api_key = api_key
        self._aws_region = aws_region

    @property
    def embedder_id(self) -> str:
        return f"litellm:{self._model}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(self.embedder_id, self._dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import litellm

        kwargs: dict[str, Any] = {"model": self._model, "input": texts}
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self._aws_region is not None:
            kwargs["aws_region_name"] = self._aws_region
        response = await litellm.aembedding(**kwargs)
        return [list(item["embedding"]) for item in response.data]
