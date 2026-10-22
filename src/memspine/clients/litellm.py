"""LiteLLM transport connector (RM-113 item 17): endpoint, credentials, region and deadline.

One connector carries everything that addresses a LiteLLM-reachable endpoint (``api_base``,
the API key, the AWS region, the per-request deadline) so the LLM, embedding and rerank
adapters only build requests. The ``litellm``, ``ollama``, ``vllm`` and ``bedrock`` LLM
descriptors all build this connector; the model route (``ollama_chat/``, ``hosted_vllm/``,
``bedrock/``) is fixed by the descriptor, never inferred from a model-name prefix (ADR-068).

``litellm`` is imported on each call (lazily, and so tests can patch the module); nothing is
connected at construction. ``health`` never makes a model call (no paid probe, ADR-068).
Retry policy for transient provider failures stays with the adapters' shared
``services._retry`` helper (it logs through observability, which clients may not import).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

__all__ = ["LOCAL_HOSTS", "LiteLLMConnector", "LiteLLMSpec"]

#: Hosts that mean "this machine" for the locality check.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


@dataclass(frozen=True, slots=True)
class LiteLLMSpec:
    """Transport spec (never a pydantic config model). ``api_key`` is the resolved secret."""

    api_base: str | None = None
    api_key: str | None = field(default=None, repr=False)
    aws_region: str | None = None
    #: per-request deadline in seconds (None: litellm's default)
    timeout_s: float | None = None


class LiteLLMConnector:
    """Stateless transport for LiteLLM calls; ``close`` is a no-op and idempotent."""

    kind = "litellm"
    scope = "thread"

    def __init__(self, spec: LiteLLMSpec | None = None) -> None:
        self._spec = spec or LiteLLMSpec()

    @property
    def spec(self) -> LiteLLMSpec:
        return self._spec

    def identity(self) -> dict[str, Any]:
        """Credential-free key: endpoint, region and an auth digest."""
        key = self._spec.api_key
        return {
            "api_base": self._spec.api_base,
            "aws_region": self._spec.aws_region,
            "auth": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else None,
        }

    def locality(self) -> str:
        """``host`` for a loopback endpoint, else ``cluster`` (a remote service)."""
        base = self._spec.api_base
        if base and (urlparse(base).hostname or "").lower() in LOCAL_HOSTS:
            return "host"
        return "cluster"

    async def open(self) -> None:
        """Nothing to connect: litellm opens per request."""

    async def health(self, deadline_s: float | None = None) -> bool:
        """The transport package is importable; never a model call."""
        del deadline_s
        try:
            import litellm  # noqa: F401
        except ImportError:
            return False
        return True

    async def close(self) -> None:
        return None

    @property
    def handle(self) -> Any:
        import litellm

        return litellm

    def request_kwargs(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Transport keyword arguments for one litellm call (only the ones that are set)."""
        out: dict[str, Any] = {}
        timeout = timeout_s if timeout_s is not None else self._spec.timeout_s
        if timeout is not None:
            out["timeout"] = timeout
        if self._spec.api_base is not None:
            out["api_base"] = self._spec.api_base
        if self._spec.api_key is not None:
            out["api_key"] = self._spec.api_key
        if self._spec.aws_region is not None:
            out["aws_region_name"] = self._spec.aws_region
        return out

    async def acompletion(self, **kwargs: Any) -> Any:
        import litellm

        return await litellm.acompletion(**{**self.request_kwargs(), **kwargs})

    async def aembedding(self, **kwargs: Any) -> Any:
        import litellm

        return await litellm.aembedding(**{**self.request_kwargs(), **kwargs})

    async def arerank(self, **kwargs: Any) -> Any:
        import litellm

        return await litellm.arerank(**{**self.request_kwargs(), **kwargs})
