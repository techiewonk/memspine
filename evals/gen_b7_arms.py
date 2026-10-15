# ruff: noqa: E501
"""Generate the B7 / B8 arm files (FU-REQS; audit 2026-10-13, findings 3-8 and section 3). Static, CPU only.

B7 re-arms the B2/B5 screens that could not show an effect (S17, S18, S21, S22, S27; S23, S24 and S46 are
skipped, see SCREENS.md "B7 / B8"). B8 is one gc3 arm per flag profile; its flags come from the preset
``src/memspine/config/presets/<profile>.yaml``, so the arm and the profile cannot drift.

    python gen_b7_arms.py            # write arms/scr-b7-*.json and arms/scr-b8-*.json
    python gen_b7_arms.py --check    # every arm equals the generator output and passes the strict arm validator
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ARMS = Path(__file__).resolve().parent / "arms"
PRESETS = Path(__file__).resolve().parents[1] / "src" / "memspine" / "config" / "presets"
J_BASE = "scr-pool-protect-jina"  # the reference arm behind f-jref2-i2
GC_BASE = "scr-gc3-graph-jina"  # S02
WF_BASE = "scr-gc3-windowfacts"  # S43: gc3 + write.window_extract=facts
ROLE = {"model": "ollama/qwen3.5:9b", "api_base": "http://127.0.0.1:11434", "timeout_seconds": 180.0, "no_think": True}
#: R3-12: one sufficiency check, at most one rewrite and one extra retrieval; without a bound role the deep tier
#: is rules-only and logs ``agentic_unbound`` (audit finding 4)
SUFF = {"llm": {"roles": {"sufficiency": ROLE}}}
#: a quarter of the 4096-token cap stays for the evidence: the reply reserve takes the other three quarters, so
#: the budget binds (audit finding 6; the arms use candidate_pool 2 / rerank_keep 10 / list_mode)
TIGHT = {"read": {"reply_reserve_tokens": 3072}}

#: name -> (base arm, overlay). Arm file = scr-b7-<name>.json
B7: dict[str, tuple[str, dict]] = {
    # S17 re-armed: a reference and an arm that share the tight budget; only exact_budget differs
    "tightbudget": (J_BASE, TIGHT),
    # read.reader_tokenizer stays unset: the reader is qwen3.5:9b and no Qwen3.5 tokenizer is in the local HF cache
    # (checked 2026-10-13; Qwen3 snapshots only, a different vocabulary). exact_budget therefore counts with the
    # labelled chars/4 estimate (accounting=estimated) and the gain is skip-and-continue + header counting.
    # To make it exact: cache the tokenizer once, then add "reader_tokenizer": "hf:<repo>" to S52 and S53 here.
    "exactbudget": (J_BASE, {"read": {"reply_reserve_tokens": 3072, "exact_budget": True}}),
    # S18 re-armed on mined facts with source turns (window facts), where a bundle is more than the fact itself
    "bundles": (WF_BASE, {"read": {"evidence_bundles": True}}),
    # S21 / S22 re-armed with the sufficiency role bound (deep_v2 needs it; obligations feed the rule check)
    "deep": (J_BASE, {"read": {"gate_v2": True, "deep_v2": True}, **SUFF}),
    "obligations": (J_BASE, {"read": {"gate_v2": True, "deep_v2": True, "obligations": True}, **SUFF}),
    # S24 prepared, row skipped: needs an as-of question set (see SCREENS.md); contract_rules emits the time view
    "timeview": (J_BASE, {"read": {"contract_rules": True, "timeview_v2": True}}),
}

#: B8 profile -> (base arm, extra overlay on top of the preset: roles and eval determinism the preset cannot carry)
B8: dict[str, dict] = {
    "read-v2-core": {},
    # FX-EVAL: summarize feeds communities_v2 summaries (extractive without it)
    "read-v2-graph": {"llm": {"roles": {"session_close": ROLE, "summarize": ROLE}}},
    "write-v2": {"write": {"window_async": False}, "llm": {"roles": {"window_extract": ROLE}}},
    "read-v2-safe": SUFF,
    "offline-v2": {"llm": {"roles": {"session_close": ROLE, "summarize": ROLE}}},
    "trust-v2": {},
    "run-v2": {},
}


def merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _arm(name: str) -> dict:
    return json.loads((ARMS / f"{name}.json").read_text(encoding="utf-8"))


def preset(name: str) -> dict:
    return yaml.safe_load((PRESETS / f"{name}.yaml").read_text(encoding="utf-8"))


def build_b7(name: str) -> dict:
    base, overlay = B7[name]
    return merge(_arm(base), overlay)


def build_b8(profile: str) -> dict:
    return merge(merge(_arm(GC_BASE), preset(profile)), B8[profile])


def targets() -> dict[str, dict]:
    out = {f"scr-b7-{n}": build_b7(n) for n in B7}
    out.update({f"scr-b8-{p}": build_b8(p) for p in B8})
    return out


def check() -> int:
    from memspine_evals.rehearsal import validate_engine_config

    bad = 0
    for name, want in targets().items():
        path = ARMS / f"{name}.json"
        try:
            assert json.loads(path.read_text(encoding="utf-8")) == want, "file differs from the generator"
            validate_engine_config(want)  # schema + policy blocks + every prerequisite (soft ones too)
            print(f"ok   {path.name}")
        except Exception as exc:
            bad += 1
            print(f"FAIL {path.name}: {type(exc).__name__}: {exc}")
    return bad


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(1 if check() else 0)
    for n, arm in targets().items():
        (ARMS / f"{n}.json").write_text(json.dumps(arm, indent=1) + "\n", encoding="utf-8")
        print("wrote", f"{n}.json")
