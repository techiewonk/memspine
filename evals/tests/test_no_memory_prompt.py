"""I29: an empty retrieved context (relevance gate / abstention) has a clean reader path."""

from __future__ import annotations

from typing import Any

import pytest
from memspine_evals import readers
from memspine_evals.cli import build_parser
from memspine_evals.readers import NO_MEMORY_QA_PROMPT, QA_PROMPTS, OpenAICompatReader


class _Resp:
    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }


class _Client:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> _Resp:
        self.payloads.append(json)
        return _Resp()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    c = _Client()
    monkeypatch.setattr(readers, "_shared_client", lambda httpx, timeout: c)
    return c


async def test_empty_context_uses_the_no_memory_prompt_when_enabled(client: _Client) -> None:
    reader = OpenAICompatReader(
        "m", prompt=QA_PROMPTS["default"], empty_context_prompt=NO_MEMORY_QA_PROMPT
    )
    await reader.answer("What is the capital of France?", "")
    text = client.payloads[0]["messages"][0]["content"]
    assert "No stored memories are relevant" in text and "capital of France" in text
    assert "Memories:" not in text and "{" not in text
    await reader.answer("Where did she hike?", "[2023-05-01] Caroline hiked in Denver")
    assert "Denver" in client.payloads[1]["messages"][0]["content"]
    assert reader.describe()["empty_context_prompt"] is True


async def test_default_reader_is_unchanged_on_an_empty_context(client: _Client) -> None:
    reader = OpenAICompatReader("m", prompt=QA_PROMPTS["default"])
    await reader.answer("Anything?", "")
    assert "No stored memories" not in client.payloads[0]["messages"][0]["content"]
    assert "empty_context_prompt" not in reader.describe()


def test_cli_flag_is_off_by_default() -> None:
    assert build_parser().parse_args(["c0-1", "--dataset", "locomo"]).no_memory_prompt is False
    args = build_parser().parse_args(["c0-1", "--dataset", "locomo", "--no-memory-prompt"])
    assert args.no_memory_prompt is True
