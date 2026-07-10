"""Cloud reranker via LiteLLM (E8, D-51): Cohere / Bedrock / Together rerank
endpoints behind the same ``Reranker`` port. The rerank ``model`` comes from
``read.rerank_model`` (e.g. ``cohere/rerank-english-v3.0``,
``bedrock/amazon.rerank-v1:0``). litellm returns a relevance score per document;
we map them back to input order (missing indices score 0.0). Imported lazily.
"""

from __future__ import annotations

from typing import Any

__all__ = ["LiteLLMReranker"]


class LiteLLMReranker:
    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        aws_region: str | None = None,
    ) -> None:
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._aws_region = aws_region
        self.reranker_id = f"litellm:{model}"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        import litellm

        kwargs: dict[str, Any] = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        }
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self._aws_region is not None:
            kwargs["aws_region_name"] = self._aws_region
        response = await litellm.arerank(**kwargs)
        scores = [0.0] * len(documents)
        for item in response.results:
            scores[int(item["index"])] = float(item["relevance_score"])
        return scores
