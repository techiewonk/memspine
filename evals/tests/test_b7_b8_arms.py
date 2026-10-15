"""B7 / B8 queue rows (FU-REQS): every arm is the generator's output, passes the strict arm validator
(schema, policy blocks, every flag prerequisite including the soft ones), every reference resolves,
every inert screen of the audit is re-armed or explicitly skipped, and every B8 arm carries its
profile. CPU only."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("memspine")
EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))
import gen_b7_arms as g
from memspine_evals.rehearsal import validate_engine_config

from memspine.config.requires import check_requires
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError

COLS = ["id", "batch", "pkg", "arm", "ref_run", "rule", "sets", "deps", "kind", "est_min", "flag"]
LINES = (EVALS / "v2_queue.tsv").read_text(encoding="utf-8").splitlines()
ROWS = [
    dict(zip(COLS, r, strict=False))
    for r in csv.reader([ln for ln in LINES if ln and not ln.startswith("#")], delimiter="\t")
]
SKIPS = [
    dict(zip(["mark", *COLS], r, strict=False))
    for r in csv.reader([ln for ln in LINES if ln.startswith("#SKIP")], delimiter="\t")
]
B7 = [r for r in ROWS if r["batch"] == "B7"]
B8 = [r for r in ROWS if r["batch"] == "B8"]
INERT = {"S17", "S18", "S21", "S22", "S23", "S24", "S27", "S46"}  # audit findings 3-8


@pytest.mark.parametrize("name", sorted(g.targets()))
def test_arm_matches_generator_and_passes_the_strict_validator(name: str) -> None:
    arm = json.loads((g.ARMS / f"{name}.json").read_text(encoding="utf-8"))
    assert arm == g.targets()[name]
    validate_engine_config(arm)


@pytest.mark.parametrize("row", B7 + B8, ids=lambda r: r["id"])
def test_row_resolves(row: dict) -> None:
    assert (g.ARMS / f"{row['arm']}.json").exists()
    assert row["rule"] in {"PROMOTE", "NONINF", "PARITY", "REPORT", "REF"}
    ref = row["ref_run"]
    if ref.startswith("v2-S"):
        rid = ref[3:-3]
        ref_row = next(r for r in ROWS if r["id"] == rid)
        assert ref_row["kind"] == row["kind"], "a reference must be built the same way (J or GC)"
        assert (g.ARMS / f"{ref_row['arm']}.json").exists()
    for d in filter(lambda x: x != "-", row["deps"].split(",")):
        ids = [r["id"] for r in ROWS]
        assert ids.index(d) < ids.index(row["id"])


def test_every_inert_screen_is_rearmed_or_skipped_with_a_reason() -> None:
    text = (
        " ".join(r["flag"] for r in B7) + " " + " ".join(s["id"] + " " + s["flag"] for s in SKIPS)
    )
    for sid in sorted(INERT):
        assert sid in text, f"{sid} is neither replaced in B7 nor marked #SKIP"
    for s in SKIPS:
        assert "skipped" in s["flag"] and len(s["flag"]) > 40, s["id"]


def test_deep_screens_bind_the_sufficiency_role() -> None:
    for name in ("deep", "obligations"):
        arm = json.loads((g.ARMS / f"scr-b7-{name}.json").read_text(encoding="utf-8"))
        assert "sufficiency" in arm["llm"]["roles"]
        assert arm["read"]["deep_v2"] is True
    arm = json.loads((g.ARMS / "scr-b7-deep.json").read_text(encoding="utf-8"))
    del arm["llm"]["roles"]["sufficiency"]
    with pytest.raises(ConfigError, match="sufficiency"):
        check_requires(MemspineConfig.model_validate(arm), strict=True)


def test_the_old_deep_arms_now_fail_the_strict_validator() -> None:
    for name in ("v2-R3-12-deep_v2", "v2-R3-16-obligations", "v2-R3-13-external_v2"):
        arm = json.loads((g.ARMS / f"{name}.json").read_text(encoding="utf-8"))
        with pytest.raises(ConfigError):
            validate_engine_config(arm)


def test_budget_pair_differs_only_in_exact_budget() -> None:
    ref = json.loads((g.ARMS / "scr-b7-tightbudget.json").read_text(encoding="utf-8"))
    arm = json.loads((g.ARMS / "scr-b7-exactbudget.json").read_text(encoding="utf-8"))
    assert ref["read"]["reply_reserve_tokens"] == arm["read"]["reply_reserve_tokens"] == 3072
    assert {k for k in arm["read"] if arm["read"][k] != ref["read"].get(k)} == {"exact_budget"}


@pytest.mark.parametrize("profile", sorted(g.B8))
def test_b8_arm_carries_its_profile(profile: str) -> None:
    arm = json.loads((g.ARMS / f"scr-b8-{profile}.json").read_text(encoding="utf-8"))
    for dotted in _leaves(g.preset(profile)):
        node, want = arm, g.preset(profile)
        for part in dotted.split("."):
            node, want = node[part], want[part]
        assert node == want, dotted
    assert any(r["arm"] == f"scr-b8-{profile}" and r["kind"] == "GC" for r in B8)


def test_every_profile_has_a_b8_row() -> None:
    assert {r["arm"] for r in B8} == {f"scr-b8-{p}" for p in g.B8}


def _leaves(node: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in node.items():
        out += _leaves(v, f"{prefix}{k}.") if isinstance(v, dict) else [f"{prefix}{k}"]
    return out
