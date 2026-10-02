"""H7/H12: QA prompt variants are well-formed and declared."""

from __future__ import annotations

from memspine_evals.readers import QA_PROMPTS


def test_variants_format_and_differ() -> None:
    rendered = {k: v.format(context="[2023-07-15] x", question="q?") for k, v in QA_PROMPTS.items()}
    assert set(rendered) == {"default", "dated", "abstain"}
    assert len(set(rendered.values())) == 3
    assert "Not mentioned" in rendered["abstain"]
    assert "computed from the line's date" in rendered["dated"]
