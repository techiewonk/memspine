"""HTTP test transport for focused model-adapter tests; never opens a socket."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import httpx

from memspine.clients.http import HttpConnector, HttpSpec
from memspine.clients.openai_compat import OpenAICompatConnector


async def _missing(**kwargs: Any) -> Any:
    raise AssertionError("test did not supply a model reply")


transport_stub = SimpleNamespace(acompletion=_missing, aembedding=_missing, arerank=_missing)


def install_transport(monkeypatch: Any) -> None:
    original = OpenAICompatConnector.__init__
    if getattr(original, "_model_test_transport", False):
        return

    def plain(value: Any) -> Any:
        if hasattr(value, "__dict__"):
            return {
                k: plain(getattr(value, k))
                for k in set(vars(value))
                | {
                    name
                    for name in ("choices", "message", "content", "usage", "data", "results")
                    if hasattr(value, name)
                }
            }
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        return value

    def initialize(self: Any, spec: Any, *, http: Any = None) -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            method = (
                "aembedding"
                if request.url.path.endswith("/embeddings")
                else "arerank"
                if request.url.path.endswith("/rerank")
                else "acompletion"
            )
            if request.url.path == "/api/chat":
                payload.update(payload.pop("options", {}))
                if "num_predict" in payload:
                    payload["max_tokens"] = payload.pop("num_predict")
            value = plain(await getattr(transport_stub, method)(**payload))
            if request.url.path == "/api/chat":
                usage = value.get("usage") or {}
                value = {
                    "message": value["choices"][0]["message"],
                    "prompt_eval_count": usage.get("prompt_tokens", 0),
                    "eval_count": usage.get("completion_tokens", 0),
                }
            return httpx.Response(200, json=value)

        spec = dataclasses.replace(spec, api_base=spec.api_base or "http://test.invalid")
        original(self, spec, http=HttpConnector(HttpSpec(transport=httpx.MockTransport(respond))))
        self._owns_http = True

    initialize._model_test_transport = True
    monkeypatch.setattr(OpenAICompatConnector, "__init__", initialize)
