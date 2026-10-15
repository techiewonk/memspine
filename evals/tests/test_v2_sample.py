"""RUNBOOK2: sample slices, results ledger, descriptive sample scoring and the queue runner's three modes (dry run)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import eval_sample
import make_sample_slice as mss
import pytest
import v2_ledger
from memspine_evals.cli import parse_query_slice_id
from memspine_evals.manifest import build_identity

EVALS = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def synthetic(n_conv=5, cats=("c1", "c2", "c3", "c4"), per=20):
    """(id, category, conversation) records: skewed so a naive random draw would miss the small category."""
    recs = []
    for ci in range(n_conv):
        for cat in cats:
            k = 2 if cat == "c4" else per
            recs += [(f"conv-{ci}/{cat}-{i}", cat, f"conv-{ci}") for i in range(k)]
    return recs


# ---------------------------------------------------------------- slices
def test_sample_is_deterministic_and_order_independent():
    recs = synthetic()
    a = mss.stratified_sample(recs, 40, seed=1)
    assert a == mss.stratified_sample(list(reversed(recs)), 40, seed=1)
    assert a != mss.stratified_sample(recs, 40, seed=2)
    assert len(a) == len(set(a)) == 40


def test_every_category_and_conversation_appears():
    recs = synthetic()
    cat_of = {q: c for q, c, _ in recs}
    conv_of = {q: v for q, _, v in recs}
    got = mss.stratified_sample(recs, 40, seed=3, min_per_category=4)
    counts = Counter(cat_of[q] for q in got)
    assert set(counts) == {"c1", "c2", "c3", "c4"}
    assert counts["c4"] >= 4  # the rare category keeps its floor although proportional share is ~1
    assert {conv_of[q] for q in got} == {f"conv-{i}" for i in range(5)}


def test_small_n_and_tiny_category_edge_cases():
    recs = synthetic(per=3)
    assert len(mss.stratified_sample(recs, 5, seed=1)) == 5  # n below categories x floor
    assert len(mss.stratified_sample(recs, 10_000, seed=1)) == len(recs)  # n capped at the universe


def test_build_from_run_dir_and_hashes(tmp_path):
    run = tmp_path / "src--memspine"
    run.mkdir()
    rows = [{"kind": "manifest"}] + [
        {
            "kind": "result",
            "item_id": f"conv-{i % 4}",
            "query_id": f"0-{i}",
            "type_label": f"cat{i % 3}",
            "question": "SECRET TEXT",
            "gold": "SECRET",
            "answer": "SECRET",
        }
        for i in range(120)
    ]
    (run / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    sl = mss.build("loc", tmp_path, "src", 30, seed=5, min_per_category=4)
    assert (
        sl["n"] == 30
        and sl["slice_id"] == "sample-loc-30-s5"
        and set(sl["sizes"]) == {"cat0", "cat1", "cat2"}
    )
    assert "SECRET" not in json.dumps(sl)  # ids only, never benchmark text
    assert sl["hash"] == mss.digest(sl["query_ids"])
    # the 16-hex hash equals what the run manifest computes for the same ids (so it enters the comparability key)
    ident = build_identity(slice_id=sl["slice_id"], slice_ids=sl["query_ids"], environ={})
    assert ident["datasets"]["slice_hash"] == sl["manifest_hash"]
    gc = mss.build("loc", tmp_path, "src", 30, seed=5, min_per_category=4, convs=2)
    assert len(gc["conversations"]) == 2 and gc["slice_id"].endswith("-c2")


@pytest.mark.parametrize(
    "name",
    [
        "sample_slice_loc_80_s20261013.json",
        "sample_slice_loc_60_s20261013_c3.json",
        "sample_slice_opb_60_s20261013.json",
    ],
)
def test_committed_slices(name):
    sl = json.loads((EVALS / "analysis" / name).read_text(encoding="utf-8"))
    ids = sl["query_ids"]
    assert len(ids) == sl["n"] == len(set(ids)) and all("/" in q for q in ids)
    assert sl["hash"] == mss.digest(ids) and sl["manifest_hash"] == mss.manifest_hash(ids)
    assert set(sl["category"].values()) == set(sl["sizes"]) and len(sl["sizes"]) >= 4
    assert set(sl) == {
        "benchmark",
        "slice_id",
        "kind",
        "source_run",
        "seed",
        "stratify",
        "n_total",
        "n",
        "conversations",
        "sizes",
        "hash",
        "manifest_hash",
        "category",
        "query_ids",
    }  # no text fields


def test_slice_id_reaches_the_manifest(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"slice_id": "sample-x", "query_ids": ["a/1"]}), encoding="utf-8")
    assert parse_query_slice_id(f"@{p}") == "sample-x"
    assert parse_query_slice_id("a/1,b/2") is None
    old = tmp_path / "focus.json"
    old.write_text(json.dumps({"query_ids": ["a/1"]}), encoding="utf-8")
    assert parse_query_slice_id(f"@{old}") is None  # old focus slices keep their comparability keys


# ---------------------------------------------------------------- descriptive scoring
def test_describe_counts_and_interval():
    ids = [f"c/{i}" for i in range(10)]
    cat = {q: ("a" if i < 6 else "b") for i, q in enumerate(ids)}
    ref = {q: 0.0 for q in ids}
    arm = {q: (1.0 if i in (0, 1, 7) else 0.0) for i, q in enumerate(ids)}
    arm[ids[2]], ref[ids[2]] = 0.0, 1.0
    r = eval_sample.describe(arm, ref, ids, cat)
    assert (r["wins"], r["losses"], r["ties"], r["net"], r["n"]) == (3, 1, 6, 2, 10)
    assert r["delta"] == pytest.approx(20.0) and r["ci_low"] <= r["delta"] <= r["ci_high"]
    assert r["by_category"] == {"a": (2, 1, 3), "b": (1, 0, 3)}
    assert (
        eval_sample.describe(arm, {}, ids, cat)["n"] == 0
    )  # missing reference rows: nothing is compared


def test_describe_graded_ties():
    r = eval_sample.describe(
        {"x/1": 0.52, "x/2": 0.9}, {"x/1": 0.5, "x/2": 0.5}, ["x/1", "x/2"], {}, tie=0.05
    )
    assert (r["wins"], r["losses"], r["ties"]) == (1, 0, 1)


# ---------------------------------------------------------------- ledger
def row(
    screen,
    net,
    mode="sample",
    arm="a",
    commit="abc1234",
    ts="2026-10-13T10:00:00",
    digest="d1",
    lo=-1.0,
):
    return {
        "ts": ts,
        "mode": mode,
        "screen": screen,
        "arm": arm,
        "arm_digest": digest,
        "reference": "ref",
        "commit": commit,
        "slice_id": "sample-loc-80-s1",
        "slice_hash": "h",
        "n": 80,
        "wins": net + 2,
        "losses": 2,
        "ties": 76,
        "net": net,
        "delta": net * 1.25,
        "ci_low": lo,
        "ci_high": 5.0,
        "verdict": "DESCRIPTIVE",
    }


def test_ledger_append_summary_history_mark(tmp_path):
    path = tmp_path / "v2_results.tsv"
    v2_ledger.append(row("S01", 1), path)
    v2_ledger.append(row("S02", 5), path)
    v2_ledger.append(
        row("S01", 3, ts="2026-10-13T11:00:00", digest="d2"), path
    )  # S01 iterated: arm changed
    v2_ledger.append(row("S03", 3, mode="focus", lo=0.5), path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert (
        lines[0].split("\t") == list(v2_ledger.COLUMNS) and len(lines) == 5
    )  # header once, append-only
    rows = v2_ledger.read(path)
    best = v2_ledger.summary(rows, mode="sample")
    assert [r["screen"] for r in best] == ["S02", "S01"] and best[1][
        "net"
    ] == "3"  # latest S01 only, sorted by net
    both = v2_ledger.summary(rows)  # net tie S01/S03: the higher interval low end first
    assert [r["screen"] for r in both] == ["S02", "S03", "S01"]
    hist = v2_ledger.history(rows, "S01")
    assert [r["arm_digest"] for r in hist] == ["d1", "d2"]
    with pytest.raises(KeyError):
        v2_ledger.append({"bogus": 1}, path)


def test_ledger_decisions_latest_wins(tmp_path):
    path = tmp_path / "v2_decisions.tsv"
    v2_ledger.mark("S01", "iterate", "try K=8", path)
    v2_ledger.mark("S02", "promote_candidate", path=path)
    v2_ledger.mark("S01", "promote_candidate", path=path)
    v2_ledger.mark("S02", "drop", path=path)
    assert v2_ledger.promote_candidates(path) == ["S01"]
    assert v2_ledger.decisions(path)["S02"]["decision"] == "drop"
    with pytest.raises(ValueError):
        v2_ledger.mark("S03", "adopt", path=path)


def test_ledger_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("V2_LEDGER_DIR", str(tmp_path))
    assert v2_ledger.main(["summary"]) == 0 and "no results yet" in capsys.readouterr().out
    v2_ledger.append(row("S09", 2))
    assert v2_ledger.main(["mark", "S09", "promote_candidate", "good"]) == 0
    assert v2_ledger.main(["summary", "--mode", "sample"]) == 0
    out = capsys.readouterr().out
    assert "S09" in out and "promote_candidate" in out and "verdict: PROMOTE" not in out
    assert v2_ledger.main(["history", "S09"]) == 0
    assert (
        v2_ledger.main(["record", "--mode", "final", "--screen", "S09", "--verdict", "CONFIRMS"])
        == 0
    )
    assert v2_ledger.read()[-1]["verdict"] == "CONFIRMS"


# ---------------------------------------------------------------- runner dry runs
def dry(mode, only, tmp_path, decisions=None):
    env = {
        **os.environ,
        "PINNED_DRY_RUN": "1",
        "V2_LEDGER_DIR": str(tmp_path),
        "V2_SLICE_MAP": "",
    }  # "": random slices only (FAIL-SLICE tests: test_fail_slices.py)
    if mode:
        env["MODE"] = mode
    else:
        env.pop("MODE", None)
    if decisions:
        for s, d in decisions.items():
            v2_ledger.mark(s, d, path=tmp_path / "v2_decisions.tsv")
    p = subprocess.run(
        [BASH, str(EVALS / "run_v2_queue.sh"), "--only", only],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        cwd=EVALS,
    )
    assert p.returncode == 0, p.stderr
    return p.stdout


needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@needs_bash
def test_runner_default_is_sample_and_never_blind(tmp_path):
    out = dry(None, "S10,S12,S13,S20", tmp_path)  # S13/S20 have blind sets in their full-gate rows
    assert "mode=sample" in out
    assert "run_blind_validation" not in out and "eval_blind" not in out
    assert "eval_focus" not in out and "eval_paired" not in out and "focus_slice" not in out
    assert (
        "verdict: PROMOTE" not in out and "set f verdict" not in out
    )  # no gate verdicts in sample mode
    assert out.count("eval_sample.py") == 5  # S10, S12, S13 once, S20 twice (LoCoMo + OP-Bench)
    assert "sample_slice_loc_80" in out and "sample_slice_opb_60" in out
    # the reference run is built once and reused by every arm
    assert (
        out.count("building reference smp-ref-scr-pool-protect-jina-") == 2
    )  # one per slice (LoCoMo, OP-Bench)
    assert "cached (built earlier in this queue, reused)" in out


@needs_bash
def test_runner_sample_uses_small_slice_for_engine_llm_rows(tmp_path):
    out = dry("sample", "S02", tmp_path)
    assert "sample_slice_loc_60_s20261013_c3" in out and "--memspine-build-sleep" in out
    assert "run_blind_validation" not in out


@needs_bash
def test_runner_focus_mode_keeps_paired_focus_screens(tmp_path):
    out = dry("focus", "S13,S05", tmp_path)
    assert "mode=focus" in out and "focus_slice_r7protect" in out and "eval_focus.py" in out
    assert (
        "eval_sample" not in out and "run_blind_validation" not in out and "eval_paired" not in out
    )
    assert "S05 (P0-02) focus: no focus-slice set in this row; skipped" in out


@needs_bash
def test_runner_final_only_promote_candidates_reach_blind(tmp_path):
    out = dry(
        "final", "S10,S13,S20", tmp_path, decisions={"S13": "promote_candidate", "S10": "drop"}
    )
    assert "S10 (R3-01) final: not a promote_candidate (decision: drop)" in out
    assert "S20 (R3-11) final: not a promote_candidate (decision: none)" in out
    assert "v2-R3-05a-entity_leg_v2 --topk 10 --force" in out  # S13's blind run
    assert (
        out.count("eval_blind.py") == 1
        and "v2-R3-01-tiers_v2" not in out
        and "v2-R3-11-gate_v2" not in out
    )
    assert (
        "eval_paired.py v2-S13-full" in out
        and "focus_slice" not in out
        and "eval_sample" not in out
    )


@needs_bash
def test_runner_rejects_unknown_mode(tmp_path):
    env = {**os.environ, "PINNED_DRY_RUN": "1", "MODE": "bogus"}
    p = subprocess.run(
        [BASH, str(EVALS / "run_v2_queue.sh")], capture_output=True, text=True, env=env, cwd=EVALS
    )
    assert p.returncode == 2 and "MODE must be" in p.stderr
