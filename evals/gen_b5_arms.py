"""Generate the B5 arm files: scr-gc3-graph-jina + one flag set each (Q-B5). Static, CPU only.

    python gen_b5_arms.py            # write arms/scr-gc3-<name>.json
    python gen_b5_arms.py --check    # validate every arm against the config schema and the gc3 base (no model, no GPU)
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ARMS = Path(__file__).resolve().parent / "arms"
BASE = "scr-gc3-graph-jina"
ROLE = {"model": "ollama/qwen3.5:9b", "api_base": "http://127.0.0.1:11434", "timeout_seconds": 180.0, "no_think": True}
SC = {"llm": {"roles": {"session_close": ROLE}}}
WX = {"write": {"window_extract": "facts", "window_async": False}, "llm": {"roles": {"window_extract": ROLE}}}

#: arm name suffix -> overlay on the gc3 base
OVERLAYS: dict[str, dict] = {
    "legs": {"read": {"legs_v2": True}},
    "graphleg": {"read": {"legs_v2": True, "graph_leg_v2": True}, "graph": {"edge_compiler_v2": True}},
    "edgecomp": {"graph": {"edge_compiler_v2": True}},
    "episodeleg": {"read": {"legs_v2": True, "episode_leg_v2": True}, "offline": {"session_close_v2": True}, **SC},
    "entityleg": {"read": {"entity_leg_v2": True}},
    "sessionclose": {"offline": {"session_close_v2": True}, **SC},
    "standing": {"read": {"standing_v2": True}, "offline": {"standing_v2": True}},
    "patterns": {"offline": {"patterns_v2": True}},
    "tiers": {"read": {"tiers_v2": True}},
    "router": {"read": {"router_rules": True}},
    "communities": {"offline": {"communities_v2": True}},
    "reconcile": {"offline": {"reconcile_v2": True}},
    "histories": {"offline": {"histories_v2": True}},
    "profilecard": {"offline": {"session_close_v2": True, "profile_card_v2": True}, **SC},
    "decay": {"offline": {"decay_v2": True}},
    "stamping": {"write": {"stamping_v2": True}},
    "rolepolicy": {"write": {"role_policy_v2": True}},
    "intake": {"write": {"intake_v2": True}},
    # window-facts reference and its two flag arms (the flags need write.window_extract = facts)
    "windowfacts": WX,
    "envelope": {"write": {"window_extract": "facts", "window_async": False, "envelope_v2": True}, "llm": {"roles": {"window_extract": ROLE}}},
    "segmentation": {"write": {"window_extract": "facts", "window_async": False, "segmentation_v2": True}, "llm": {"roles": {"window_extract": ROLE}}},
}


def merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def build(name: str) -> dict:
    base = json.loads((ARMS / f"{BASE}.json").read_text(encoding="utf-8"))
    return merge(base, OVERLAYS[name])


def check() -> int:
    from memspine_evals.rehearsal import validate_engine_config

    bad = 0
    for name in OVERLAYS:
        path = ARMS / f"scr-gc3-{name}.json"
        try:
            arm = json.loads(path.read_text(encoding="utf-8"))
            assert arm == build(name), "file differs from the generator"
            validate_engine_config(arm)
            print(f"ok   {path.name}")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"FAIL {path.name}: {type(exc).__name__}: {exc}")
    return bad


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(1 if check() else 0)
    for n in OVERLAYS:
        (ARMS / f"scr-gc3-{n}.json").write_text(json.dumps(build(n), indent=1) + "\n", encoding="utf-8")
        print("wrote", f"scr-gc3-{n}.json")
