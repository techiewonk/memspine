"""Harness model transport and response conversion for endpoint HTTP clients."""

from __future__ import annotations
import os
from types import SimpleNamespace
from typing import Any
from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec


def object_reply(value: dict[str, Any]) -> Any:
    fields = dict(value)
    if "choices" in fields:
        fields["choices"] = [
            SimpleNamespace(**{**choice, "message": SimpleNamespace(**choice["message"])})
            for choice in fields["choices"]
        ]
    if fields.get("usage") is not None:
        fields["usage"] = SimpleNamespace(**fields["usage"])
    return SimpleNamespace(**fields)


def json_reply(value: Any) -> Any:
    if isinstance(value, SimpleNamespace):
        return {k: json_reply(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: json_reply(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_reply(v) for v in value]
    return value


class HarnessEndpoint:
    """A local-first endpoint used by readers and transport-contract tests."""

    def __init__(self) -> None:
        self.connector = OpenAICompatConnector(
            OpenAICompatSpec(
                api_base=os.environ.get("OPENAI_API_BASE", "http://localhost:8000/v1"),
                api_key_env="OPENAI_API_KEY",
            )
        )

    async def acompletion(self, **kwargs: Any) -> Any:
        return object_reply(await self.connector.acompletion(**kwargs))

    async def aembedding(self, **kwargs: Any) -> Any:
        return object_reply(await self.connector.aembedding(**kwargs))
