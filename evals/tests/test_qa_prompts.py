"""H7/H12 and benchmark QA prompt variants are well-formed, distinct and declared."""

from __future__ import annotations

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
