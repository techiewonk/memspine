"""judge_audit.py: stratified sample, resume merge, kappa and agreement scoring."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import judge_audit as ja
import pytest


def _run(folder: Path, rows: list[dict[str, object]]) -> Path:
    folder.mkdir(parents=True)
    lines = [json.dumps({"kind": "manifest"})] + [json.dumps({"kind": "result", **r}) for r in rows]
    (folder / "results.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return folder


def _rows(cat: str, n_right: int, n_wrong: int, start: int = 0) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for i in range(n_right + n_wrong):
        out.append(
            {
                "item_id": "conv-1",
                "query_id": f"{cat}-{start + i}",
                "type_label": cat,
                "question": f"q{i}?",
                "gold": "g",
                "answer": "a",
                "score": 1.0 if i < n_right else 0.0,
                "status": "completed",
                "meta": {"judge_raw": '{"label": "X"}'},
            }
        )
    return out


def test_sample_is_stratified_seeded_and_unmarked(tmp_path: Path) -> None:
    run = _run(tmp_path / "r--a--memspine", _rows("cat1", 40, 30) + _rows("cat2", 5, 3))
    rows = ja.load_rows([run])
    one = ja.draw_sample(rows, wrong=20, right=10, seed=3)
    two = ja.draw_sample(rows, wrong=20, right=10, seed=3)
    assert one == two
    counts: dict[tuple[str, str], int] = {}
    for r in one:
        counts[(r["category"], r["judge_verdict"])] = (
            counts.get((r["category"], r["judge_verdict"]), 0) + 1
        )
    assert counts == {("cat1", "correct"): 10, ("cat1", "wrong"): 20, ("cat2", "correct"): 5,
                      ("cat2", "wrong"): 3}  # fmt: skip
    assert all(r["human"] == "" and r["note"] == "" for r in one)
    assert {r["stratum_n"] for r in one if r["category"] == "cat1"} == {"40", "30"}
    assert [r["audit_id"] for r in one] == [f"A{i:03d}" for i in range(1, len(one) + 1)]
    out = tmp_path / "audit.csv"
    ja.write_csv(one, out)
    with out.open(encoding="utf-8", newline="") as fh:
        back = list(csv.DictReader(fh))
    assert tuple(back[0]) == ja.FIELDS and len(back) == len(one)


def test_resume_rows_replace_and_unscored_rows_are_dropped(tmp_path: Path) -> None:
    orig = _run(
        tmp_path / "r--a--memspine",
        [{"query_id": "q1", "type_label": "cat1", "score": None, "status": "error"},
         {"query_id": "q2", "type_label": "cat1", "score": 0.0, "status": "completed"}],
    )  # fmt: skip
    resume = _run(
        tmp_path / "r--a-resume--memspine",
        [{"query_id": "q1", "type_label": "cat1", "score": 1.0, "status": "completed"}],
    )
    rows = {r["query_id"]: r for r in ja.load_rows([resume, orig])}
    assert set(rows) == {"q1", "q2"} and rows["q1"]["score"] == 1.0


def test_cohen_kappa_known_values() -> None:
    perfect = [("correct", "correct"), ("wrong", "wrong")] * 5
    assert ja.cohen_kappa(perfect) == pytest.approx(1.0)
    # 2x2 table: a=20 (c,c), b=5 (c,w), c=10 (w,c), d=15 (w,w): po=0.7, pe=0.5 -> 0.4
    pairs = (
        [("correct", "correct")] * 20 + [("correct", "wrong")] * 5
        + [("wrong", "correct")] * 10 + [("wrong", "wrong")] * 15
    )  # fmt: skip
    assert ja.cohen_kappa(pairs) == pytest.approx(0.4)
    assert ja.cohen_kappa([("correct", "correct")] * 3) != ja.cohen_kappa(
        [("correct", "correct")] * 3
    )


def test_score_rows_agreement_kappa_and_weighting() -> None:
    def rec(i: int, cat: str, verdict: str, human: str, n: int) -> dict[str, str]:
        return {"audit_id": f"A{i:03d}", "category": cat, "judge_verdict": verdict,
                "human": human, "stratum_n": str(n)}  # fmt: skip

    records = [
        rec(1, "cat1", "wrong", "agree", 100),
        rec(2, "cat1", "wrong", "disagree", 100),
        rec(3, "cat1", "correct", "agree", 900),
        rec(4, "cat1", "correct", "agree", 900),
        rec(5, "cat1", "correct", "", 900),
        rec(6, "cat1", "correct", "maybe", 900),
    ]
    score = ja.score_rows(records)
    assert score.n_marked == 4 and score.n_unmarked == 1 and len(score.invalid) == 1
    assert score.agreement == pytest.approx(0.75)
    # judge: w w c c; human: w c c c -> po 0.75, pe = 0.5*0.25 + 0.5*0.75 = 0.5 -> 0.5
    assert score.kappa == pytest.approx(0.5)
    # strata: wrong 1/2 (n 100), correct 2/2 (n 900) -> (100*0.5 + 900*1) / 1000
    assert score.weighted_agreement == pytest.approx(0.95)
    assert score.by_stratum[("cat1", "wrong")] == (1, 2)


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _run(tmp_path / "r--a--memspine", _rows("cat4", 12, 25))
    out = tmp_path / "audit.csv"
    assert ja.main(["--run", str(run), "--out", str(out)]) == 0
    with out.open(encoding="utf-8", newline="") as fh:
        records = list(csv.DictReader(fh))
    assert len(records) == 30
    for r in records:
        r["human"] = "agree"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ja.FIELDS)
        writer.writeheader()
        writer.writerows(records)
    capsys.readouterr()
    assert ja.main(["--score", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "raw agreement 1.000" in printed and "Cohen's kappa 1.000" in printed
