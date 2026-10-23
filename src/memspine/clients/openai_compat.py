"""OpenAI-compatible endpoint connector on the shared HTTP pool (ADR-085)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from memspine.clients.http import HttpConnector, HttpSpec


class RateLimitError(RuntimeError):
    pass


class ServiceUnavailableError(RuntimeError):
    pass


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


@dataclass(frozen=True, slots=True)
class OpenAICompatSpec:
    api_base: str | None = None
    api_key_env: str | None = None
    api_key: str | None = field(default=None, repr=False)
    timeout_s: float | None = None
    family: str = "openai_compatible"
    rerank_path: str = "/rerank"


class OpenAICompatConnector:
    kind = "openai_compatible"
    scope = "loop"

    def __init__(self, spec: OpenAICompatSpec, *, http: HttpConnector | None = None) -> None:
        if not spec.api_base:
            raise ValueError("openai_compatible requires settings.api_base")
        parts = urlsplit(spec.api_base)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("api_base must not contain credentials, a query or a fragment")
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("api_base must be an absolute HTTP(S) URL")
        self._spec = spec
        self._http = http or HttpConnector(HttpSpec(timeout_s=spec.timeout_s or 60.0))
        self._owns_http = http is None

    @property
    def spec(self) -> OpenAICompatSpec:
        return self._spec

    def borrow_http(self, http: HttpConnector) -> None:
        """Use a composition-owned pool; its lease, rather than this holder, closes it."""
        self._http = http
        self._owns_http = False

    def _key(self) -> str | None:
        return (
            os.environ.get(self._spec.api_key_env) if self._spec.api_key_env else self._spec.api_key
        )

    def identity(self) -> dict[str, Any]:
        key = self._key()
        return {
            "api_base": self._spec.api_base,
            "family": self._spec.family,
            "auth": hashlib.sha256(key.encode()).hexdigest()[:16] if key else None,
        }

    def locality(self) -> str:
        return "host" if urlsplit(self._spec.api_base or "").hostname in LOCAL_HOSTS else "cluster"

    async def open(self) -> None:
        await self._http.open()

    async def health(self, deadline_s: float | None = None) -> bool:
        # Configuration/package availability only; health must never invoke a model.
        return True

    async def close(self) -> None:
        if self._owns_http:
            await self._http.close()

    def _url(self, path: str) -> str:
        base = (self._spec.api_base or "").rstrip("/")
        if path.startswith("/api/"):
            base = base.removesuffix("/v1")
        elif path.startswith("/v1/") and base.endswith("/v1"):
            path = path[3:]
        return base + path

    async def request(
        self, path: str, payload: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        import json

        headers = {"Content-Type": "application/json"}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        reply = await self._http.request(
            "POST",
            self._url(path),
            headers=headers,
            json=payload,
            timeout_s=timeout_s or self._spec.timeout_s,
        )
        if reply.status >= 400:
            # Error bodies and URLs can contain secrets; status alone is safe.
            error = (
                RateLimitError
                if reply.status == 429
                else ServiceUnavailableError
                if reply.status >= 500
                else RuntimeError
            )
            raise error(f"Model endpoint returned HTTP {reply.status}")
        result = json.loads(reply.body)
        if not isinstance(result, dict):
            raise ValueError("Model endpoint response must be a JSON object")
        return result

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", None)
        if self._spec.family == "ollama":
            payload = {k: kwargs[k] for k in ("model", "messages")}
            payload["stream"] = False
            options: dict[str, Any] = {
                k: kwargs[k] for k in ("temperature", "top_p") if k in kwargs
            }
            if "max_tokens" in kwargs:
                options["num_predict"] = kwargs["max_tokens"]
            payload["options"] = options
            if "think" in kwargs:
                payload["think"] = kwargs["think"]
            fmt = kwargs.get("response_format")
            if fmt:
                payload["format"] = fmt.get("json_schema", {}).get(
                    "schema", fmt.get("schema", "json")
                )
            result = await self.request("/api/chat", payload, timeout_s=timeout)
            return {
                "choices": [{"message": result["message"]}],
                "usage": {
                    "prompt_tokens": result.get("prompt_eval_count", 0),
                    "completion_tokens": result.get("eval_count", 0),
                },
            }
        return await self.request("/v1/chat/completions", kwargs, timeout_s=timeout)

    async def aembedding(self, **kwargs: Any) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", None)
        return await self.request("/v1/embeddings", kwargs, timeout_s=timeout)

    async def arerank(self, **kwargs: Any) -> dict[str, Any]:
        timeout = kwargs.pop("timeout", None)
        path = "/v1/rerank" if self._spec.family == "vllm" else self._spec.rerank_path
        return await self.request(path, kwargs, timeout_s=timeout)
