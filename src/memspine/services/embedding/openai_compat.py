"""Remote embedding adapter over an injected connector (ADR-085).

Dimensions are configured before any model call. Query/document input types and
requested dimensions contribute to the vector-space identity.
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.anyllm import AnyLLMConnector
from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
from memspine.services._retry import retry_transient
from memspine.services.embedding.base import EmbedderManifest
from memspine.services.embedding.identity import remote_embedding_id

__all__ = ["OpenAICompatEmbedding"]


class OpenAICompatEmbedding:
    def __init__(
        self,
        model: str,
        dim: int,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        request_timeout_s: float,
        request_dimensions: bool = False,
        query_input_type: str | None = None,
        document_input_type: str | None = None,
        connector: Any = None,
        provider: str = "openai_compatible",
    ) -> None:
        self._model = model
        self._provider = provider
        self._dim = dim
        # The factory injects the transport; standalone use describes a private one.
        self._connector = connector or OpenAICompatConnector(
            OpenAICompatSpec(api_base=api_base, api_key=api_key)
        )
        self._request_timeout_s = request_timeout_s
        self._request_dimensions = request_dimensions
        self._query_input_type = query_input_type
        self._document_input_type = document_input_type

    @property
    def embedder_id(self) -> str:
        if self._provider != "bedrock":
            if isinstance(self._connector, AnyLLMConnector):
                endpoint = self._connector.api_base
                family = self._connector.provider
                upstream = self._connector.provider
            else:
                endpoint = self._connector.spec.api_base
                family = self._connector.spec.family
                upstream = None
            return remote_embedding_id(
                self._provider,
                self._model,
                self._dim,
                api_base=endpoint,
                family=family,
                upstream_provider=upstream,
                request_dimensions=self._request_dimensions,
                query_input_type=self._query_input_type,
                document_input_type=self._document_input_type,
            )
        suffix = f"@{self._dim}" if self._request_dimensions else ""
        if self._query_input_type or self._document_input_type:
            suffix += f"[{self._document_input_type}|{self._query_input_type}]"
        return f"{self._provider}:{self._model}{suffix}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(self.embedder_id, self._dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, self._document_input_type)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, self._query_input_type)

    async def _embed(self, texts: list[str], input_type: str | None) -> list[list[float]]:
        kwargs: dict[str, Any] = {"model": self._model, "input": texts}
        kwargs["timeout"] = self._request_timeout_s
        if self._request_dimensions:
            kwargs["dimensions"] = self._dim
        if input_type is not None:
            kwargs["input_type"] = input_type
        response = await retry_transient(
            lambda: asyncio.wait_for(
                self._connector.aembedding(**kwargs), timeout=self._request_timeout_s
            ),
            what=f"embed:{self._model}",
        )
        vectors = [list(map(float, item["embedding"])) for item in response["data"]]
        if any(len(vector) != self._dim for vector in vectors):
            raise ValueError(
                "OpenAICompat output dimension differs from model.embedding.settings.dim"
            )
        return vectors

    async def aclose(self) -> None:
        await self._connector.close()
