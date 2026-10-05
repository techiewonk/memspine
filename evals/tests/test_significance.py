"""significance.py: paired delta, bootstrap CI, exact McNemar, resume/repeat merging."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest
import significance as sig


def _write_run(folder: Path, rows: Sequence[tuple[str, str, float | None, str]]) -> Path:
    """rows: (query_id, type_label, score, status)."""
    folder.mkdir(parents=True)
    lines = [json.dumps({"kind": "manifest", "run_id": folder.name})]
    for qid, cat, score, status in rows:
        lines.append(
            json.dumps(
                {
                    "kind": "result",
                    "query_id": qid,
                    "type_label": cat,
                    "score": score,
                    "status": status,
                }
            )
        )
    (folder / "results.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def test_mcnemar_exact_matches_the_binomial() -> None:
    assert sig.mcnemar_exact(0, 0) == 1.0
    # b=0, c=5: two-sided p = 2 * (1/2)^5
    assert sig.mcnemar_exact(0, 5) == pytest.approx(2 / 32)
    assert sig.mcnemar_exact(5, 0) == sig.mcnemar_exact(0, 5)
    # b=3, c=7, n=10: 2 * sum_{i<=3} C(10,i) / 1024
    expected = 2 * sum(math.comb(10, i) for i in range(4)) / 1024
    assert sig.mcnemar_exact(3, 7) == pytest.approx(expected)
    assert sig.mcnemar_exact(5, 5) == 1.0


def test_bootstrap_ci_is_seeded_and_brackets_the_mean() -> None:
    diffs = [1.0] * 30 + [0.0] * 60 + [-1.0] * 10
    one = sig.bootstrap_ci(diffs, resamples=2000, seed=7)
    two = sig.bootstrap_ci(diffs, resamples=2000, seed=7)
    assert one == two
    assert one[0] < 0.2 < one[1]
    assert one[0] > 0.0  # a clear positive shift excludes zero
    constant = sig.bootstrap_ci([0.0] * 20, resamples=500, seed=1)
    assert constant == (0.0, 0.0)


def test_paired_delta_and_discordant_counts(tmp_path: Path) -> None:
    a = _write_run(
        tmp_path / "p--base--memspine",
        [("q1", "cat1", 1.0, "completed"), ("q2", "cat1", 0.0, "completed"),
         ("q3", "cat2", 0.0, "completed"), ("q4", "cat2", 1.0, "completed")],
    )  # fmt: skip
    b = _write_run(
        tmp_path / "p--x--memspine",
        [("q1", "cat1", 1.0, "completed"), ("q2", "cat1", 1.0, "completed"),
         ("q3", "cat2", 1.0, "completed"), ("q4", "cat2", 0.0, "completed"),
         ("q5", "cat2", 1.0, "completed")],
    )  # fmt: skip
    result = sig.compare(sig.load_side([a]), sig.load_side([b]), resamples=200, seed=3)
    o = result.overall
    assert o.n == 4 and result.n_only_b == 1
    assert o.acc_a == pytest.approx(50.0) and o.acc_b == pytest.approx(75.0)
    assert o.delta == pytest.approx(25.0)
    assert (o.b, o.c) == (1, 2)
    assert result.by_category["cat1"].delta == pytest.approx(50.0)
    assert result.by_category["cat2"].delta == pytest.approx(0.0)


def test_resume_replaces_and_repeats_average(tmp_path: Path) -> None:
    orig = _write_run(
        tmp_path / "p--arm--memspine",
        [("q1", "cat1", None, "error"), ("q2", "cat1", 1.0, "completed")],
    )
    resume = _write_run(tmp_path / "p--arm-resume--memspine", [("q1", "cat1", 1.0, "completed")])
    rep1 = _write_run(
        tmp_path / "p--arm--r1--memspine",
        [("q1", "cat1", 0.0, "completed"), ("q2", "cat1", 1.0, "completed")],
    )
    rep2 = _write_run(
        tmp_path / "p--arm--r2--memspine",
        [("q1", "cat1", 0.0, "completed"), ("q2", "cat1", None, "error")],
    )
    side = sig.load_side([rep1, resume, orig, rep2])
    assert side.replicates == 3
    # q1: resume 1.0 (replaces the error), r1 0.0, r2 0.0 -> 1/3
    assert side.score["q1"] == pytest.approx(1 / 3)
    # q2: 1.0, 1.0, and r2's error scores 0 (C-7: one rule, single or repeated)
    assert side.score["q2"] == pytest.approx(2 / 3)
    assert side.errors == 1
    assert sig.arm_dirs(tmp_path, "p", "arm", "memspine") == sorted([orig, resume, rep1, rep2])


def test_unscored_everywhere_counts_as_wrong(tmp_path: Path) -> None:
    run = _write_run(tmp_path / "p--a--memspine", [("q1", "cat1", None, "error")])
    side = sig.load_side([run])
    assert side.score == {"q1": 0.0}
    assert side.errors == 1


def test_repeats_get_a_per_pair_mcnemar_range(tmp_path: Path) -> None:
    a = _write_run(tmp_path / "p--a--memspine", [("q1", "cat1", 1.0, "completed")])
    b1 = _write_run(tmp_path / "p--b--memspine", [("q1", "cat1", 1.0, "completed")])
    b2 = _write_run(tmp_path / "p--b--r1--memspine", [("q1", "cat1", 0.0, "completed")])
    result = sig.compare(sig.load_side([a]), sig.load_side([b1, b2]), resamples=50, seed=1)
    o = result.overall
    assert o.p_mcnemar is None and o.n_pairs == 2
    assert (o.b, o.c) == ((0, 1), (0, 0))  # pair 1: no discordance; pair 2: A right, B wrong
    assert o.mcnemar_range == (1.0, 1.0)
    assert o.delta == pytest.approx(-50.0)


def test_single_runs_keep_the_exact_mcnemar(tmp_path: Path) -> None:
    a = _write_run(
        tmp_path / "p--a--memspine", [(f"q{i}", "cat1", 0.0, "completed") for i in range(6)]
    )
    b = _write_run(
        tmp_path / "p--b--memspine", [(f"q{i}", "cat1", 1.0, "completed") for i in range(6)]
    )
    o = sig.compare(sig.load_side([a]), sig.load_side([b]), resamples=50, seed=1).overall
    assert (o.b, o.c) == (0, 6)
    assert o.p_mcnemar == pytest.approx(sig.mcnemar_exact(0, 6))
    assert o.mcnemar_range is None
    # the reported p is the sign-flip test: all six flips agree -> 2 / 64 exactly in the limit
    assert o.p == pytest.approx(2 / 64, abs=0.01)


def test_error_rows_score_zero_in_single_and_repeated_runs(tmp_path: Path) -> None:
    """C-7: an error row is 0 whether the side has one run or several, and is counted."""
    single = _write_run(
        tmp_path / "p--s--memspine",
        [("q1", "cat1", None, "error"), ("q2", "cat1", 1.0, "completed")],
    )
    rep0 = _write_run(
        tmp_path / "p--m--memspine",
        [("q1", "cat1", None, "error"), ("q2", "cat1", 1.0, "completed")],
    )
    rep1 = _write_run(
        tmp_path / "p--m--r1--memspine",
        [("q1", "cat1", None, "error"), ("q2", "cat1", 1.0, "completed")],
    )
    one, many = sig.load_side([single]), sig.load_side([rep0, rep1])
    assert one.score == many.score == {"q1": 0.0, "q2": 1.0}
    assert (one.errors, many.errors) == (1, 2)
    result = sig.compare(one, many, resamples=50, seed=1)
    assert result.overall.delta == pytest.approx(0.0)
    assert result.errors == (1, 2)
    text = sig.render_markdown([result], resamples=50, seed=1)
    assert "error rows scored 0: 1 in A, 2 in B" in text


def test_permutation_p_is_seeded_and_bounded() -> None:
    diffs = [1.0] * 20 + [0.0] * 30
    p1 = sig.permutation_p(diffs, permutations=2000, seed=3)
    assert p1 == sig.permutation_p(diffs, permutations=2000, seed=3)
    assert 1 / 2001 <= p1 < 0.01
    assert sig.permutation_p([0.0] * 10) == 1.0
    assert sig.permutation_p([1.0, -1.0] * 10, permutations=500, seed=1) > 0.5


def test_unequal_repeats_keep_the_test_size() -> None:
    """C-1: identical systems, one run vs three, 500 simulations: rejections <= 7%.

    Per-question accuracy is skewed high (Beta(2, 1), mean 0.67, like the LoCoMo arms);
    the majority-vote McNemar this replaces rejected 17% of the time here.
    """
    import numpy as np

    rng = np.random.default_rng(20261005)
    n_q, sims, rejections = 300, 500, 0
    qids = [f"q{i}" for i in range(n_q)]

    def side(label: str, runs: np.ndarray) -> sig.Side:
        per_rep = {
            str(r): dict(zip(qids, map(float, runs[r]), strict=True)) for r in range(len(runs))
        }
        return sig.Side(
            label=label,
            score=dict(zip(qids, map(float, runs.mean(axis=0)), strict=True)),
            category=dict.fromkeys(qids, "cat1"),
            replicates=len(runs),
            per_rep=per_rep,
        )

    for sim in range(sims):
        p_q = rng.beta(2.0, 1.0, size=n_q)  # per-question accuracy, shared by both
        a_runs = (rng.uniform(size=(1, n_q)) < p_q).astype(float)
        b_runs = (rng.uniform(size=(3, n_q)) < p_q).astype(float)
        result = sig.compare(
            side("a", a_runs), side("b", b_runs), resamples=20, seed=sim, permutations=1000
        )
        rejections += result.overall.p < 0.05
    assert rejections / sims <= 0.07


def test_holm_is_monotone_and_capped() -> None:
    adjusted = sig.holm([0.01, 0.04, 0.03, 0.5])
    assert adjusted == pytest.approx([0.04, 0.09, 0.09, 0.5])
    assert sig.holm([0.6, 0.9]) == pytest.approx([1.0, 1.0])


def test_parse_pairs_defaults_to_base() -> None:
    assert sig.parse_pairs(["H1", "combo-A:R-cohere"], "memspine-base") == [
        ("memspine-base", "H1"),
        ("combo-A", "R-cohere"),
    ]


def test_cli_writes_a_markdown_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs"
    rows_a = [(f"q{i}", "cat2", float(i % 2), "completed") for i in range(40)]
    rows_b = [(f"q{i}", "cat2", 1.0, "completed") for i in range(40)]
    _write_run(runs / "pp--memspine-base--memspine", rows_a)
    _write_run(runs / "pp--H1--memspine", rows_b)
    out = tmp_path / "table.md"
    code = sig.main(
        ["--runs", str(runs), "--prefix", "pp", "--pair", "H1", "--resamples", "300",
         "--out", str(out)]
    )  # fmt: skip
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "| memspine-base | H1 | 1/1 | 40 | 50.0 | 100.0 | **+50.0**" in text
    assert "cat2 temporal" in text
    assert "memspine-base | H1" in capsys.readouterr().out
