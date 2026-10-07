"""PersonaBench adapter (retrieval-only) on a synthetic fixture that copies the release schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.personabench import PersonaBenchDataset, official_recall

FIX = Path(__file__).resolve().parent / "fixtures" / "personabench"
DATA = Path(__file__).resolve().parents[1] / "data" / "personabench" / "repo"


def test_segments_become_turns_per_person_and_noise() -> None:
    ds = PersonaBenchDataset(FIX, revision_id="auto", noises=("0.0", "0.3"))
    items = {i.item_id: i for i in ds.items()}
    assert set(items) == {"community_0/Ana Vell/noise_0.0", "community_0/Ana Vell/noise_0.3"}
    clean = items["community_0/Ana Vell/noise_0.0"]
    noisy = items["community_0/Ana Vell/noise_0.3"]
    assert [t.turn_id for t in clean.history] == [
        "000000000100",
        "000000000200",
        "000000000201",
        "000000000400",
    ]
    assert len(noisy.history) == 5  # the noise level adds a distractor session
    conv = clean.history[0]
    assert "Ana Vell: I studied at Ridge College." in conv.text
    assert "segment_id" not in conv.text and "session" not in conv.text.split("\n")[0]
    assert conv.timestamp == "2024/Oct/14/09:14 AM"
    assert "Tea set" in clean.history[3].text
    info = ds.info()
    assert info.revision_id.startswith("sha256:") and info.n_queries == 6


def test_gold_groups_and_labels() -> None:
    (item,) = list(PersonaBenchDataset(FIX, revision_id="151e8c9", noises=("0.0",)).items())
    q = {query.query_id.rsplit("/", 1)[1]: query for query in item.queries}
    assert set(q) == {"000000000", "000000001", "000000002"}  # Ghost Person has no data
    assert q["000000000"].gold_turn_ids == ("000000000100",)
    pref = q["000000001"]
    assert pref.type_label == "Preference/hard"
    assert set(pref.gold_turn_ids) == {"000000000200", "000000000400", "000000000201"}
    assert pref.meta["outdated_value"] == ["matcha"]
    assert pref.gold == '["green tea", "espresso"]'
    assert q["000000002"].meta["official_excluded"] is True
    history = {t.turn_id for t in item.history}
    assert all(set(query.gold_turn_ids) <= history for query in item.queries)


def test_official_recall_takes_best_combination() -> None:
    groups = {"green tea": ["a", "b"], "espresso": ["c"]}
    assert official_recall(["b", "c"], groups) == 1.0
    assert official_recall(["a"], groups) == 0.5
    assert official_recall([], groups) == 0.0
    assert official_recall(["a"], {}) == 0.0


def test_bad_noise_and_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PersonaBenchDataset(FIX, revision_id="x", noises=("0.9",))
    with pytest.raises(FileNotFoundError):
        PersonaBenchDataset(tmp_path, revision_id="x")


@pytest.mark.skipif(not (DATA / "eval_data").exists(), reason="PersonaBench not fetched")
def test_local_release_parses() -> None:
    ds = PersonaBenchDataset(DATA, revision_id="151e8c9", noises=("0.0",))
    items = list(ds.items())
    assert len(items) == 6 and ds.info().n_queries == 263
    for item in items:
        ids = {t.turn_id for t in item.history}
        assert all(q.gold_turn_ids and set(q.gold_turn_ids) <= ids for q in item.queries)
