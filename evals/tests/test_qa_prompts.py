"""H7/H12 and benchmark QA prompt variants are well-formed, distinct and declared."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.readers import QA_PROMPTS


def test_variants_format_and_differ() -> None:
    rendered = {
        k: v.format(context="[2023-07-15] x", question="q?", question_date="2023/05/30")
        for k, v in QA_PROMPTS.items()
    }
    assert {"default", "dated", "abstain", "converse", "mab_fc", "question_dated"} <= set(rendered)
    assert len(set(rendered.values())) == len(rendered)
    assert "Not mentioned" in rendered["abstain"]
    assert "computed from the line's date" in rendered["dated"]
    assert "larger serial number is newer" in rendered["mab_fc"]


def test_dated_infer_is_dated_plus_the_inference_rule() -> None:
    """G10: same as ``dated`` except the refusal clause, which now asks for an inference."""
    from memspine_evals.readers import DATED_QA_PROMPT, INFER_RULE

    infer = QA_PROMPTS["dated_infer"]
    assert INFER_RULE in infer and "Likely yes, because" in infer
    assert infer.replace(INFER_RULE, "") == DATED_QA_PROMPT.replace(
        "If the context does not contain the answer, say you do not know.", ""
    )
    assert "would, might, or is likely to" not in QA_PROMPTS["dated"]


def test_cli_accepts_every_qa_prompt() -> None:
    from memspine_evals.cli import build_parser

    parser = build_parser()
    for name in QA_PROMPTS:
        args = parser.parse_args(["c0-1", "--dataset", "locomo", "--qa-prompt", name])
        assert args.qa_prompt == name


def test_dated2_is_dated_plus_the_said_vs_happened_rule() -> None:
    """G12: same as ``dated`` plus one rule before the length instruction."""
    from memspine_evals.readers import DATED_QA_PROMPT, SAID_HAPPENED_RULE

    dated2 = QA_PROMPTS["dated2"]
    assert SAID_HAPPENED_RULE in dated2
    assert "[= ...] value when present" in dated2
    assert dated2.replace(f"{SAID_HAPPENED_RULE} ", "") == DATED_QA_PROMPT
    assert "happened date" not in QA_PROMPTS["dated"]


def test_dated3_reasons_then_answers_without_a_blanket_refusal() -> None:
    """#34: brief reasoning, then a final "Answer:" line; quote the detail; dates in the
    style asked; said vs happened (#29); merge repeats before counting (#60)."""
    from memspine_evals.readers import REASONING_QA_PROMPTS

    dated3 = QA_PROMPTS["dated3"]
    assert "dated3" in REASONING_QA_PROMPTS and "dated" not in REASONING_QA_PROMPTS
    assert "A date in brackets is when it was said; the event may be earlier." in dated3
    assert 'starts with "Answer:"' in dated3 and dated3.endswith("Reasoning:")
    assert "quoting its words" in dated3 and "a month for" in dated3
    assert "merge repeated mentions of the same event" in dated3
    assert "say you do not know" not in dated3
    assert 'answer "Not mentioned"' in dated3
    rendered = dated3.format(context="[2023-07-15] x", question="q?", question_date="d")
    assert "[said ... \u00b7 happened ...]" in rendered


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        ("Line 3 names the second puppy.\nAnswer: Shadow", "Shadow"),
        ("**Answer:** 7 May 2023", "7 May 2023"),
        ("I first thought answer: Coco.\nFinal answer: Shadow", "Shadow"),
        ("<think>Answer: no</think>Line 2.\nAnswer: yes", "yes"),
        ("Not mentioned", "Not mentioned"),
        ("Melanie went camping.\nAnswer:", "Melanie went camping."),
    ],
)
def test_final_answer_matches_the_engine_extractor(reply: str, answer: str) -> None:
    from memspine_evals.readers import final_answer

    from memspine.core.answer import final_answer as engine_final_answer

    assert final_answer(reply) == answer == engine_final_answer(reply)


