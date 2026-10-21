"""In-process open-weight inference via llama-cpp-python (D-39), ``[llmlocal]``.

Import-guarded: constructing without the extra raises MissingServiceError
naming the fix (D-10). Inference runs in a worker thread (the lib is sync).

Thinking control matches the LiteLLM adapter: ``no_think`` (on by default for
``qwen3`` model files) appends the ``/no_think`` soft switch to the last user
message, and every reply goes through ``strip_think`` (B-7).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from memspine.exceptions import LLMError, MissingServiceError
from memspine.models.providers.llm.litellm_llm import _with_no_think, default_no_think, strip_think

__all__ = ["LlamaCppLLM"]


class LlamaCppLLM:
    def __init__(
        self,
        model_path: str,
        n_ctx: int = 8192,
        *,
        no_think: bool | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.8,
        top_p: float = 0.95,
        max_tokens: int = 2048,
        **llama_kwargs: Any,
    ) -> None:
        self._initialize_lifecycle_state()
        self.initialize(
            model_path,
            n_ctx=n_ctx,
            no_think=no_think,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            **llama_kwargs,
        )

    @classmethod
    def allocate(cls) -> LlamaCppLLM:
        """Allocate lifecycle state before a builder registers the close callback."""
        service = cls.__new__(cls)
        service._initialize_lifecycle_state()
        return service

    def _initialize_lifecycle_state(self) -> None:
        self._native_lock = threading.RLock()
        self._closed = False
        self._llama: Any | None = None
        self._model_path = ""
        self._thinking_model = False
        self.no_think = False

    def initialize(
        self,
        model_path: str,
        n_ctx: int = 8192,
        *,
        no_think: bool | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.8,
        top_p: float = 0.95,
        max_tokens: int = 2048,
        **llama_kwargs: Any,
    ) -> None:
        """Load the native model after the owner registered its closer.

        If the native constructor itself raises after allocating inaccessible
        partial state, llama.cpp does not return a handle this wrapper can close.
        """
        with self._native_lock:
            if self._closed or self._llama is not None:
                raise LLMError("llama.cpp model cannot be initialized more than once")
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise MissingServiceError("llm:llama_cpp", extra="llmlocal") from exc
            self._model_path = model_path
            self._timeout_seconds = timeout_seconds
            self._generation_defaults = {
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
            }
            self._thinking_model = default_no_think(Path(model_path).name)
            self.no_think = self._thinking_model if no_think is None else no_think
            self._llama = Llama(model_path=model_path, n_ctx=n_ctx, **llama_kwargs)

    @property
    def provider_id(self) -> str:
        return f"llama_cpp:{self._model_path}"

    async def close(self) -> None:
        """Release the native model once, after any in-flight inference ends."""
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._native_lock:
            if self._closed:
                return
            self._closed = True
            native, self._llama = self._llama, None
            if native is None:
                return
            close = getattr(native, "close", None)
            if not callable(close):
                raise LLMError("llama.cpp model does not expose explicit native cleanup")
            close()

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        # llama-cpp types ``messages`` as role-specific TypedDicts; ours are the plain
        # role/content dicts every provider port takes, so hand them over untyped.
        chat_messages: Any = _with_no_think(messages) if self.no_think else messages

        def _run() -> Any:
            with self._native_lock:
                if self._closed or self._llama is None:
                    raise LLMError("llama.cpp model is closed")
                generation = {**self._generation_defaults, **options}
                return self._llama.create_chat_completion(messages=chat_messages, **generation)

        data = await asyncio.wait_for(asyncio.to_thread(_run), timeout=self._timeout_seconds)
        try:
            content = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {str(data)[:500]}") from exc
        return strip_think(content, lenient=self.no_think or self._thinking_model)
