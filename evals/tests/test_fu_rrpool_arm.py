"""FU-RRPOOL queue row S67: the arm is R3-07 plus the search pool, validates, and its reference resolves. CPU only."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("memspine")
EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))
from memspine_evals.rehearsal import validate_engine_config

ARMS = EVALS / "arms"
COLS = ["id", "batch", "pkg", "arm", "ref_run", "rule", "sets", "deps", "kind", "est_min", "flag"]
ROWS = [
    dict(zip(COLS, r))
    for r in csv.reader(
        [
            ln
            for ln in (EVALS / "screen_queue.tsv").read_text(encoding="utf-8").splitlines()
            if ln and not ln.startswith("#")
        ],
        delimiter="\t",
    )
]


def _arm(name: str) -> dict:
    return json.loads((ARMS / f"{name}.json").read_text(encoding="utf-8"))


def test_arm_is_r3_07_plus_the_pool_and_validates() -> None:
    base, arm = _arm("R3-07-rerank"), _arm("R3-07b-rerank_pool")
    assert arm["model"]["rerank"].pop("search_pool") == 40
    assert arm == base
    validate_engine_config(_arm("R3-07b-rerank_pool"))


def test_row_s67_resolves() -> None:
    row = next(r for r in ROWS if r["id"] == "S67")
    assert (row["batch"], row["rule"], row["sets"], row["ref_run"]) == (
        "B9",
        "NONINF",
        "fo",
        "screen-S15-i2",
    )
    assert (ARMS / f"{row['arm']}.json").exists()
    assert next(r for r in ROWS if r["id"] == "S15")["kind"] == row["kind"]
