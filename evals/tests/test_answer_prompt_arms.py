"""N46 / N56 / N65 answer-prompt arms (QA only; paid runs need approval)."""

from __future__ import annotations

import pytest
from memspine_evals.readers import (
    ANSWER_EXTRACTOR_VERSION,
    QA_PROMPTS,
    REASONING_MAX_TOKENS,
    REASONING_QA_PROMPTS,
    final_answer,
    reasoning_max_tokens,
)


@pytest.mark.parametrize("name", ["dated_planned", "dated_noabstain", "evermemos_cot"])
def test_prompt_renders_context_and_question(name: str) -> None:
    text = QA_PROMPTS[name].format(context="CTX-LINE", question="Q-TEXT?")
    assert "CTX-LINE" in text and "Q-TEXT?" in text


def test_planned_clause_and_no_abstain_clause() -> None:
    assert "treat it as done" in QA_PROMPTS["dated_planned"]
    assert "say you do not know" not in QA_PROMPTS["dated_noabstain"]
    assert "never reply that you do not know" in QA_PROMPTS["dated_noabstain"]


def test_evermemos_is_a_reasoning_prompt_with_room_for_seven_steps() -> None:
    assert "evermemos_cot" in REASONING_QA_PROMPTS
    assert reasoning_max_tokens("evermemos_cot") > REASONING_MAX_TOKENS
    assert reasoning_max_tokens("dated3") == REASONING_MAX_TOKENS
    assert "STEP 7: FINAL ANSWER" in QA_PROMPTS["evermemos_cot"]


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        ("## STEP 6: checks\n## STEP 7: FINAL ANSWER\nAna went camping on 7 May 2023.\n", None),
        ("**STEP 7: FINAL ANSWER**: The Hobbit", "The Hobbit"),
        ("reasoning here\nAnswer: Paris", "Paris"),
    ],
)
def test_final_answer_reads_the_step_7_section(reply: str, answer: str | None) -> None:
    expected = answer or "Ana went camping on 7 May 2023."
    assert final_answer(reply) == expected


def test_extractor_version_bumped_for_the_new_marker() -> None:
    assert ANSWER_EXTRACTOR_VERSION == "v3"
