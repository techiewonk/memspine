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
