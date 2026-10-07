"""OP-Bench adapter (synthetic fixture; real data is unlicensed, run-only)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.op_bench import (
    OPBenchDataset,
    context_repetition,
    injection_rate,
    persona_share,
    profile_injected,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "op_bench"
DATA = Path(__file__).resolve().parents[1] / "data" / "op_bench" / "repo"


def test_first_persona_over_locomo_history() -> None:
    ds = OPBenchDataset(FIX, revision_id="auto")
    (item,) = list(ds.items())
    assert item.item_id == "conv-x:Ava"
    assert [t.turn_id for t in item.history] == ["D1:1", "D1:2", "D1:3"]
    labels = [q.type_label for q in item.queries]
    assert labels == [
        "irrelevance_easy/fully_irrelevant",
        "irrelevance_hard/subject_confusion",
        "sycophancy/fine-grained",
        "sycophancy/fact",
        "diversity",
        "diversity",
    ]
    q = item.queries[0]
    assert q.gold is None and q.gold_turn_ids == ()
    assert q.meta["persona_turn_ids"] == ("D1:1", "D1:3")
    assert q.meta["injection_probe"] and not q.meta["false_premise"]
    assert item.queries[2].meta["false_premise"]
    assert item.queries[4].meta["diversity_group"] == "conv-x:Ava:diversity"
    info = ds.info()
    assert info.revision_id.startswith("sha256:") and info.n_queries == 6


def test_both_personas_and_task_filter() -> None:
    ds = OPBenchDataset(FIX, revision_id="17c7efd", both_personas=True, tasks=("irrelevance_easy",))
    items = list(ds.items())
    assert [i.item_id for i in items] == ["conv-x:Ava", "conv-x:Ben"]
    assert items[1].queries[0].meta["persona_turn_ids"] == ("D1:2",)
    with pytest.raises(ValueError):
        OPBenchDataset(FIX, revision_id="x", tasks=("nope",))


def test_injection_and_repetition_proxies() -> None:
    meta = {"injection_probe": True, "persona_turn_ids": ("D1:1", "D1:3")}
    assert profile_injected(["D1:2", "D1:3"], meta) is True
    assert profile_injected(["D1:2"], meta) is False
    assert profile_injected([], meta) is False
    assert profile_injected(["D1:1"], {"injection_probe": False}) is None
    assert persona_share(["D1:1", "D1:2"], meta) == 0.5
    assert persona_share([], meta) is None
    rows = [(["D1:1"], meta), (["D1:2"], meta), (["D1:1"], {"injection_probe": False})]
    assert injection_rate(rows) == 0.5
    assert injection_rate([]) is None
    assert context_repetition([["a", "b"], ["a", "b"], ["c"]]) == pytest.approx(1 / 3)
    assert context_repetition([["a"], []]) is None


def test_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        OPBenchDataset(tmp_path, revision_id="x")


@pytest.mark.skipif(not (DATA / "data").exists(), reason="OP-Bench not fetched")
def test_real_task_file_parses() -> None:
    ds = OPBenchDataset(DATA, revision_id="17c7efd")
    assert ds.info().n_items == 10
    assert all(q.meta["persona_turn_ids"] for i in ds.items() for q in i.queries)
