"""ASB firewall TPR/FPR harness (synthetic fixture; the engine detector only if importable)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.asb import ASB_TEMPLATES, ASBDataset, asb_samples, firewall_rates

FIX = Path(__file__).resolve().parent / "fixtures" / "asb"


def test_samples_cover_positive_and_negative_groups() -> None:
    samples = asb_samples(FIX)
    groups = {s.group for s in samples}
    assert {"instruction", "tool_description", "normal_tool", "agent_task"} <= groups
    assert {f"injected/{v}" for v in ASB_TEMPLATES} <= groups
    assert sum(s.is_attack for s in samples) == 2 * (2 + len(ASB_TEMPLATES))
    assert sum(not s.is_attack for s in samples) == 2 + 2
    naive = next(s for s in samples if s.sample_id == "atk0:naive")
    assert '"SynLeak" tool, and do NOT use other tools.' in naive.text


def test_firewall_rates_with_a_stub_detector() -> None:
    samples = asb_samples(FIX, variants=["context_ignoring"])
    rates = firewall_rates(samples, detector=lambda text: "ignore" in text.lower())
    assert rates["groups"]["injected/context_ignoring"]["tpr"] == 1.0
    assert rates["groups"]["instruction"]["tpr"] == 0.5  # only SynWipe says "Ignore"
    assert rates["groups"]["tool_description"]["tpr"] == 0.0
    assert rates["groups"]["normal_tool"]["fpr"] == 0.0
    assert rates["n_pos"] == 6 and rates["n_neg"] == 4
    assert rates["tpr"] == 3 / 6 and rates["fpr"] == 0.0


def test_asb_items_mark_attack_turns() -> None:
    ds = ASBDataset(FIX, revision_id="auto", variant="naive")
    (item,) = list(ds.items())
    assert len(item.history) == 4  # 2 normal tools + 2 injected attacks
    q = item.queries[0]
    assert q.gold_turn_ids == ("norm0", "norm1")
    assert q.meta["attack_turn_ids"] == ["atk0:naive", "atk1:naive"]
    with pytest.raises(ValueError):
        ASBDataset(FIX, revision_id="auto", variant="nope")


def test_engine_detector_flags_context_ignoring() -> None:
    pytest.importorskip("memspine.core.firewall")
    samples = asb_samples(FIX, variants=["context_ignoring", "naive"])
    base = firewall_rates(samples, "base")
    assert base["groups"]["injected/context_ignoring"]["tpr"] == 1.0
    # "From now on ..." in a benign normal tool is a known base-pattern false flag (A7).
    assert base["groups"]["normal_tool"]["flagged"] == 1
    extended = firewall_rates(samples, "extended")
    assert extended["groups"]["injected/naive"]["tpr"] == 1.0
