"""llama-cpp-python weights connector (RM-113, ``[llmlocal]`` extra).

The native model is keyed by its absolute path and native load options (never a role or
profile name), so two roles or profiles using the same GGUF share one load per process
(ADR-079 / RM-73). Every native call holds the loaded handle's lock.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import LLMError, MissingServiceError

__all__ = ["LlamaCppSpec", "NativeLlama", "llama_class", "llama_weights", "load_native"]


class NativeLlama:
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


@dataclass(frozen=True, slots=True)
class LlamaCppSpec:
    """A GGUF path plus the native load options (``n_gpu_layers``, ``n_batch``, ...)."""

    model_path: str
    n_ctx: int = 8192
    options: dict[str, Any] = field(default_factory=dict)


def llama_class() -> Any:
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise MissingServiceError("llm:llama_cpp", extra="llmlocal") from exc
    return Llama


def load_native(spec: LlamaCppSpec) -> NativeLlama:
    """A private (unshared) native load."""
    llama = llama_class()
    return NativeLlama(llama(model_path=spec.model_path, n_ctx=spec.n_ctx, **spec.options))


def llama_weights(
    spec: LlamaCppSpec, *, registry: WeightRegistry | None = None
) -> SharedWeights[NativeLlama]:
    """A lease on the native model; the extra is checked here.

    If the native constructor raises after allocating inaccessible partial state, llama.cpp
    does not return a handle this connector can close.
    """
    llama = llama_class()
    return SharedWeights(
        "model.llm.llamacpp",
        # The weight key's path: two spellings of one GGUF share a load (no filesystem I/O).
        {"model_path": os.path.abspath(spec.model_path)},
        {"n_ctx": spec.n_ctx, **spec.options},
        lambda: NativeLlama(llama(model_path=spec.model_path, n_ctx=spec.n_ctx, **spec.options)),
        close=NativeLlama.close,
        registry=registry,
    )