def _fake_httpx(reply: str) -> Any:
    class FakeResponse:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None: ...

        async def post(self, url: str, json: dict[str, Any], headers: Any) -> FakeResponse:
            return FakeResponse()

    return type("H", (), {"AsyncClient": FakeClient})


@pytest.mark.parametrize("extract", [False, True])
def test_openai_compat_reader_extracts_only_when_asked(extract: bool) -> None:
    from memspine_evals.readers import DATED3_QA_PROMPT, OpenAICompatReader

    reply = "Line 3 names her second puppy.\nAnswer: Shadow"
    reader = OpenAICompatReader(model="m", prompt=DATED3_QA_PROMPT, extract_answer=extract)
    reader._httpx = _fake_httpx(reply)
    out = asyncio.run(reader.answer("What is the second puppy's name?", "[2023-05-08] x"))
    assert out.text == ("Shadow" if extract else reply)
    assert reader.describe().get("extract_answer", False) is extract


def test_litellm_reader_extracts_in_answer_not_in_complete() -> None:
    from types import SimpleNamespace

    from memspine_evals.bedrock import CallBudget, LiteLLMReader
    from memspine_evals.readers import DATED3_QA_PROMPT

    async def acompletion(**kwargs: object) -> object:
        message = SimpleNamespace(content="Line 2 says magical.\nAnswer: magical")
        return SimpleNamespace(
            usage=None, choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )

    reader = LiteLLMReader(
        CallBudget(max_calls=3), model="m", prompt=DATED3_QA_PROMPT, extract_answer=True
    )
    reader._litellm = SimpleNamespace(acompletion=acompletion)  # type: ignore[assignment]
    assert asyncio.run(reader.answer("How does dance feel?", "ctx")).text == "magical"
    # The judge path (complete) is never post-processed.
    assert asyncio.run(reader.complete("x")).text.endswith("Answer: magical")


@pytest.mark.parametrize("bedrock", [False, True])
@pytest.mark.parametrize("qa_prompt", ["dated", "dated_infer", "dated3"])
def test_build_reader_wires_the_reasoning_prompts(
    monkeypatch: pytest.MonkeyPatch, bedrock: bool, qa_prompt: str
) -> None:
    """#34: only a reasoning prompt extracts the final answer (and gets 512 tokens on
    Bedrock); the other prompts are unchanged."""
    import sys
    from types import SimpleNamespace

    from memspine_evals.experiments import C01Config, build_reader_and_judge

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    config = C01Config(
        mode="qa", bedrock=bedrock, max_model_calls=5, qa_prompt=qa_prompt, categories=(1, 2, 3, 4)
    )
    reader, _, _ = build_reader_and_judge(config)
    reasoning = qa_prompt == "dated3"
    assert reader.prompt == QA_PROMPTS[qa_prompt]  # type: ignore[attr-defined]
    assert reader.extract_answer is reasoning  # type: ignore[attr-defined]
    if bedrock:
        assert reader.max_tokens == (512 if reasoning else 256)  # type: ignore[attr-defined]


@pytest.mark.parametrize("qa_prompt", ["dated_infer", "dated3"])
@pytest.mark.parametrize("categories", ["1", "2", "3", "4", "1,2,3,4", "5", "all"])
def test_qa_prompt_is_selectable_for_every_category(qa_prompt: str, categories: str) -> None:
    """#59: ``dated_infer`` (and ``dated3``) apply to any LoCoMo category, not only cat 3:
    the prompt is one per run, with no per-category restriction."""
    from memspine_evals.cli import build_parser, parse_categories

    args = build_parser().parse_args(
        ["c0-1", "--dataset", "locomo", "--qa-prompt", qa_prompt, "--categories", categories]
    )
    assert args.qa_prompt == qa_prompt
    assert parse_categories(args.categories) in (None, *[(c,) for c in range(1, 6)], (1, 2, 3, 4))
