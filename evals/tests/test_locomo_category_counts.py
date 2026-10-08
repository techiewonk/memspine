"""N48 (plan v3.2 / S6a, S6b): LoCoMo category labels and counts are pinned.

Three vendors' reports permuted LoCoMo's categories (their "temporal" was category 3,
their "single-hop" category 1). Our numbering is the dataset's own: 1 multi-hop,
2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. A per-category number in a
report must come from these labels and these counts.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest
from memspine_evals.datasets.locomo import LoCoMoDataset

DATA = Path(__file__).resolve().parents[1] / "data" / "locomo10.json"
COUNTS = {"cat1": 282, "cat2": 321, "cat3": 96, "cat4": 841, "cat5": 446}
EXAMPLE = {  # one question per category, from the dataset itself
    "cat2": "When did Caroline go to the LGBTQ support group?",
}


@pytest.mark.skipif(not DATA.exists(), reason="LoCoMo data not present")
def test_locomo_category_counts_and_labels() -> None:
    ds = LoCoMoDataset(DATA, revision_id="test")
    counts = collections.Counter(q.type_label for item in ds.items() for q in item.queries)
    assert dict(counts) == COUNTS
    assert sum(counts[c] for c in ("cat1", "cat2", "cat3", "cat4")) == 1540
    by_text = {q.text: q.type_label for item in ds.items() for q in item.queries}
    for label, text in EXAMPLE.items():
        assert by_text.get(text) == label
