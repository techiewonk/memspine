"""LaMP-2 and MemoryCD retrieval-only adapters + the kNN vote/mean scorers.

Fixtures under ``tests/fixtures/{lamp,memorycd}`` are hand-written and synthetic.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest
from memspine_evals.datasets.knn import knn_mean, knn_vote, mae, vote_accuracy
from memspine_evals.datasets.lamp import LaMP2Dataset, lamp2_query_text
from memspine_evals.datasets.memorycd import MemoryCDDataset

FIX = Path(__file__).resolve().parent / "fixtures"
DATA = Path(__file__).resolve().parents[1] / "data"


def test_knn_vote_majority_and_rank_tiebreak() -> None:
    labels = {"a": "x", "b": "y", "c": "y", "d": "x"}
    assert knn_vote(["a", "b", "c"], labels, k=3) == "y"
    assert knn_vote(["a", "b"], labels, k=2) == "x"  # tie -> best-ranked label
    assert knn_vote(["zz", "b", "b", "a"], labels, k=1) == "y"  # unknown + dup skipped
    assert knn_vote(["zz"], labels, k=3) is None


def test_knn_mean_mae_and_accuracy() -> None:
    values = {"a": 5.0, "b": 1.0, "c": 3.0}
    assert knn_mean(["a", "b", "c"], values, k=2) == 3.0
    assert knn_mean([], values, k=2) is None
    assert mae([3.0, None], [4.0, 2.0]) == 1.0
    assert mae([3.0, None], [4.0, 2.0], fallback=2.0) == 0.5
    with pytest.raises(ValueError):
        mae([None], [1.0])
    assert vote_accuracy(["Sci-Fi", None], ["sci-fi", "comedy"]) == 0.5


def test_lamp2_maps_profile_and_proxy_gold() -> None:
    ds = LaMP2Dataset(FIX / "lamp", revision_id="auto")
    items = list(ds.items())
    assert [i.item_id for i in items] == ["9001", "9002"]  # 9003 has no gold output
    q = items[0].queries[0]
    assert q.text == "SYNTHETIC. A robot crew drifts past a dying star."
    assert q.gold == "sci-fi"
    assert q.gold_turn_ids == ("9001:p90010", "9001:p90012")
    assert all("sci-fi" not in t.text for t in items[0].history)  # tag kept out of text
    assert knn_vote(["9001:p90012", "9001:p90011"], q.meta["labels"], k=1) == "sci-fi"
    assert items[1].queries[0].gold_turn_ids == ()  # no profile item has the gold tag
    assert ds.info().n_queries == 2
    assert lamp2_query_text("no marker here") == "no marker here"


def test_memorycd_holdout_has_no_future_leak(tmp_path: Path) -> None:
    gz = tmp_path / "users.jsonl.gz"
    with (FIX / "memorycd" / "users.jsonl").open("rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    cross = {i.item_id: i for i in MemoryCDDataset(gz, revision_id="auto").items()}
    assert set(cross) == {"U1:Books", "U1:Electronics"}  # U2 has a single interaction
    books = cross["U1:Books"]
    q = books.queries[0]
    assert q.gold == "5" and q.text == "Another great mystery"
    # history = everything before ts 5000, across domains: B1, E1, B2
    assert [t.turn_id.split(":")[2] for t in books.history] == ["B1", "E1", "B2"]
    assert q.gold_turn_ids == ("U1:Books:B1:1000",)
    assert knn_mean([t.turn_id for t in books.history], q.meta["ratings"], k=1) == 5.0
    single = {i.item_id: i for i in MemoryCDDataset(gz, "auto", setting="single").items()}
    assert {t.session_id for t in single["U1:Books"].history} == {"Books"}
    with pytest.raises(ValueError):
        MemoryCDDataset(gz, "auto", setting="bogus")


@pytest.mark.skipif(
    not (DATA / "lamp" / "LaMP_2_new_dev_dev_questions.json").exists(), reason="LaMP not fetched"
)
def test_lamp2_real_data_parses() -> None:
    items = list(LaMP2Dataset(DATA / "lamp", revision_id="auto", max_questions=20).items())
    assert items and all(i.queries[0].gold for i in items)
    assert any(i.queries[0].gold_turn_ids for i in items)
