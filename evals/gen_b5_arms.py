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
ROLE = {
    "provider": "ollama",
    "timeout_s": 180.0,
    "settings": {"model": "qwen3.5:9b", "base_url": "http://127.0.0.1:11434", "no_think": True},
}
SC = {
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
            }
        }
    }
}
WX = {
    "write": {"window_extract": "facts", "window_async": False},
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
}

#: arm name suffix -> overlay on the gc3 base
OVERLAYS = {
    "episodeleg": {
        "read": {"episode_leg": True},
        "offline": {"session_close": True},
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
                }
            }
        },
    },
    "entityleg": {"read": {"entity_leg": True}},
    "sessionclose": {
        "offline": {"session_close": True},
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
                }
            }
        },
    },
    "standing": {"read": {"standing": True}, "offline": {"standing": True}},
    "patterns": {"offline": {"patterns": True}},
    "tiers": {"read": {"tiers": True}},
    "router": {"read": {"router_rules": True}},
    "communities": {
        "offline": {"communities": True},
        "model": {
            "llm": {
                "summarize": {
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
    "reconcile": {"offline": {"reconcile": True}},
    "histories": {"offline": {"histories": True}},
    "profilecard": {
        "offline": {"session_close": True, "profile_card": True},
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
                }
            }
        },
    },
    "decay": {"offline": {"decay": True}},
    "windowfacts": {
        "write": {"window_extract": "facts", "window_async": False},
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
        except Exception as exc:
            bad += 1
            print(f"FAIL {path.name}: {type(exc).__name__}: {exc}")
    return bad


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(1 if check() else 0)
    for n in OVERLAYS:
        (ARMS / f"scr-gc3-{n}.json").write_text(
            json.dumps(build(n), indent=1) + "\n", encoding="utf-8"
        )
        print("wrote", f"scr-gc3-{n}.json")
