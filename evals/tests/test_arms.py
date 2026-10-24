"""Every active screen arm uses the current base configuration without pending aliases."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("memspine")
from memspine_evals.rehearsal import validate_engine_config

EVALS = Path(__file__).resolve().parents[1]
ARMS = EVALS / "arms"
QUEUE = EVALS / "screen_queue.tsv"
COLUMNS = [
    "id",
    "batch",
    "pkg",
    "arm",
    "ref_run",
    "rule",
    "sets",
    "deps",
    "kind",
    "est_min",
    "flag",
]
ROWS = [
    dict(zip(COLUMNS, row, strict=True))
    for row in csv.reader(
        [
            line
            for line in QUEUE.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ],
        delimiter="\t",
    )
]


def test_queue_is_well_formed() -> None:
    ids = [row["id"] for row in ROWS]
    assert len(ids) == len(set(ids))
    for row in ROWS:
        assert row["rule"] in {"PROMOTE", "NONINF", "PARITY", "REPORT", "REF"}
        assert set(row["sets"]) <= set("foFb")
        assert row["kind"] in {"J", "GC"}
        assert int(row["est_min"]) > 0
        assert (ARMS / f"{row['arm']}.json").is_file()
        for dependency in row["deps"].split(","):
            if dependency != "-":
                assert dependency in ids
                assert ids.index(dependency) < ids.index(row["id"])


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        assert key not in result, f"duplicate JSON configuration key: {key}"
        result[key] = value
    return result


@pytest.mark.parametrize("path", sorted(ARMS.glob("*.json")), ids=lambda path: path.stem)
def test_arm_validates(path: Path) -> None:
    arm = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    validate_engine_config(arm)
    assert not {"embedding", "vector", "cache", "models"} & arm.keys()
    assert not {"path", "provider", "settings", "profile"} & arm.get("storage", {}).keys()
    assert "roles" not in arm.get("llm", {})
