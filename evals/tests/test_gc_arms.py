"""G-C2 / G-C3 / G-C9 arms: valid against the config schema and a strict superset of scr-pool-protect-jina."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.rehearsal import validate_engine_config

ARMS = Path(__file__).resolve().parents[1] / "arms"
GC = ("scr-gc2-facts-jina", "scr-gc3-graph-jina", "scr-gc9-summary-jina")


def _load(name: str) -> dict:
    return json.loads((ARMS / f"{name}.json").read_text(encoding="utf-8"))


def _contains(base, arm, path="") -> list[str]:
    """Paths where `arm` does not carry `base`'s value (a dict may only gain keys)."""
    if isinstance(base, dict):
        bad = []
        for k, v in base.items():
            if not isinstance(arm, dict) or k not in arm:
                bad.append(f"{path}.{k}")
            else:
                bad += _contains(v, arm[k], f"{path}.{k}")
        return bad
    return [] if base == arm else [path]


@pytest.mark.parametrize("name", GC)
def test_arm_validates_and_keeps_the_jina_base(name: str) -> None:
    arm = _load(name)
    validate_engine_config(arm)
    assert _contains(_load("scr-pool-protect-jina"), arm) == []


@pytest.mark.parametrize("name", GC)
def test_every_llm_role_is_local_qwen_thinking_off(name: str) -> None:
    roles = _load(name)["model"]["llm"]
    assert roles
    for binding in roles.values():
        assert binding["settings"]["model"] == "qwen3.5:9b"
        assert binding["settings"]["base_url"] == "http://127.0.0.1:11434"
        assert binding["settings"]["no_think"] is True


def test_arm_keys() -> None:
    gc2, gc3, gc9 = (_load(n) for n in GC)
    cons = gc2["memories"]["episodic"]["policies"]["consolidation"]
    assert (
        cons["mine_facts"]
        and cons["miner"] == "llm"
        and cons["mine_multiview"]
        and cons["list_cards"]
    )
    assert (
        gc2["read"]["view_tag_leg"]
        and gc2["read"]["raw_turn_floor"]
        and "extract" in gc2["model"]["llm"]
    )
    assert (
        gc3["read"]["graph_leg"]
        and gc3["read"]["graph_node_search"]
        and gc3["read"]["entity_summaries"]
    )
    assert gc3["memories"]["associative"]["policies"]["entity_nodes"] is True
    assert gc3["memories"]["semantic"]["policies"]["extract_graph"]["granularity"] == "session"
    assert gc3["graph"]["entity_embeddings"] is True and "extract_edges" in gc3["model"]["llm"]
    assert gc9["read"]["profile_header_packing"] and {"summarize", "reflect"} <= set(
        gc9["model"]["llm"]
    )
    assert gc9["memories"]["episodic"]["policies"]["consolidation"]["reflect_profile"] is True
    assert gc9["memories"]["reflective"]["enabled"] is True
