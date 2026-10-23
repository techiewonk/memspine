"""Remote reranking adapter: relevance scores restored to document input order."""

from __future__ import annotations

from typing import Any

from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
from memspine.services._retry import retry_transient

__all__ = ["OpenAICompatReranker"]


class OpenAICompatReranker:
    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        request_timeout_s: float,
        connector: Any = None,
        provider: str = "openai_compatible",
    ) -> None:
        self._model = model
        self._provider = provider
        # The factory injects the transport; standalone use describes a private one.
        self._connector = connector or OpenAICompatConnector(
            OpenAICompatSpec(api_base=api_base, api_key=api_key)
        )
        self._request_timeout_s = request_timeout_s
        self.reranker_id = f"{provider}:{model}"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        kwargs: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
            "timeout": self._request_timeout_s,
        }
        response = await retry_transient(
            lambda: self._connector.arerank(**kwargs), what=f"rerank:{self._model}"
        )
        scores = [0.0] * len(documents)
        for item in response["results"]:
            scores[int(item["index"])] = float(item["relevance_score"])
        return scores

    async def aclose(self) -> None:
        await self._connector.close()
