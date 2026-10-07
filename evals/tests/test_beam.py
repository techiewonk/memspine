"""BEAM adapter (retrieval-only) on a synthetic fixture that copies the release schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from memspine_evals.datasets.beam import (  # noqa: E402
    BEAMDataset,
    contradiction_pair_recall,
    update_order,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "beam" / "rows.json"
DATA = (
    Path(__file__).resolve().parents[1] / "data" / "beam" / "data" / "100K-00000-of-00001.parquet"
)


def _parquet(tmp_path: Path) -> Path:
    path = tmp_path / "100K-00000-of-00001.parquet"
    pq.write_table(pa.Table.from_pylist(json.loads(FIX.read_text(encoding="utf-8"))), path)
    return path


def test_messages_become_turns_with_carried_time_anchor(tmp_path: Path) -> None:
    ds = BEAMDataset(_parquet(tmp_path), revision_id="auto")
    (item,) = list(ds.items())
    assert item.item_id == "c1"
    assert [t.turn_id for t in item.history] == [f"c1:{i}" for i in range(6)]
    assert [t.session_id for t in item.history] == ["s0"] * 3 + ["s1"] * 3
    assert item.history[2].timestamp == "2024-04-01"  # carried forward within the session
    assert item.history[3].timestamp == "2024-05-10"
    info = ds.info()
    assert info.dataset_id == "beam_100k" and info.revision_id.startswith("sha256:")
    assert info.n_queries == 5


def test_gold_ids_feed_recall_and_ku_gold_is_the_update(tmp_path: Path) -> None:
    (item,) = list(BEAMDataset(_parquet(tmp_path), revision_id="3205395").items())
    q = {query.type_label: query for query in item.queries}
    assert q["information_extraction"].gold_turn_ids == ("c1:0",)
    assert q["abstention"].gold_turn_ids == () and q["abstention"].meta["abstention"]
    ku = q["knowledge_update"]
    assert ku.gold_turn_ids == ("c1:5",) and ku.meta["stale_turn_ids"] == ["c1:2"]
    assert ku.gold == "250ms" and ku.meta["sub_type"] == "perf"
    cr = q["contradiction_resolution"]
    assert set(cr.gold_turn_ids) == {"c1:3", "c1:5"}
    # nested lists are flattened; an id absent from the chat (99) is dropped
    assert q["summarization"].gold_turn_ids == ("c1:0", "c1:2")
    history_ids = {t.turn_id for t in item.history}
    assert all(set(query.gold_turn_ids) <= history_ids for query in item.queries)


def test_update_order_and_contradiction_pair(tmp_path: Path) -> None:
    (item,) = list(BEAMDataset(_parquet(tmp_path), revision_id="x").items())
    q = {query.type_label: query for query in item.queries}
    ku, cr = q["knowledge_update"], q["contradiction_resolution"]
    assert update_order(["c1:5", "c1:2"], ku) is True
    assert update_order(["c1:2", "c1:5"], ku) is False
    assert update_order(["c1:2"], ku) is False
    assert update_order(["c1:5"], ku) is True
    assert update_order(["c1:0"], ku) is None
    assert update_order(["c1:5"], cr) is None  # not a KU probe
    assert contradiction_pair_recall(["c1:3", "c1:0", "c1:5"], cr, 3) is True
    assert contradiction_pair_recall(["c1:3", "c1:0", "c1:5"], cr, 2) is False
    assert contradiction_pair_recall(["c1:3"], ku, 3) is None


def test_ability_filter_and_missing_file(tmp_path: Path) -> None:
    ds = BEAMDataset(_parquet(tmp_path), revision_id="x", abilities=("knowledge_update",))
    assert [q.type_label for i in ds.items() for q in i.queries] == ["knowledge_update"]
    with pytest.raises(FileNotFoundError):
        BEAMDataset(tmp_path / "nope.parquet", revision_id="x")


@pytest.mark.skipif(not DATA.exists(), reason="BEAM 100K not fetched")
def test_local_100k_split_parses() -> None:
    ds = BEAMDataset(DATA, revision_id="3205395", max_conversations=2)
    items = list(ds.items())
    assert len(items) == 2
    ids = {t.turn_id for t in items[0].history}
    assert all(set(q.gold_turn_ids) <= ids for q in items[0].queries)
    assert any(q.gold_turn_ids for q in items[0].queries)
