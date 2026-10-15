"""P0-01: manifests reach the paired gate from every caller, so INCOMPARABLE holds in practice. Synthetic data only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import eval_blind
import eval_focus
import eval_paired
import pytest
from memspine_evals.manifest import build_identity


def manifest(judge_model: str = "j1", flags: dict | None = None, commit: str = "c1") -> dict:
    return {
        "kind": "manifest",
        "dataset": {"dataset_id": "synthetic", "revision_id": "r1", "content_sha256": "aa"},
        "judge": {"judge_id": "j", "scale": "binary", "model": judge_model, "prompt_hash": "ph"},
        "reader": {"model": "rd"},
        "harness_version": "1.0",
        "protocol": {"budget_tokens": 4096, "top_k": 10, "seed": 1},
        "identity": build_identity(
            flags=flags or {"a": 1},
            slice_id="s",
            slice_ids=["x"],
            environ={"MEMSPINE_PINNED_COMMIT": commit},
        ),
    }


def write_run(runs: Path, run_dir_name: str, correct: dict[str, bool], man: dict | None) -> None:
    d = runs / run_dir_name
    d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(man)] if man is not None else []
    for q, ok in correct.items():
        item, qid = q.split("/")
        lines.append(
            json.dumps(
                {
                    "kind": "result",
                    "item_id": item,
                    "query_id": qid,
                    "status": "completed",
                    "answer": "a",
                    "score": 1.0 if ok else 0.0,
                    "type_label": "t",
                }
            )
        )
    (d / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def ids(n_conv: int = 6, per: int = 20) -> list[str]:
    return [f"c{c}/q{i}" for c in range(n_conv) for i in range(per)]


def _blind(
    runs: Path, arm: str, correct: bool, man: dict | None, sets=("mab", "cm"), qids=None
) -> None:
    for key in sets:
        write_run(runs, f"bv-{arm}-{key}--memspine", {q: correct for q in (qids or ids())}, man)


@pytest.fixture(autouse=True)
def _no_real_slices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        eval_blind, "HERE", tmp_path / "nowhere"
    )  # slice hash -> "unknown"; no real files read


# ---- eval_paired ---------------------------------------------------------------------------
def test_paired_incomparable_when_judges_differ(tmp_path: Path, capsys) -> None:
    q = ids()
    write_run(tmp_path, "arm--memspine", {x: True for x in q}, manifest("j1"))
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, manifest("j2"))
    assert eval_paired.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "verdict: INCOMPARABLE" in out and "judge.model" in out


def test_paired_comparable_runs_still_promote(tmp_path: Path, capsys) -> None:
    q = ids()
    write_run(tmp_path, "arm--memspine", {x: True for x in q}, manifest(flags={"a": 2}))
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, manifest())
    eval_paired.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "verdict: PROMOTE" in out and "WARNING" not in out


def test_paired_run_without_manifest_warns_but_scores(tmp_path: Path, capsys) -> None:
    q = ids()
    write_run(tmp_path, "arm--memspine", {x: True for x in q}, manifest())
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, None)
    eval_paired.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "comparability not verified" in out and "verdict: PROMOTE" in out


def test_arm_flags_and_commit_do_not_make_a_pair_incomparable(tmp_path: Path, capsys) -> None:
    q = ids()
    write_run(
        tmp_path, "arm--memspine", {x: True for x in q}, manifest(flags={"z": 9}, commit="other")
    )
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, manifest())
    eval_paired.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    assert "INCOMPARABLE" not in capsys.readouterr().out


# ---- eval_focus ----------------------------------------------------------------------------
def _slice(tmp_path: Path, q: list[str]) -> Path:
    sl = {
        "source_run": "ref",
        "hash": "0123456789abcdef",
        "fail": q[:60],
        "control": q[60:],
        "category": {x: "cat1" for x in q},
        "n_correct_total": 100,
        "n_total": len(q),
    }
    p = tmp_path / "slice.json"
    p.write_text(json.dumps(sl), encoding="utf-8")
    return p


def test_focus_incomparable_and_missing_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    q = ids()
    sl = _slice(tmp_path, q)
    write_run(tmp_path, "arm--memspine", {x: True for x in q}, manifest("j1"))
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, manifest("j2"))
    argv = ["eval_focus.py", "arm", "--ref", "ref", "--slice", str(sl), "--runs-dir", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    eval_focus.main()
    assert "verdict: INCOMPARABLE" in capsys.readouterr().out
    write_run(tmp_path, "ref--memspine", {x: False for x in q}, None)
    eval_focus.main()
    out = capsys.readouterr().out
    assert "comparability not verified" in out and "verdict: PROMOTE" in out


# ---- eval_blind ----------------------------------------------------------------------------
def test_blind_incomparable_prints_no_aggregate_and_records_nothing(tmp_path: Path, capsys) -> None:
    _blind(tmp_path, "arm", True, manifest("j1"))
    _blind(tmp_path, "ref", False, manifest("j2"))
    assert eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "verdict: INCOMPARABLE" in out and "gained" not in out and "net " not in out
    assert not (tmp_path / "_logs").exists()


def test_blind_missing_set_is_incomplete_and_unrecorded(tmp_path: Path, capsys) -> None:
    _blind(tmp_path, "arm", True, manifest(), sets=("mab",))
    _blind(tmp_path, "ref", False, manifest())
    eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "verdict: INCOMPLETE" in out and " vs " not in out.split("verdict")[0].replace(
        "run missing", ""
    )
    assert not (tmp_path / "_logs").exists()


def test_blind_missing_questions_are_incomplete(tmp_path: Path, capsys) -> None:
    _blind(tmp_path, "arm", True, manifest())
    _blind(tmp_path, "ref", False, manifest())
    _blind(tmp_path, "arm", True, manifest(), sets=("cm",), qids=ids()[:-5])
    eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    assert "verdict: INCOMPLETE" in capsys.readouterr().out
    assert not (tmp_path / "_logs").exists()


def test_blind_scores_without_manifest_but_warns(tmp_path: Path, capsys) -> None:
    _blind(tmp_path, "arm", True, None)
    _blind(tmp_path, "ref", False, None)
    eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "comparability not verified" in out and "verdict: CONFIRMS" in out


def test_blind_never_prints_question_ids(tmp_path: Path, capsys) -> None:
    _blind(tmp_path, "arm", True, manifest())
    _blind(tmp_path, "ref", False, manifest())
    eval_blind.main(["arm", "--ref", "ref", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "c0/" not in out and "q1" not in out


def test_verdict_incomparable_outranks_the_rest() -> None:
    assert (
        eval_blind.verdict([{"n": 5, "incomparable": ["x"]}, {"n": 9, "guard_ok": False}])
        == "INCOMPARABLE"
    )
