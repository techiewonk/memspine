"""Unified LLM provider via LiteLLM (D-07/D-33/D-39): cloud + OpenAI-compatible
local behind one adapter (supersedes the hand-rolled ``OpenAICompatLLM``).

The litellm ``model`` string carries the provider — ``openai/gpt-4o``,
``ollama/llama3`` (local; set ``api_base``), ``bedrock/anthropic.claude-...``
(set ``aws_region`` or rely on the boto3 credential chain), ``vertex_ai/...``,
``azure/...``, or any of litellm's 100+ providers. litellm is a core dep now
(amends D-03/D-33) but imported lazily on first ``chat`` so a default engine
with no LLM role never pays its multi-second import cost.
"""

from __future__ import annotations

from typing import Any

from memspine.exceptions import LLMError

__all__ = ["LiteLLMLLM"]


class LiteLLMLLM:
    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        aws_region: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._aws_region = aws_region
        self._timeout = timeout_seconds

    @property
    def provider_id(self) -> str:
        return f"litellm:{self._model}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "timeout": self._timeout,
        }
        if self._api_base is not None:
            kwargs["api_base"] = self._api_base
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self._aws_region is not None:
            kwargs["aws_region_name"] = self._aws_region
        kwargs.update(options)
        try:
            response = await litellm.acompletion(**kwargs)
            content = response.choices[0].message.content
        except Exception as exc:
            raise LLMError(f"litellm chat failed for model {self._model!r}: {exc}") from exc
        if content is None:
            raise LLMError(f"litellm returned empty content for model {self._model!r}")
        return str(content)
