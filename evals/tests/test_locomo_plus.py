"""LoCoMo-Plus adapter: deterministic construction (data-gated)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.locomo_plus import LoCoMoPlusDataset, gap_days

DATA = Path(__file__).resolve().parents[1] / "data" / "locomo_plus" / "data"


def test_gap_days_matches_build_conv() -> None:
    assert gap_days("two weeks later") == 14
    assert gap_days("about a month later") == 30
    assert gap_days("3 years later") == 3 * 365
    assert gap_days("later that day") == 0


@pytest.mark.skipif(not (DATA / "locomo_plus.json").exists(), reason="LoCoMo-Plus not fetched")
def test_items_are_deterministic_and_cue_is_planted() -> None:
    a = LoCoMoPlusDataset(DATA / "locomo_plus.json", DATA / "locomo10.json", revision_id="auto")
    b = LoCoMoPlusDataset(DATA / "locomo_plus.json", DATA / "locomo10.json", revision_id="auto")
    ia, ib = list(a.items()), list(b.items())
    assert len(ia) == 401
    assert [i.item_id for i in ia] == [i.item_id for i in ib]
    first = ia[0]
    [q] = first.queries
    cue_ids = set(q.gold_turn_ids)
    assert cue_ids
    assert cue_ids <= {h.turn_id for h in first.history}
    assert q.gold
    assert q.type_label in {"causal", "state", "goal", "value"}
    assert q.meta["base_conversation"] == "conv-26"  # item 0 pairs with conversation 0
