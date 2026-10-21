"""In-process open-weight inference via llama-cpp-python (D-39), ``[llmlocal]``.

Import-guarded: constructing without the extra raises MissingServiceError
naming the fix (D-10). Inference runs in a worker thread (the lib is sync).

Thinking control matches the LiteLLM adapter: ``no_think`` (on by default for
``qwen3`` model files) appends the ``/no_think`` soft switch to the last user
message, and every reply goes through ``strip_think`` (B-7).

Builders load through :meth:`LlamaCppLLM.initialize_shared`: the native model is keyed by
its absolute path and native load options (never the role or profile name), so two roles or
profiles using the same GGUF share one load per process (ADR-079 / RM-73). Generation
defaults, timeouts and thinking control stay per holder. Every native call holds the shared
handle's lock.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

from memspine.exceptions import LLMError, MissingServiceError
from memspine.models.providers.llm.litellm_llm import _with_no_think, default_no_think, strip_think
from memspine.models.runtime.shared_weights import SharedWeights
from memspine.providers.shared import ResourceRegistry

__all__ = ["LlamaCppLLM"]


class _NativeLlama:
    """One loaded GGUF plus the lock serialising every native call on it."""

    __slots__ = ("llama", "lock")

    def __init__(self, llama: Any) -> None:
        self.llama: Any | None = llama
        self.lock = threading.RLock()

    def close(self) -> None:
        """Release the native model once, after any in-flight inference ends."""
        with self.lock:
            native, self.llama = self.llama, None
            if native is None:
                return
            close = getattr(native, "close", None)
            if not callable(close):
                raise LLMError("llama.cpp model does not expose explicit native cleanup")
            close()


def _absolute(model_path: str) -> str:
    """The weight key's path: two spellings of one GGUF share a load (no filesystem I/O)."""
    return os.path.abspath(model_path)


def _llama_class() -> Any:
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise MissingServiceError("llm:llama_cpp", extra="llmlocal") from exc
    return Llama


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
        self._state_lock = threading.Lock()
        self._closed = False
        self._native: _NativeLlama | None = None
        self._weights: SharedWeights[_NativeLlama] | None = None
        self._model_path = ""
        self._thinking_model = False
        self.no_think = False

    def _configure(
        self,
        model_path: str,
        *,
        no_think: bool | None,
        timeout_seconds: float,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> None:
        with self._state_lock:
            if self._closed or self._native is not None or self._weights is not None:
                raise LLMError("llama.cpp model cannot be initialized more than once")
        self._model_path = model_path
        self._timeout_seconds = timeout_seconds
        self._generation_defaults = {
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        self._thinking_model = default_no_think(Path(model_path).name)
        self.no_think = self._thinking_model if no_think is None else no_think

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
        """Load a private native model (direct construction; never shared)."""
        llama = _llama_class()
        self._configure(
            model_path,
            no_think=no_think,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        self._native = _NativeLlama(llama(model_path=model_path, n_ctx=n_ctx, **llama_kwargs))

    async def initialize_shared(
        self,
        model_path: str,
        *,
        registry: ResourceRegistry,
        n_ctx: int = 8192,
        no_think: bool | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.8,
        top_p: float = 0.95,
        max_tokens: int = 2048,
        **llama_kwargs: Any,
    ) -> None:
        """Load (or join) the process-shared native model after the owner registered its
        closer. The key is the absolute model path plus the native load options.

        If the native constructor raises after allocating inaccessible partial state,
        llama.cpp does not return a handle this wrapper can close.
        """
        llama = _llama_class()
        self._configure(
            model_path,
            no_think=no_think,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        weights: SharedWeights[_NativeLlama] = SharedWeights(
            "model.llm.llamacpp",
            {"model_path": _absolute(model_path)},
            {"n_ctx": n_ctx, **llama_kwargs},
            lambda: _NativeLlama(llama(model_path=model_path, n_ctx=n_ctx, **llama_kwargs)),
            close=_NativeLlama.close,
            registry=registry,
        )
        with self._state_lock:
            if self._closed:
                raise LLMError("llama.cpp model is closed")
            self._weights = weights
        native = await weights.get()
        with self._state_lock:
            if not self._closed:
                self._native = native

    @property
    def provider_id(self) -> str:
        return f"llama_cpp:{self._model_path}"

    async def warm(self) -> None:
        """ADR-079 / RM-74 warm hook: make sure the shared native model is loaded (the
        Engine build loads it; this re-joins a load a cancelled warm-up interrupted)."""
        with self._state_lock:
            weights = self._weights
        if weights is not None:
            await weights.warm()

    async def close(self) -> None:
        """Release this holder once. The native model closes after in-flight inference
        ends, and only when no other holder shares it."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            native, self._native = self._native, None
            weights, self._weights = self._weights, None
        if weights is not None:
            await weights.aclose()
        elif native is not None:
            await asyncio.to_thread(native.close)

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        # llama-cpp types ``messages`` as role-specific TypedDicts; ours are the plain
        # role/content dicts every provider port takes, so hand them over untyped.
        chat_messages: Any = _with_no_think(messages) if self.no_think else messages

        def _run() -> Any:
            native = self._native
            if self._closed or native is None:
                raise LLMError("llama.cpp model is closed")
            with native.lock:
                if self._closed or native.llama is None:
                    raise LLMError("llama.cpp model is closed")
                generation = {**self._generation_defaults, **options}
                return native.llama.create_chat_completion(messages=chat_messages, **generation)

        data = await asyncio.wait_for(asyncio.to_thread(_run), timeout=self._timeout_seconds)
        try:
            content = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected llama.cpp response shape: {str(data)[:500]}") from exc
        return strip_think(content, lenient=self.no_think or self._thinking_model)
