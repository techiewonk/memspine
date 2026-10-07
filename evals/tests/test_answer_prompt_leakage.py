"""M-a (plan v3.2): answer prompts may read only the context, the question and its date.

BEAM's public leaderboard was inflated by harnesses that put gold hints (the answer, the
evidence ids, the rubric) into the answer prompt. Every QA prompt here, fixed or routed,
may name only these placeholders; a new placeholder fails this test until it is reviewed.
"""

from __future__ import annotations

import string

import pytest

from memspine_evals.readers import QA_PROMPTS, ROUTED_QA_VARIANTS

ALLOWED = {"context", "question", "question_date"}
#: Words that would mean a gold field reached the reader's prompt.
GOLD_HINTS = ("{answer", "{gold", "{golden", "{evidence", "{rubric", "{reference", "{label")


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


ALL_PROMPTS = {
    **{f"qa:{name}": text for name, text in QA_PROMPTS.items()},
    **{f"routed:{name}": text for name, text in ROUTED_QA_VARIANTS.items()},
}


@pytest.mark.parametrize("name", sorted(ALL_PROMPTS))
def test_answer_prompt_reads_only_context_question_and_date(name: str) -> None:
    template = ALL_PROMPTS[name]
    assert _fields(template) <= ALLOWED, (name, _fields(template) - ALLOWED)
    lowered = template.lower()
    assert not any(hint in lowered for hint in GOLD_HINTS), name
