"""PrefEval adapter (synthetic fixture; CC BY-NC data)."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.prefeval import PrefEvalDataset, preference_turn

FIX = Path(__file__).resolve().parent / "fixtures" / "prefeval"
DATA = Path(__file__).resolve().parents[1] / "data" / "prefeval" / "benchmark_dataset"


def test_three_forms_map_gold_preference_turns() -> None:
    ds = PrefEvalDataset(FIX, revision_id="auto")
    items = {i.item_id: i for i in ds.items()}
    assert set(items) == {
        "explicit/travel_restaurant/0",
        "choice/travel_hotel/0",
        "persona/travel_hotel/0",
        "persona/travel_hotel/1",
    }
    exp = items["explicit/travel_restaurant/0"].queries[0]
    assert exp.gold_turn_ids == ("explicit/travel_restaurant/0:p0",)
    assert exp.gold == "I avoid spicy food." and exp.type_label == "explicit/travel_restaurant"
    choice = items["choice/travel_hotel/0"].queries[0]
    assert choice.gold_turn_ids == ("choice/travel_hotel/0:c2",)
    assert choice.meta["options_turn_id"] == "choice/travel_hotel/0:c1"
    persona = items["persona/travel_hotel/0"].queries[0]
    assert persona.gold_turn_ids == ("persona/travel_hotel/0:t1u",)
    assert persona.meta["gold_method"] == "overlap"
    unmapped = items["persona/travel_hotel/1"].queries[0]
    assert unmapped.gold_turn_ids == () and unmapped.meta["gold_method"] == "unmapped"
    hist_ids = {t.turn_id for i in items.values() for t in i.history}
    assert all(g in hist_ids for i in items.values() for g in i.queries[0].gold_turn_ids)
    assert ds.info().revision_id.startswith("sha256:")


def test_filler_is_seeded_and_appended() -> None:
    a = list(PrefEvalDataset(FIX, revision_id="x", forms=("explicit",), filler=2, seed=3).items())
    b = list(PrefEvalDataset(FIX, revision_id="x", forms=("explicit",), filler=2, seed=3).items())
    assert [t.turn_id for t in a[0].history] == [t.turn_id for t in b[0].history]
    assert len(a[0].history) == 1 + 4 and a[0].meta["filler"] == 2
    assert a[0].history[0].turn_id.endswith(":p0")  # the preference precedes the filler
    assert {t.session_id for t in a[0].history[1:]} == {"filler:f1", "filler:f2"}


def test_preference_turn_verbatim_and_threshold() -> None:
    turns = {"a": "Hello", "b": "Well, I avoid spicy food, honestly."}
    assert preference_turn("I avoid spicy food.", turns) == ("b", "verbatim", 1.0)
    assert preference_turn("I love opera music", turns)[1] == "unmapped"
    with pytest.raises(ValueError):
        PrefEvalDataset(FIX, revision_id="x", forms=("bogus",))


@pytest.mark.skipif(not (DATA / "explicit_preference").exists(), reason="PrefEval not fetched")
def test_real_topics_parse() -> None:
    ds = PrefEvalDataset(DATA, revision_id="main", per_topic=2)
    assert ds.info().n_queries == 3 * 20 * 2
