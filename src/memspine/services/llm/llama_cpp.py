"""In-process open-weight inference via llama-cpp-python (D-39), ``[llmlocal]``.

Import-guarded: constructing without the extra raises MissingServiceError
naming the fix (D-10). Inference runs in a worker thread (the lib is sync).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.exceptions import LLMError, MissingServiceError

__all__ = ["LlamaCppLLM"]


class LlamaCppLLM:
    def __init__(self, model_path: str, n_ctx: int = 8192, **llama_kwargs: Any) -> None:
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise MissingServiceError("llm:llama_cpp", extra="llmlocal") from exc
        self._model_path = model_path
        self._llama = Llama(model_path=model_path, n_ctx=n_ctx, **llama_kwargs)

    @property
    def provider_id(self) -> str:
        return f"llama_cpp:{self._model_path}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        def _run() -> Any:
            return self._llama.create_chat_completion(messages=messages, **options)

        data = await asyncio.to_thread(_run)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {str(data)[:500]}") from exc
