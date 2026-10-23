"""Optional any-llm connector: official provider SDKs behind the [anyllm] extra."""

from __future__ import annotations

import inspect
import os
from typing import Any, cast

from memspine.exceptions import MissingServiceError


class AnyLLMConnector:
    def __init__(
        self, provider: str, *, api_key_env: str | None = None, api_base: str | None = None
    ) -> None:
        self.provider = provider
        self.api_key_env = api_key_env
        self.api_base = api_base
        self._client: Any = None

    async def open(self) -> None:
        if self._client is None:
            try:
                from any_llm import AnyLLM
            except ImportError:
                raise MissingServiceError("anyllm", "anyllm") from None
            options: dict[str, Any] = {}
            if self.api_key_env:
                options["api_key"] = os.environ.get(self.api_key_env)
            if self.api_base:
                options["api_base"] = self.api_base
            self._client = AnyLLM.create(self.provider, unified_exceptions=True, **options)

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        await self.open()
        if getattr(self._client, "TIMEOUT_SUPPORT", "native") == "unsupported":
            # The service still enforces its asyncio wall-clock bound for these SDKs.
            kwargs.pop("timeout", None)
        response = await self._client.acompletion(**kwargs)
        return cast("dict[str, Any]", response.model_dump())

    async def close(self) -> None:
        provider, self._client = self._client, None
        if provider is None:
            return
        # any-llm 1.x exposes the provider SDK as `client`, with no base close method.
        target = provider if hasattr(provider, "aclose") else getattr(provider, "client", None)
        close = getattr(target, "aclose", None) or getattr(target, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
