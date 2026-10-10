"""Reader/judge gap fixes: grounded QA prompt, --retry-refusal, --judge-guards (all opt-in)."""

from __future__ import annotations

from typing import Any

import failure_buckets as fb
from memspine_evals.contracts import Query, ReaderAnswer
from memspine_evals.experiments import C01Config, build_judge
from memspine_evals.judge import GuardedJudge
from memspine_evals.judge_prompts import JUDGE_PROMPTS, RoutedLLMJudge
from memspine_evals.readers import QA_PROMPTS, ScriptedReader
from memspine_evals.refusal import DENIAL, REFUSAL, RefusalRetryReader, is_refusal

CONTEXT = "[2023-05-25] Caroline: I went hiking last Friday [= 2023-05-19]"


class _Reader:
    """Scripted replies in order; records the questions it was asked."""

    reader_id = "stub"
    model = "stub-model"
    makes_model_calls = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.questions: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.questions.append(question)
        return ReaderAnswer(
            text=self.replies.pop(0), prompt_tokens=10, completion_tokens=2, model_calls=1
        )


class _Chat:
    def __init__(self, reply: str = '{"label": "CORRECT"}') -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, system: str | None = None) -> str:
        self.prompts.append(prompt)
        return self.reply


def test_grounded_prompt_is_registered_and_explains_the_dates() -> None:
    from memspine_evals.cli import build_parser

    text = QA_PROMPTS["grounded"]
    rendered = text.format(context=CONTEXT, question="When?", question_date="x")
    assert CONTEXT in rendered and "Question: When?" in rendered
    assert "[= 2023-05-20]" in text and "[YYYY-MM-DD]" in text
    assert "only when nothing in the memories bears on" in text
    assert "say you do not know" not in text
    assert text != QA_PROMPTS["default"]
    args = build_parser().parse_args(["c0-1", "--dataset", "locomo", "--qa-prompt", "grounded"])
    assert args.qa_prompt == "grounded"


def test_default_prompt_is_unchanged() -> None:
    assert QA_PROMPTS["default"].startswith("Answer the question using only the context below.")


def test_refusal_patterns_match_failure_buckets() -> None:
    assert REFUSAL.pattern == fb.REFUSAL.pattern and REFUSAL.flags == fb.REFUSAL.flags
    assert DENIAL.pattern == fb.DENIAL.pattern


def test_is_refusal() -> None:
    for text in ("I do not know.", "Not mentioned", "Melanie did not go.", "", "   "):
        assert is_refusal(text), text
    for text in ("Denver", "The Friday before 25 May 2023", "Likely yes, because she runs"):
        assert not is_refusal(text), text


async def test_retry_runs_once_and_accounts_the_extra_call() -> None:
    inner = _Reader("I do not know", "19 May 2023")
    reader = RefusalRetryReader(inner, mode="assertive")
    out = await reader.answer("When?", CONTEXT)
    assert out.text == "19 May 2023"
    assert out.model_calls == 2 and out.prompt_tokens == 20 and out.completion_tokens == 4
    assert inner.questions[0] == "When?" and inner.questions[1].startswith("When?")
    assert "best-supported evidence" in inner.questions[1]
    assert out.extra_meta == {
        "retry_refusal": True,
        "first_answer": "I do not know",
        "retry_answer": "19 May 2023",
        "retry_accepted": True,
        # D3: the first call and the retry keep their own token counts
        "first_prompt_tokens": 10,
        "first_completion_tokens": 2,
        "retry_prompt_tokens": 10,
        "retry_completion_tokens": 2,
        "retry_mode": "assertive",
        "retry_require_context_overlap": False,
    }
    assert reader.retried == 1 and reader.recovered == 1
    assert reader.reader_id == "stub+retry" and reader.describe()["retry_refusal"] is True


