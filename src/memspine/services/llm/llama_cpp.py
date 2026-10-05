"""In-process open-weight inference via llama-cpp-python (D-39), ``[llmlocal]``.

Import-guarded: constructing without the extra raises MissingServiceError
naming the fix (D-10). Inference runs in a worker thread (the lib is sync).

Thinking control matches the LiteLLM adapter: ``no_think`` (on by default for
``qwen3`` model files) appends the ``/no_think`` soft switch to the last user
message, and every reply goes through ``strip_think`` (B-7).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memspine.exceptions import LLMError, MissingServiceError
from memspine.services.llm.litellm_llm import _with_no_think, default_no_think, strip_think

__all__ = ["LlamaCppLLM"]


class LlamaCppLLM:
    def __init__(
        self,
        model_path: str,
        n_ctx: int = 8192,
        *,
        no_think: bool | None = None,
        **llama_kwargs: Any,
    ) -> None:
        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise MissingServiceError("llm:llama_cpp", extra="llmlocal") from exc
        self._model_path = model_path
        self._thinking_model = default_no_think(Path(model_path).name)
        self.no_think = self._thinking_model if no_think is None else no_think
        self._llama = Llama(model_path=model_path, n_ctx=n_ctx, **llama_kwargs)

    @property
    def provider_id(self) -> str:
        return f"llama_cpp:{self._model_path}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        # llama-cpp types ``messages`` as role-specific TypedDicts; ours are the plain
        # role/content dicts every provider port takes, so hand them over untyped.
        chat_messages: Any = _with_no_think(messages) if self.no_think else messages

        def _run() -> Any:
            return self._llama.create_chat_completion(messages=chat_messages, **options)

        data = await asyncio.to_thread(_run)
        try:
            content = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {str(data)[:500]}") from exc
        return strip_think(content, lenient=self.no_think or self._thinking_model)
