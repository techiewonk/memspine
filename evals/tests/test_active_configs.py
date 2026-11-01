"""Phase D (ADR-096; RM-19, RM-20, RM-21, PA-107): every active evaluation config is current.

``test_arms.py`` covers ``evals/arms/*.json``. This covers the other active configs the
harness builds an engine from: plan arms (``plans/*.json``, base config merged with each arm's
delta), the inspector's demo arms, the CPU smoke's rewritten arm, and the launchers' extras.
``validate_engine_config`` also validates each selected provider's own ``settings``.
Historical run outputs (``results_qwen_stack/``, ``analysis/``, ``runs/``) are never rewritten
and are not checked here.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("memspine")
from memspine_evals.rehearsal import arm_engine_config, load_plan, validate_engine_config

EVALS = Path(__file__).resolve().parents[1]
REPO = EVALS.parent
PLANS = sorted((EVALS / "plans").glob("*.json"))
DEMO_ARMS = sorted((EVALS / "memspine_evals" / "inspect").glob("*arm.json"))

#: ADR-081 / ADR-082 keys that the current schema rejects.
RETIRED_TOP = {"embedding", "vector", "cache", "models", "decision"}
RETIRED_STORAGE = {"path", "provider", "settings", "profile", "graph_mirror"}
RETIRED_READ = {"rerank", "rerank_model", "rerank_device", "decider", "decider_model"}


def _assert_current_shape(config: dict[str, Any]) -> None:
    assert not RETIRED_TOP & config.keys()
    assert not RETIRED_STORAGE & (config.get("storage") or {}).keys()
    assert not RETIRED_READ & (config.get("read") or {}).keys()
    assert "roles" not in (config.get("llm") or {})
    assert ((config.get("model") or {}).get("embedding") or {}).get("provider") != "fastembed"
    for role, slot in ((config.get("model") or {}).get("llm") or {}).items():
        assert "provider" in slot and "timeout_s" in slot, role
        assert "base_url" not in (slot.get("settings") or {}), role  # ollama takes api_base


def _plan_arms() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in PLANS:
        plan = load_plan(path)
        for arm in plan["arms"]:
            config = arm_engine_config(plan, arm)
            if config is None:
                continue
            if plan.get("protocol", {}).get("memspine_llm"):
                # c0-1 --memspine-llm adds deployment.id (bedrock.merge_engine_llm_roles)
                config.setdefault("deployment", {}).setdefault("id", "bedrock-evaluation")
            out.append((f"{path.stem}:{arm['id']}", config))
    return out


@pytest.mark.parametrize("name,config", _plan_arms(), ids=lambda v: v if isinstance(v, str) else "")
def test_plan_arm_validates(name: str, config: dict[str, Any]) -> None:
    _assert_current_shape(config)
    validate_engine_config(config)


@pytest.mark.parametrize("path", DEMO_ARMS, ids=lambda p: p.stem)
def test_inspect_demo_arm_validates(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    _assert_current_shape(config)
    validate_engine_config(config)


def test_smoke_cpu_config_validates() -> None:
    import smoke_gc

    for arm_id in smoke_gc.ARMS:
        arm = json.loads((EVALS / "arms" / f"{arm_id}.json").read_text(encoding="utf-8"))
        config = smoke_gc.cpu_config(arm)
        assert config["storage"] == {
            "records": {"provider": "sqlite", "settings": {"path": "tempdir"}}
        }
        _assert_current_shape(config)
        validate_engine_config(config)


def test_launcher_extras_exist() -> None:
    """RM-19: a launcher may only sync extras that pyproject declares."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = set(pyproject["project"]["optional-dependencies"])
    for script in sorted(EVALS.glob("*.sh")):
        named = set(re.findall(r"--extra[ =]([A-Za-z0-9_-]+)", script.read_text(encoding="utf-8")))
        assert named <= extras, f"{script.name}: unknown extras {sorted(named - extras)}"


def test_forensics_report_reads_current_and_historical_manifests() -> None:
    import forensics_report as fr

    current = {
        "model": {
            "embedding": {"provider": "st", "settings": {"model": "Qwen/Qwen3-Embedding-0.6B"}},
            "rerank": {"chain": [{"provider": "jina", "settings": {"model": "jina-v3"}}]},
        }
    }
    assert fr._embedder_label(current) == "Qwen/Qwen3-Embedding-0.6B"
    assert fr._reranker_label(current) == "jina-v3"
    assert fr._reranker_label({"model": {"rerank": {"chain": []}}}) == "off"
    # a historical result manifest is read as written, never rewritten
    old = {"embedding": {"model": "bge"}, "read": {"rerank": "jina", "rerank_model": "jina-v3"}}
    assert fr._embedder_label(old) == "bge"
    assert fr._reranker_label(old) == "jina-v3"
