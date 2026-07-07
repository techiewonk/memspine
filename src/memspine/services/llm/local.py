"""OpenAI-compatible local provider (D-39): Ollama, vLLM, LM Studio, llama.cpp
server — one adapter for all of them, over the injected HTTP client (D-24)."""

from __future__ import annotations

from typing import Any

import httpx

from memspine.clients.http import HTTPClient
from memspine.exceptions import LLMError

__all__ = ["OpenAICompatLLM"]


class OpenAICompatLLM:
    def __init__(
        self,
        http: HTTPClient,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    @property
    def provider_id(self) -> str:
        return f"openai_compat:{self._base_url}:{self._model}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {"model": self._model, "messages": messages, **options}
        try:
            response = await self._http.client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMError(
                f"LLM endpoint {self._base_url} returned {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach LLM endpoint {self._base_url}: {exc}") from exc
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"unexpected response shape from {self._base_url}: {str(data)[:500]}"
            ) from exc
        return str(content)
