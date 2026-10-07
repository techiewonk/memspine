"""PerLTQA adapter (synthetic fixture; CC BY-NC data)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.perltqa import PerLTQADataset

FIX = Path(__file__).resolve().parent / "fixtures" / "perltqa"
DATA = Path(__file__).resolve().parents[1] / "data" / "perltqa" / "repo"


def test_memory_records_become_turns_and_gold() -> None:
    ds = PerLTQADataset(FIX, revision_id="auto")
    (item,) = list(ds.items())
    ids = [t.turn_id for t in item.history]
    assert ids == [
        "Ava Li:profile:Protagonist",
        "Ava Li:profile:Age",
        "Ava Li:social:1_0",
        "Ava Li:event:1_0_0",
        "Ava Li:dialogue:1_0_0#0:0",
    ]
    assert item.history[3].timestamp == "May 1, 2022"
    assert "Bo Li: Well done." in item.history[4].text
    by_id = {q.query_id: q for q in item.queries}
    assert by_id["Ava Li:profile:0"].gold_turn_ids == ("Ava Li:profile:Age",)
    assert by_id["Ava Li:social_relationship:0"].gold_turn_ids == ("Ava Li:social:1_0",)
    assert by_id["Ava Li:events:0"].gold_turn_ids == ("Ava Li:event:1_0_0",)
    assert by_id["Ava Li:dialogues:0"].gold_turn_ids == ("Ava Li:dialogue:1_0_0#0:0",)
    missing = by_id["Ava Li:events:1"]
    assert missing.gold_turn_ids == () and missing.meta["gold_unmapped"] == ["9_9_9"]
    assert {q.type_label for q in item.queries} == {
        "profile",
        "social_relationship",
        "events",
        "dialogues",
    }
    assert ds.skipped == ["Ghost"] and "Ghost" in ds.info().notes


def test_type_filter_and_bad_version(tmp_path: Path) -> None:
    ds = PerLTQADataset(FIX, revision_id="8d9e198", memory_types=("events",))
    assert {q.type_label for i in ds.items() for q in i.queries} == {"events"}
    with pytest.raises(ValueError):
        PerLTQADataset(FIX, revision_id="x", version="fr")
    with pytest.raises(FileNotFoundError):
        PerLTQADataset(tmp_path, revision_id="x")


@pytest.mark.skipif(not (DATA / "Dataset").exists(), reason="PerLTQA not fetched")
def test_real_en_v2_parses() -> None:
    ds = PerLTQADataset(DATA, revision_id="8d9e198")
    queries = [q for i in ds.items() for q in i.queries]
    assert len(queries) > 8000
    assert sum(bool(q.gold_turn_ids) for q in queries) / len(queries) > 0.99
