"""v2 screen queue: every queued arm parses and validates against the engine config schema.

Flags that the wiring agents have not merged yet are listed in ``PENDING_FLAGS``. An arm that fails ONLY because of
those keys is skipped and the skip reason names them, so the test run reports exactly which flags are still missing.
Any other failure (a misspelt existing key, a bad value, an arm that is not base + its flags) fails the test.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.rehearsal import validate_engine_config  # noqa: E402

EVALS = Path(__file__).resolve().parents[1]
ARMS = EVALS / "arms"
QUEUE = EVALS / "v2_queue.tsv"
BASE = "scr-pool-protect-jina"
#: dotted config paths named by the plan that are being wired now (read.*, graph.*)
PENDING_FLAGS = (
    "read.tiers_v2", "read.contract_rules", "read.router_rules", "read.family_fusion",
    "read.lift_before_rerank", "read.rerank_v2", "read.exact_budget", "read.evidence_bundles",
    "read.trust_rendering", "read.gate_v2", "read.deep_v2", "read.obligations", "read.timeview_v2",
    "read.entity_leg_v2", "read.neighbours_v2", "read.cache.enabled", "read.parallel_legs",
    "read.external_v2", "graph.key_v2", "read.rerank_v2_search_pool",
    # R14 builds (S69-S87)
    "read.inference_mode", "read.decompose_v2", "read.list_mode_trigger_v2", "read.coverage_select",
    "read.pool_protect_per_leg", "read.date_prior", "write.index_view", "write.index_view_carry",
)


#: v2-PICK2-winners (S02 graph base + the screened winners; see docs/v2/PICK2.md)
PICK2_KEYS = {
    "read.legs_v2", "read.episode_leg_v2", "read.standing_v2", "read.tiers_v2", "read.gate_v2",
    "read.trust_rendering", "read.decompose_v2", "read.pool_protect_per_leg",
    "offline.session_close_v2", "offline.standing_v2", "offline.histories_v2",
    "llm.roles.session_close.model", "llm.roles.session_close.api_base",
    "llm.roles.session_close.timeout_seconds", "llm.roles.session_close.no_think",
}


#: v2-PICK3-j (J reference + only the J-screened winners; see docs/v2/PICK2.md, "PICK3 (J winners)")
PICK3_KEYS = {
    "read.pool_protect_per_leg", "read.tiers_v2", "read.gate_v2", "read.trust_rendering", "read.decompose_v2",
    "write.index_view", "write.index_view_carry",
}


def _load(name: str) -> dict:
    return json.loads((ARMS / f"{name}.json").read_text(encoding="utf-8"))


def _rows() -> list[dict]:
    lines = [ln for ln in QUEUE.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    cols = ["id", "batch", "pkg", "arm", "ref_run", "rule", "sets", "deps", "kind", "est_min", "flag"]
    return [dict(zip(cols, r)) for r in csv.reader(lines, delimiter="\t")]


def _has(cfg: dict, dotted: str) -> bool:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _diff_paths(arm, base, prefix="") -> list[str]:
    """Leaf paths where ``arm`` differs from ``base`` (new keys count)."""
    if isinstance(arm, dict):
        base = base if isinstance(base, dict) else {}
        out: list[str] = []
        for k, v in arm.items():
            out += _diff_paths(v, base.get(k), f"{prefix}{k}.")
        return out
    return [] if arm == base else [prefix.rstrip(".")]


def _strip_pending(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    for dotted in PENDING_FLAGS:
        *parents, leaf = dotted.split(".")
        cur = out
        for p in parents:
            cur = cur.get(p, {}) if isinstance(cur, dict) else {}
        if isinstance(cur, dict):
            cur.pop(leaf, None)
    for sec in ("graph",):  # a section that only held pending flags is dropped as a whole
        if out.get(sec) == {}:
            out.pop(sec)
    if out.get("read", {}).get("cache") == {}:
        out["read"].pop("cache")
    return out


ROWS = _rows()
ARM_NAMES = sorted({r["arm"] for r in ROWS})


def test_queue_is_well_formed() -> None:
    ids = [r["id"] for r in ROWS]
    assert len(ids) == len(set(ids))
    known = set(ids)
    for r in ROWS:
        assert r["rule"] in {"PROMOTE", "NONINF", "PARITY", "REPORT", "REF"}
        assert set(r["sets"]) <= set("foFb")
        assert r["kind"] in {"J", "GC"}
        assert int(r["est_min"]) > 0
        for d in filter(lambda x: x != "-", r["deps"].split(",")):
            assert d in known, f"{r['id']} depends on unknown {d}"
            assert ids.index(d) < ids.index(r["id"]), f"{r['id']} is queued before its dependency {d}"
        assert (ARMS / f"{r['arm']}.json").exists(), f"{r['id']}: arm file missing"


def test_every_pending_flag_has_a_screen() -> None:
    # a flag column may name several flags joined by '+' and may carry a value ('key=8')
    queued = {f.split("=")[0] for r in ROWS for f in r["flag"].split("+")}
    assert set(PENDING_FLAGS) <= queued, sorted(set(PENDING_FLAGS) - queued)


@pytest.mark.parametrize("name", ARM_NAMES)
def test_arm_validates(name: str) -> None:
    arm = _load(name)
    if not name.startswith("v2-"):
        validate_engine_config(arm)  # existing arms are fully wired
        return
    if name.startswith("v2-PICK3"):
        # J-based winners: the J reference plus exactly the J-screened winners, no GC-only item
        extra = _diff_paths(arm, _load(BASE))
        assert set(extra) == PICK3_KEYS, sorted(set(extra) ^ PICK3_KEYS)
        assert arm["read"]["pool_protect_per_leg"] == 8 and arm["write"]["index_view_carry"] == "anaphoric"
        validate_engine_config(arm)
        return
    if name.startswith("v2-PICK2"):
        # the combined winners arm: the S02 graph base plus exactly the screened winners, and every prerequisite holds
        base = _load("scr-gc3-graph-jina")
        extra = _diff_paths(arm, base)
        allowed = PICK2_KEYS | ({"write.index_view", "write.index_view_carry"} if name.endswith("-iv") else set())
        assert set(extra) == allowed, sorted(set(extra) ^ allowed)
        validate_engine_config(arm)  # strict requires: legs_v2 under episode_leg_v2, offline.standing_v2 under read.standing_v2
        return
    flags = [f for f in PENDING_FLAGS if _has(arm, f)]
    assert flags, "a v2 arm must switch on at least one named flag"
    # the arm is the reference arm (or the GC base) plus flags only: nothing else differs
    base = _load("scr-gc3-graph-jina" if name.startswith("v2-F-01") else BASE)
    extra = [p for p in _diff_paths(arm, base) if not any(p == f or p.startswith(f + ".") for f in PENDING_FLAGS)]
    assert extra == [], f"{name} differs from its base beyond the flags: {extra}"
    try:
        validate_engine_config(arm)
    except Exception as exc:  # noqa: BLE001 - a missing flag is a schema error of any pydantic flavour
        validate_engine_config(_strip_pending(arm))  # the arm without the pending flags must be valid
        pytest.skip(f"flag(s) not in the config schema yet: {', '.join(flags)} ({type(exc).__name__})")
