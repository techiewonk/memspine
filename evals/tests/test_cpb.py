"""CPB Live replay adapter + retrieval-level false-adoption scorer (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.cpb import CPBLiveDataset, cpb_retrieval_rates

FIX = Path(__file__).resolve().parent / "fixtures" / "cpb"
DATA = Path(__file__).resolve().parents[1] / "data" / "cpb"


def test_cpb_replays_feeds_in_round_order_with_gold() -> None:
    ds = CPBLiveDataset(FIX, revision_id="fixture")
    (item,) = list(ds.items())
    assert [t.turn_id for t in item.history] == [
        "syn-00-srcA",
        "syn-00-srcB",
        "syn-00-copy",
        "syn-00-auth",
    ]
    stamps = [t.timestamp for t in item.history]
    assert stamps == sorted(stamps)
    assert item.history[2].meta["root"] == "syn-00-srcB"  # lineage collapses the copy
    (q,) = item.queries
    assert q.gold_turn_ids == ("syn-00-srcA",)
    assert q.meta["false_turn_ids"] == ["syn-00-srcB", "syn-00-copy"]
    assert q.meta["n_lineage_roots"] == 3
    assert q.type_label == "stage2/D-CONC"
    assert ds.info().n_queries == 1
    assert list(CPBLiveDataset(FIX, "fixture", stages=(5,)).items()) == []


def test_cpb_retrieval_rates() -> None:
    rows = [
        (["t", "f"], ["t"], ["f"]),  # both, true first
        (["f", "t"], ["t"], ["f"]),  # both, false first
        (["x", "y"], ["t"], ["f"]),  # neither
        (["t"], ["t"], []),  # no false feed in gold
    ]
    rates = cpb_retrieval_rates(rows, k=2)
    assert rates["true_recall"] == 0.75
    assert rates["false_retrieval_rate"] == 2 / 3
    assert rates["true_above_false"] == 0.5
    assert cpb_retrieval_rates([], k=1)["true_recall"] is None


@pytest.mark.skipif(not (DATA / "data" / "cpb_live").exists(), reason="CPB not fetched")
def test_cpb_real_live_parses() -> None:
    items = list(CPBLiveDataset(DATA, revision_id="auto-check").items())
    assert len(items) == 160  # local copy: 10+60+20+20+20+30 (card says 180)
    assert all(q.gold_turn_ids or q.meta["false_turn_ids"] for i in items for q in i.queries)
