"""B5/B6 queue rows (Q-B5): every arm is the generator's gc3 + flags output, validates, and every reference resolves. CPU only."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("memspine")
EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))
import gen_b5_arms as g
from memspine_evals.rehearsal import validate_engine_config

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
B5 = [r for r in ROWS if r["batch"] == "B5"]


@pytest.mark.parametrize("name", sorted(g.OVERLAYS))
def test_arm_matches_generator_and_validates(name: str) -> None:
    arm = json.loads((g.ARMS / f"scr-gc3-{name}.json").read_text(encoding="utf-8"))
    assert arm == g.build(name)
    validate_engine_config(arm)


@pytest.mark.parametrize("row", B5 + [r for r in ROWS if r["batch"] == "B6"], ids=lambda r: r["id"])
def test_row_resolves(row: dict) -> None:
    assert (g.ARMS / f"{row['arm']}.json").exists()
    ref = row["ref_run"]
    if ref.startswith("screen-S"):
        rid = ref.removeprefix("screen-").removesuffix("-i2")
        ref_row = next(r for r in ROWS if r["id"] == rid)
        assert ref_row["kind"] == row["kind"]
        assert (g.ARMS / f"{ref_row['arm']}.json").exists()
