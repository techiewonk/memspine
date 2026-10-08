"""N57 re-judge: vendor judge prompts render their own placeholders; summary arithmetic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memspine_evals.judge_prompts import JUDGE_PROMPTS, JUDGE_SUITES, PromptStatus
from memspine_evals.rejudge import load_answers, main, summarise

VENDOR = ("vendor/mem0-generous", "vendor/mem0-unified", "vendor/evermemos")


@pytest.mark.parametrize("pid", VENDOR)
def test_vendor_prompt_renders_question_gold_and_answer(pid: str) -> None:
    prompt = JUDGE_PROMPTS[pid]
    text = prompt.render("QQ-question", "GG-gold", "AA-answer")
    assert "QQ-question" in text and "GG-gold" in text and "AA-answer" in text
    assert "{" not in text.replace("{\n", "")  # no placeholder left unfilled
    assert prompt.status is PromptStatus.VENDORED
    assert prompt.system


def test_vendor_suites_are_registered() -> None:
    for suite in ("mem0-generous", "mem0-unified", "evermemos"):
        assert suite in JUDGE_SUITES


def test_vendor_replies_parse() -> None:
    prompt = JUDGE_PROMPTS["vendor/mem0-generous"]
    assert prompt.parse_reply('{"reasoning": "same date", "label": "CORRECT"}') == 1.0
    assert prompt.parse_reply('{"reasoning": "not correct", "label": "WRONG"}') == 0.0


def _row(qid: str, score: float, cat: str = "cat1") -> dict:
    return {
        "kind": "result",
        "run_id": "r1",
        "item_id": "conv-1",
        "query_id": qid,
        "question": "q",
        "gold": "g",
        "answer": "a",
        "score": score,
        "status": "completed",
        "type_label": cat,
    }


def test_load_answers_keeps_completed_gold_rows(tmp_path: Path) -> None:
    p = tmp_path / "results.jsonl"
    rows = [{"kind": "manifest"}, _row("0", 1.0), {**_row("1", 0.0), "gold": None}]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf8")
    assert [r["query_id"] for r in load_answers(p)] == ["0"]


def test_summarise_counts_flips_against_the_original_judge() -> None:
    graded = [
        {"run_id": "r1", "suite": "s", "type_label": "cat1", "score": 1.0, "original_score": 0.0},
        {"run_id": "r1", "suite": "s", "type_label": "cat2", "score": 1.0, "original_score": 1.0},
        {"run_id": "r1", "suite": "s", "type_label": "cat2", "score": None, "original_score": 1.0},
    ]
    out = summarise(graded)
    assert out["by_suite_run"]["s|r1"]["all"] == {"acc": 100.0, "n": 2}
    assert out["original_by_run"]["r1"]["all"] == {"acc": 50.0, "n": 2}
    assert out["flips_vs_original"]["s"] == {"wrong_to_correct": 1, "correct_to_wrong": 0}
    assert out["errors"] == 1


def test_real_run_refuses_without_a_call_cap(tmp_path: Path) -> None:
    p = tmp_path / "results.jsonl"
    p.write_text(json.dumps(_row("0", 1.0)), encoding="utf8")
    with pytest.raises(SystemExit):
        main(["--run", str(p), "--suites", "mem0-generous", "--out-dir", str(tmp_path / "o")])


def test_dry_run_makes_no_call(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "results.jsonl"
    p.write_text(json.dumps(_row("0", 1.0)), encoding="utf8")
    assert (
        main(
            [
                "--run",
                str(p),
                "--suites",
                "mem0-generous,evermemos",
                "--dry-run",
                "--out-dir",
                str(tmp_path / "o"),
            ]
        )
        == 0
    )
    assert "calls pending=2" in capsys.readouterr().out
    assert not (tmp_path / "o").exists()
