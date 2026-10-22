"""Structured output (D-31): format-aware parsing + repair + validation."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.exceptions import LLMError
from memspine.prompts.base import parse_prompt_text
from memspine.prompts.models import ExtractedFacts
from memspine.services.llm.structured import structured_call

PROMPT = parse_prompt_text("id: extract\nrole: extract\nformat: yaml\n---\nExtract: {{ content }}")


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "fake"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.messages.append(messages)
        return self.reply


async def test_yaml_reply_parses_e9() -> None:
    llm = FakeLLM("facts:\n  - entity: alice\n    attribute: city\n    value: Berlin\n")
    result = await structured_call(llm, PROMPT, {"content": "x"}, ExtractedFacts)
    assert result.facts[0].entity == "alice"
    assert llm.messages[0][-1]["content"] == "Extract: x"


async def test_fenced_and_broken_json_repaired() -> None:
    llm = FakeLLM('```json\n{"facts": [{"entity": "a", "attribute": "b", "value": "c",]}\n```')
    result = await structured_call(llm, PROMPT, {"content": "x"}, ExtractedFacts)
    assert result.facts[0].value == "c"


async def test_unusable_reply_raises_llm_error_with_context() -> None:
    llm = FakeLLM("I cannot help with that.")
    with pytest.raises(LLMError, match=r"extract@1.*failed validation"):
        await structured_call(llm, PROMPT, {"content": "x"}, ExtractedFacts)
