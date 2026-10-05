"""#39: the harness ``--verify-answer`` reader wrapper (fake chat backends only)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from memspine_evals.readers import ScriptedReader
from memspine_evals.verify import VerifyingReader, chat_verifier

CONTEXT = "[2023-05-08] Caroline: I moved to Denver\n[2023-05-09] Caroline: I like tea"


class _Chat:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, prompt: str, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _reader(chat: _Chat, answer: str = "Boston") -> VerifyingReader:
    verify, version = chat_verifier(chat)
    return VerifyingReader(ScriptedReader({"Where?": answer}), verify, version)


async def test_unsupported_answer_is_revised() -> None:
    chat = _Chat("supported: false\nevidence: [1]\nrevised_answer: Denver")
    reader = _reader(chat)
    out = await reader.answer("Where?", CONTEXT)
    assert out.text == "Denver"
    assert out.model_calls == 1  # the scripted reader made none, verification one
    assert reader.revised == 1 and reader.failures == 0
    prompt, system = chat.calls[0]
    assert "[1] [2023-05-08] Caroline: I moved to Denver" in prompt
    assert "answer: Boston" in prompt
    assert system is not None and "numbered context lines" in system


async def test_supported_answer_stands() -> None:
    reader = _reader(_Chat("supported: true\nevidence: [1]\nrevised_answer: Paris"), "Denver")
    out = await reader.answer("Where?", CONTEXT)
    assert out.text == "Denver" and reader.revised == 0


async def test_unsupported_without_revision_stands() -> None:
    reader = _reader(_Chat("supported: false\nevidence: []\nrevised_answer:"))
    assert (await reader.answer("Where?", CONTEXT)).text == "Boston"


async def test_a_failed_verification_keeps_the_answer() -> None:
    reader = _reader(_Chat(RuntimeError("endpoint down")))
    out = await reader.answer("Where?", CONTEXT)
    assert out.text == "Boston" and out.model_calls == 1 and reader.failures == 1


def test_describe_adds_only_the_verify_key() -> None:
    inner = ScriptedReader({})
    verify, version = chat_verifier(_Chat("supported: true"))
    wrapped = VerifyingReader(inner, verify, version)
    assert wrapped.describe() == {**inner.describe(), "verify_answer": version}
    assert version.startswith("verify_answer")


@pytest.mark.parametrize("bedrock", [False, True])
def test_flag_off_builds_the_unchanged_reader(
    monkeypatch: pytest.MonkeyPatch, bedrock: bool
) -> None:
    from memspine_evals.experiments import C01Config, build_reader_and_judge

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    base: dict[str, Any] = {"mode": "qa", "bedrock": bedrock, "max_model_calls": 5}
    off, _, _ = build_reader_and_judge(C01Config(**base, qa_prompt="dated"))
    on, _, _ = build_reader_and_judge(C01Config(**base, qa_prompt="dated", verify_answer=True))
    assert not isinstance(off, VerifyingReader)
    assert "verify_answer" not in off.describe()
    assert isinstance(on, VerifyingReader)
    assert type(on.inner) is type(off)
    assert on.describe() == {**off.describe(), "verify_answer": on.prompt_version}


def test_cli_flag() -> None:
    from memspine_evals.cli import build_parser

    parser = build_parser()
    on = parser.parse_args(["c0-1", "--dataset", "locomo", "--verify-answer"])
    assert on.verify_answer is True
    assert parser.parse_args(["c0-1", "--dataset", "locomo"]).verify_answer is False
