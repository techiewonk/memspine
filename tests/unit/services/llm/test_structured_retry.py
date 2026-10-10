"""I69: structured-output outcome counters, opt-in retry-with-error, constrained retry."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from memspine.config.schema import MemspineConfig
from memspine.exceptions import LLMError
from memspine.prompts.base import Prompt, PromptFormat
from memspine.services.llm import structured
from memspine.services.llm.structured import (
    StructuredOptions,
    configure,
    reset_structured_stats,
    structured_call,
    structured_stats,
)


class Out(BaseModel):
    n: int


class Fake:
    provider_id = "fake"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append({"messages": list(messages), "options": options})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _prompt(fmt: PromptFormat = PromptFormat.YAML) -> Prompt:
    return Prompt(id="t", version=1, role="extract", format=fmt, body="give n")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    reset_structured_stats()
    configure(None)
    yield
    reset_structured_stats()
    configure(None)


async def test_counts_clean_repaired_and_failed() -> None:
    p = _prompt(PromptFormat.JSON)
    llm = Fake(['{"n": 1}', '{"n": 2,', "not json at all"])
    assert (await structured_call(llm, p, {}, Out)).n == 1
    assert (await structured_call(llm, p, {}, Out)).n == 2  # repaired by json-repair
    with pytest.raises(LLMError):
        await structured_call(llm, p, {}, Out)
    row = structured_stats()["t@1"]
    assert (row["calls"], row["clean"], row["repaired"], row["validation_failed"]) == (3, 1, 1, 1)
    assert row["failure_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert len(llm.calls) == 3  # default: no retry


async def test_llm_error_is_counted_and_reraised() -> None:
    llm = Fake([LLMError("boom")])
    with pytest.raises(LLMError):
        await structured_call(llm, _prompt(), {}, Out)
    assert structured_stats()["t@1"]["llm_errors"] == 1


async def test_retry_with_error_once_and_recovers() -> None:
    configure(StructuredOptions(retry_on_error=True))
    llm = Fake(["n: abc", "n: 7"])
    assert (await structured_call(llm, _prompt(), {}, Out)).n == 7
    retry_msgs = llm.calls[1]["messages"]
    assert retry_msgs[-2] == {"role": "assistant", "content": "n: abc"}
    assert "failed validation" in retry_msgs[-1]["content"] and "int" in retry_msgs[-1]["content"]
    assert "response_format" not in llm.calls[1]["options"]  # constrained is a separate switch
    row = structured_stats()["t@1"]
    assert (row["retried"], row["retry_ok"], row["retry_failed"]) == (1, 1, 0)
    assert row["failure_rate"] == 0


async def test_retry_is_single_and_failure_counted() -> None:
    configure(StructuredOptions(retry_on_error=True))
    llm = Fake(["n: x", "n: y", "n: 3"])
    with pytest.raises(LLMError):
        await structured_call(llm, _prompt(), {}, Out)
    assert len(llm.calls) == 2  # one retry, never more
    assert structured_stats()["t@1"]["retry_failed"] == 1


async def test_constrained_decoding_only_on_the_retry() -> None:
    configure(StructuredOptions(retry_on_error=True, constrained_retry=True))
    llm = Fake(["n: oops", '{"n": 5}'])
    assert (await structured_call(llm, _prompt(), {}, Out)).n == 5
    assert "response_format" not in llm.calls[0]["options"]
    rf = llm.calls[1]["options"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["properties"]["n"]["type"] == "integer"
    assert structured_stats()["t@1"]["constrained_retries"] == 1


async def test_constrained_unsupported_falls_back_to_plain_retry() -> None:
    configure(StructuredOptions(retry_on_error=True, constrained_retry=True))
    llm = Fake(["n: oops", LLMError("response_format not supported"), "n: 9"])
    assert (await structured_call(llm, _prompt(), {}, Out)).n == 9
    assert "response_format" not in llm.calls[2]["options"]


def test_config_defaults_off_and_unknown_keys_refused() -> None:
    cfg = MemspineConfig()
    assert cfg.llm.structured.retry_on_error is False
    assert cfg.llm.structured.constrained_retry is False
    with pytest.raises(ValueError):
        MemspineConfig(llm={"structured": {"bogus": True}})
    assert StructuredOptions() == structured._OPTIONS
