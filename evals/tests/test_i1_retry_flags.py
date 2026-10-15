"""I1: ``--retry-mode neutral|assertive`` and ``--retry-overlap`` reach ``RefusalRetryReader``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.experiments import C01Config, _retry_decider_kwargs
from memspine_evals.refusal import RefusalRetryReader


def _args(*extra: str) -> Any:
    return build_parser().parse_args(["c0-1", "--dataset", "locomo", *extra])


def test_cli_defaults_keep_neutral_and_no_overlap() -> None:
    a = _args("--retry-refusal")
    assert a.retry_mode == "neutral" and a.retry_overlap is False


def test_cli_accepts_assertive_and_overlap() -> None:
    a = _args("--retry-refusal", "--retry-mode", "assertive", "--retry-overlap")
    assert a.retry_mode == "assertive" and a.retry_overlap is True


def test_cli_rejects_unknown_mode() -> None:
    with pytest.raises(SystemExit):
        _args("--retry-mode", "pushy")


def test_default_kwargs_are_unchanged() -> None:
    assert _retry_decider_kwargs(C01Config()) == {}


def test_kwargs_carry_mode_and_overlap() -> None:
    kw = _retry_decider_kwargs(C01Config(retry_mode="assertive", retry_overlap=True))
    assert kw == {"mode": "assertive", "require_context_overlap": True}


class _Inner:
    reader_id = "stub"
    model = "m"
    makes_model_calls = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(self, question: str, context: str, question_date: str | None = None) -> ReaderAnswer:
        return ReaderAnswer(text=self.replies.pop(0), model_calls=1)


def test_mode_changes_reader_id_and_overlap_blocks_ungrounded_retry() -> None:
    cfg = C01Config(retry_mode="assertive", retry_overlap=True)
    reader = RefusalRetryReader(_Inner("Not mentioned.", "zzzz qqqq"), **_retry_decider_kwargs(cfg))
    assert reader.reader_id == "stub+retry" and reader.mode == "assertive"
    out = asyncio.run(reader.answer("Where?", "Caroline hiked in Denver"))
    assert out.text == "Not mentioned."  # the retry shares no content word with the context
    neutral = RefusalRetryReader(_Inner("x"), **_retry_decider_kwargs(C01Config()))
    assert neutral.reader_id == "stub+retry-neutral"