async def test_retry_keeps_the_first_answer_when_the_retry_also_refuses() -> None:
    reader = RefusalRetryReader(_Reader("Not mentioned", "I do not know"))
    out = await reader.answer("When?", CONTEXT)
    assert out.text == "Not mentioned" and out.model_calls == 2
    assert out.extra_meta["retry_accepted"] is False


async def test_no_retry_for_a_real_answer_or_an_empty_context() -> None:
    inner = _Reader("Denver")
    out = await RefusalRetryReader(inner).answer("Where?", CONTEXT)
    assert out.text == "Denver" and out.model_calls == 1 and not out.extra_meta
    inner = _Reader("Not mentioned")
    out = await RefusalRetryReader(inner).answer("Where?", "  ")
    assert out.text == "Not mentioned" and out.model_calls == 1 and len(inner.questions) == 1


async def test_retry_flag_wraps_the_built_reader() -> None:
    from memspine_evals.experiments import build_reader_and_judge

    config = C01Config(mode="qa", max_model_calls=5, categories=(1, 2, 3, 4), retry_refusal=True)
    reader, _, _ = build_reader_and_judge(config)
    assert isinstance(reader, RefusalRetryReader)
    plain, _, _ = build_reader_and_judge(
        C01Config(mode="qa", max_model_calls=5, categories=(1, 2, 3, 4))
    )
    assert not isinstance(plain, RefusalRetryReader)


async def test_empty_answer_is_wrong_without_a_judge_call() -> None:
    chat = _Chat()
    config = C01Config(mode="qa", judge_prompt="rubric", judge_guards=True)
    judge = build_judge(config, chat, "m")
    assert isinstance(judge, GuardedJudge) and judge.spec.params["empty_answer_guard"]
    for blank in ("", "  \n"):
        v = await judge.score_query(Query(query_id="q", text="When?", gold="19 May 2023"), blank)
        assert v.score == 0.0 and v.model_calls == 0 and v.meta["guard"] == "empty_answer"
    assert chat.prompts == []
    v = await judge.score_query(Query(query_id="q", text="When?", gold="19 May"), "19 May 2023")
    assert v.score == 1.0 and v.model_calls == 1 and len(chat.prompts) == 1


async def test_guarded_rubric_variant_is_selected_only_with_guards() -> None:
    chat = _Chat()
    query = Query(query_id="q", text="When?", gold="The Friday before 25 May 2023")
    guarded = build_judge(C01Config(mode="qa", judge_guards=True), chat, "m")
    await guarded.score_query(query, "19 May 2023")  # type: ignore[attr-defined]
    assert "Friday before" in chat.prompts[-1] and "hedging" in chat.prompts[-1]
    assert guarded.spec.params["suite"] == "rubric-guarded"

    plain = build_judge(C01Config(mode="qa"), chat, "m")
    assert isinstance(plain, RoutedLLMJudge) and plain.spec.params["suite"] == "rubric"
    await plain.score_query(query, "19 May 2023")
    assert "hedging" not in chat.prompts[-1]
    # the original rubric text is unchanged and still reachable
    assert JUDGE_PROMPTS["memspine/rubric"].text != JUDGE_PROMPTS["memspine/rubric-guarded"].text
    explicit = build_judge(C01Config(mode="qa", judge_prompt="rubric-guarded"), chat, "m")
    assert explicit.spec.params["suite"] == "rubric-guarded"


def test_cli_flags_reach_the_parser() -> None:
    from memspine_evals.cli import build_parser

    args = build_parser().parse_args(
        [
            "c0-1",
            "--dataset",
            "locomo",
            "--retry-refusal",
            "--judge-guards",
            "--judge-prompt",
            "rubric-guarded",
        ]
    )
    assert args.retry_refusal and args.judge_guards and args.judge_prompt == "rubric-guarded"
    off = build_parser().parse_args(["c0-1", "--dataset", "locomo"])
    assert not off.retry_refusal and not off.judge_guards


def test_scripted_reader_unaffected() -> None:
    assert ScriptedReader({}).describe()["reader_id"] == "scripted"
