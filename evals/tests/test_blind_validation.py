"""V03 blind validation sets: cli wiring, frozen slices, aggregate-only scorer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))
import eval_blind  # noqa: E402
import freeze_blind_slices as fbs  # noqa: E402
from memspine_evals.cli import build_parser, parse_item_ids  # noqa: E402

MAB = EVALS / "analysis" / "blind_split_mab.json"
CM = EVALS / "analysis" / "blind_split_convomem.json"


# ---- cli wiring -----------------------------------------------------------------------------
def test_item_ids_comma_list_unchanged() -> None:
    assert parse_item_ids("a,b/1") == ("a", "b/1")
    assert parse_item_ids(None) is None and parse_item_ids("") is None


def test_item_ids_from_blind_split_file() -> None:
    assert parse_item_ids(f"@{MAB}") == tuple(json.loads(MAB.read_text())["blind_items"])
    assert len(parse_item_ids(f"@{CM}") or ()) == 200


def test_item_ids_from_plain_files(tmp_path: Path) -> None:
    (tmp_path / "l.txt").write_text("x\ny\n")
    (tmp_path / "l.json").write_text('["p", "q"]')
    assert parse_item_ids(f"@{tmp_path / 'l.txt'}") == ("x", "y")
    assert parse_item_ids(f"@{tmp_path / 'l.json'}") == ("p", "q")


def test_cli_accepts_both_datasets() -> None:
    p = build_parser()
    for ds in ("memoryagentbench", "convomem"):
        a = p.parse_args(["c0-1", "--dataset", ds, "--path", "d", "--item-ids", "@f"])
        assert a.dataset == ds and a.judge_prompt == "rubric"
    a = p.parse_args(["c0-1", "--dataset", "memoryagentbench", "--judge-prompt", "alias"])
    assert a.judge_prompt == "alias"


def test_run_sh_wires_judge_and_sampling() -> None:
    sh = (EVALS / "run.sh").read_text(encoding="utf-8")
    assert '[ -e "${data:-/nonexistent}" ]' in sh  # a directory is a valid --data
    assert "--judge-prompt alias" in sh and "--per-stratum 1000" in sh
    assert "needs --questions" in sh or "cannot derive the expected question count" in sh


@pytest.mark.skipif(not (EVALS / "data" / "convomem" / "core_benchmark").exists(), reason="ConvoMem not fetched")
def test_convomem_abstention_items_use_the_abstention_judge() -> None:
    from memspine_evals.datasets import ConvoMemDataset
    from memspine_evals.judge_prompts import JUDGE_SUITES

    qs = [i.queries[0] for i in ConvoMemDataset(EVALS / "data" / "convomem", "x", per_stratum=3).items()]
    flagged = {q.type_label.split("/")[0] for q in qs if q.meta.get("abstention")}
    assert flagged == {"abstention_evidence"}
    assert JUDGE_SUITES["rubric"].handles_abstention


# ---- slice freezing -------------------------------------------------------------------------
def test_blind_files_are_frozen_and_verified() -> None:
    assert fbs.check(MAB) == 0 and fbs.check(CM) == 0
    for path, n in ((MAB, 4), (CM, 200)):
        d = json.loads(path.read_text())
        assert d["blind"] is True and "BLIND" in d["status"] and "BLIND" in d["note"]
        assert len(d["blind_items"]) == n and d["blind_items"] == d["dev_items"]
        assert not set(d["blind_items"]) & set(d["heldout_items"])
        assert len(d["content_sha256"]) == 64 and len(d["split_sha256"]) == 64
        assert set(d) >= {"dataset_id", "revision_id", "seed", "unit"}  # ids and hashes only
    assert set(json.loads(MAB.read_text())["blind_items"]) == set(fbs.MAB_BLIND_ITEMS)


def test_edited_blind_file_is_detected(tmp_path: Path) -> None:
    d = json.loads(CM.read_text())
    d["dev_items"] = d["dev_items"][1:]
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        fbs.check(bad)


def test_convomem_pick_is_stratified_and_deterministic() -> None:
    ids = {f"t{t}": [f"t{t}/1/{i}" for i in range(50)] for t in range(6)}
    pick = fbs.convomem_pick(ids, 20)
    counts = [sum(x.startswith(f"t{t}/") for x in pick) for t in range(6)]
    assert sorted(counts) == [3, 3, 3, 3, 4, 4] and pick == fbs.convomem_pick(ids, 20)


def test_convomem_slice_covers_every_type() -> None:
    types = {i.split("/")[0] for i in json.loads(CM.read_text())["blind_items"]}
    assert len(types) == 6


def test_refuses_to_recut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        fbs.main(["--mab", "nowhere.parquet"])  # MAB_FILE exists: cut once


# ---- aggregate-only scorer ------------------------------------------------------------------
def _write(runs: Path, arm: str, key: str, correct: list[bool]) -> None:
    d = eval_blind.run_dir(runs, arm, key)
    d.mkdir(parents=True)
    with (d / "results.jsonl").open("w", encoding="utf-8") as f:
        for i, ok in enumerate(correct):
            row = {"kind": "result", "query_id": f"SECRET-{i}", "status": "completed",
                   "answer": "SECRET-ANSWER", "score": 1.0 if ok else 0.0,
                   "question": "SECRET-QUESTION", "gold": "SECRET-GOLD"}
            f.write(json.dumps(row) + "\n")


def test_compare_counts() -> None:
    mk = lambda xs: {str(i): {"status": "completed", "answer": "a", "score": float(x)} for i, x in enumerate(xs)}  # noqa: E731
    r = eval_blind.compare(mk([1, 1, 0, 0]), mk([1, 0, 1, 0]))
    assert (r["n"], r["gained"], r["lost"], r["net"]) == (4, 1, 1, 0)
    assert r["delta"] == 0 and r["guard_ok"]
    assert eval_blind.compare(mk([0] * 100), mk([1] * 100))["guard_ok"] is False
    assert eval_blind.compare({}, mk([1])) == {"n": 0}
    empty = {"0": {"status": "completed", "answer": " ", "score": 1.0}}
    assert not eval_blind.is_correct(empty["0"])


def test_scorer_prints_aggregates_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for key in ("mab", "cm"):
        _write(tmp_path, "new", key, [True] * 8 + [False] * 2)
        _write(tmp_path, "old", key, [True] * 6 + [False] * 4)
    assert eval_blind.main(["new", "--ref", "old", "--runs-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "MAB-CR" in out and "ConvoMem" in out and "CONFIRMS" in out and "+2/-0" in out
    for leaked in ("SECRET", "query_id", "type_label"):
        assert leaked not in out


def test_scorer_reports_missing_and_regression(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "new", "mab", [False] * 100)
    _write(tmp_path, "old", "mab", [True] * 100)
    eval_blind.main(["new", "--ref", "old", "--runs-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "ConvoMem: run missing" in out and "REJECT (guard)" in out
    assert eval_blind.verdict([]) == "NO DATA"
