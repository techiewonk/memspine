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
ROLE = {
    "provider": "ollama",
    "timeout_s": 180.0,
    "settings": {"model": "qwen3.5:9b", "base_url": "http://127.0.0.1:11434", "no_think": True},
}
#: R3-12: one sufficiency check, at most one rewrite and one extra retrieval; without a bound role the deep tier
#: is rules-only and logs ``agentic_unbound`` (audit finding 4)
SUFF = {
    "model": {
        "llm": {
            "sufficiency": {
                "provider": "ollama",
                "timeout_s": 180.0,
                "settings": {
                    "model": "qwen3.5:9b",
                    "base_url": "http://127.0.0.1:11434",
                    "no_think": True,
                },
            }
        }
    }
}
#: a quarter of the 4096-token cap stays for the evidence: the reply reserve takes the other three quarters, so
#: the budget binds (audit finding 6; the arms use candidate_pool 2 / rerank_keep 10 / list_mode)
TIGHT = {"read": {"reply_reserve_tokens": 3072}}

#: name -> (base arm, overlay). Arm file = scr-b7-<name>.json
B7 = {
    "tightbudget": ("scr-pool-protect-jina", {"read": {"reply_reserve_tokens": 3072}}),
    "exactbudget": (
        "scr-pool-protect-jina",
        {"read": {"reply_reserve_tokens": 3072, "exact_budget": True}},
    ),
    "bundles": ("scr-gc3-windowfacts", {"read": {"evidence_bundles": True}}),
    "deep": (
        "scr-pool-protect-jina",
        {
            "read": {"gate": True, "deep": True},
            "model": {
                "llm": {
                    "sufficiency": {
                        "provider": "ollama",
                        "timeout_s": 180.0,
                        "settings": {
                            "model": "qwen3.5:9b",
                            "base_url": "http://127.0.0.1:11434",
                            "no_think": True,
                        },
                    }
                }
            },
        },
    ),
    "obligations": (
        "scr-pool-protect-jina",
        {
            "read": {"gate": True, "deep": True, "obligations": True},
            "model": {
                "llm": {
                    "sufficiency": {
                        "provider": "ollama",
                        "timeout_s": 180.0,
                        "settings": {
                            "model": "qwen3.5:9b",
                            "base_url": "http://127.0.0.1:11434",
                            "no_think": True,
                        },
                    }
                }
            },
        },
    ),
    "timeview": ("scr-pool-protect-jina", {"read": {"contract_rules": True}}),
}

#: B8 profile -> (base arm, extra overlay on top of the preset: roles and eval determinism the preset cannot carry)
B8 = {
    "read-core": {},
    "read-graph": {
        "model": {
            "llm": {
                "session_close": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                },
                "summarize": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                },
            }
        }
    },
    "write": {
        "write": {"window_async": False},
        "model": {
            "llm": {
                "window_extract": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                }
            }
        },
    },
    "read-safe": {
        "model": {
            "llm": {
                "sufficiency": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                }
            }
        }
    },
    "offline": {
        "model": {
            "llm": {
                "session_close": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                },
                "summarize": {
                    "provider": "ollama",
                    "timeout_s": 180.0,
                    "settings": {
                        "model": "qwen3.5:9b",
                        "base_url": "http://127.0.0.1:11434",
                        "no_think": True,
                    },
                },
            }
        }
    },
    "trust": {},
    "run": {},
}


def merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = (
            merge(out[k], v)
            if isinstance(v, dict) and isinstance(out.get(k), dict)
            else copy.deepcopy(v)
        )
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
            assert json.loads(path.read_text(encoding="utf-8")) == want, (
                "file differs from the generator"
            )
            validate_engine_config(
                want
            )  # schema + policy blocks + every prerequisite (soft ones too)
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
