"""ConvoMem adapter (data-gated; CC BY-NC data, research only)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "data" / "convomem"


@pytest.mark.skipif(not (ROOT / "core_benchmark").exists(), reason="ConvoMem not fetched")
def test_stratified_sample_with_filler_is_reproducible() -> None:
    from memspine_evals.datasets import ConvoMemDataset

    a = list(ConvoMemDataset(ROOT, revision_id="e3e9b39", per_stratum=5, filler=2, seed=1).items())
    b = list(ConvoMemDataset(ROOT, revision_id="e3e9b39", per_stratum=5, filler=2, seed=1).items())
    # per_stratum caps each (evidence type, k) stratum, not the whole sample
    per_label = Counter(i.queries[0].type_label for i in a)
    assert per_label and all(n <= 5 for n in per_label.values())
    assert len(a) == sum(per_label.values())
    assert [i.history for i in a] == [i.history for i in b]
    assert all(i.queries[0].gold for i in a)
    assert all(i.meta["filler"] == 2 for i in a)
    assert any(i.queries[0].gold_turn_ids for i in a)
