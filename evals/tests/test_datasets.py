"""Dataset adapters against miniature fixtures in each benchmark's real shape.

The fixtures are hand-written, tiny and licence-free: the point is that the
adapters parse the published shape correctly, including the two fields the
literature routinely loses — LoCoMo's adversarial category and LongMemEval's
data revision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memspine_evals.datasets import LoCoMoDataset, LongMemEvalDataset, SyntheticDataset

LOCOMO_FIXTURE = [
    {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Ana",
            "speaker_b": "Ben",
            "session_1_date_time": "2:00 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ana", "dia_id": "D1:1", "text": "I adopted a greyhound."},
                {"speaker": "Ben", "dia_id": "D1:2", "text": "What did you name her?"},
                {"speaker": "Ana", "dia_id": "D1:3", "text": "Her name is Juno."},
            ],
            "session_2_date_time": "9:00 am on 12 June, 2023",
            "session_2": [
                {
                    "speaker": "Ana",
                    "dia_id": "D2:1",
                    "text": "Here she is at the park.",
                    "blip_caption": "a greyhound running on grass",
                }
            ],
        },
        "qa": [
            {
                "question": "What is the name of Ana's dog?",
                "answer": "Juno",
                "evidence": ["D1:3"],
                "category": 1,
            },
            {
                "question": "What breed is Ben's cat?",
                "adversarial_answer": "No information available",
                "evidence": [],
                "category": 5,
            },
        ],
    }
]

LME_FIXTURE = [
    {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What is my allergy?",
        "answer": "walnuts",
        "question_date": "2026/03/01",
        "haystack_session_ids": ["sess_a", "sess_b"],
        "haystack_dates": ["2026/01/05", "2026/02/11"],
        "answer_session_ids": ["sess_b"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Morning!", "has_answer": False},
                {"role": "assistant", "content": "Good morning.", "has_answer": False},
            ],
            [
                {"role": "user", "content": "I am allergic to walnuts.", "has_answer": True},
                {"role": "assistant", "content": "Noted.", "has_answer": False},
            ],
        ],
    }
]


@pytest.fixture
def locomo_path(tmp_path: Path) -> Path:
    path = tmp_path / "locomo10.json"
    path.write_text(json.dumps(LOCOMO_FIXTURE), encoding="utf-8")
    return path


@pytest.fixture
def lme_path(tmp_path: Path) -> Path:
    path = tmp_path / "longmemeval_s.json"
    path.write_text(json.dumps(LME_FIXTURE), encoding="utf-8")
    return path


def test_locomo_parses_sessions_in_order(locomo_path: Path) -> None:
    dataset = LoCoMoDataset(locomo_path, revision_id="fixture")
    item = next(dataset.items())
    assert [t.turn_id for t in item.history] == ["D1:1", "D1:2", "D1:3", "D2:1"]
    assert item.history[0].session_id == "session_1"
    assert item.history[0].timestamp == "2:00 pm on 8 May, 2023"


def test_locomo_keeps_image_captions_as_evidence_text(locomo_path: Path) -> None:
    item = next(LoCoMoDataset(locomo_path, revision_id="fixture").items())
    assert "greyhound running on grass" in item.history[-1].text


def test_locomo_keeps_the_adversarial_class_separable(locomo_path: Path) -> None:
    item = next(LoCoMoDataset(locomo_path, revision_id="fixture").items())
    labels = [q.type_label for q in item.queries]
    assert labels == ["cat1", "cat5"]
    assert item.queries[1].meta["adversarial"] is True


def test_locomo_cat5_gold_is_a_refusal_never_the_distractor(locomo_path: Path) -> None:
    """R3-1: cat 5's ``adversarial_answer`` is what a fooled system says; as gold it
    scored being fooled as correct."""
    from memspine_evals.judge import ABSTENTION_GOLD

    dataset = LoCoMoDataset(locomo_path, revision_id="fixture")
    cat5 = [q for item in dataset.items() for q in item.queries if q.type_label == "cat5"]
    assert cat5
    for query in cat5:
        assert query.gold == ABSTENTION_GOLD
        assert query.gold != query.meta["adversarial_answer"]
        assert query.meta["abstention"] is True
        assert query.gold_turn_ids == ()
    assert "cat5-gold=abstention" in dataset.info().notes


def test_locomo_judge_evidence_matches_locomo_plus_format(locomo_path: Path) -> None:
    item = next(LoCoMoDataset(locomo_path, revision_id="fixture").items())
    assert item.queries[0].meta["judge_evidence"] == "Ana\uff1aHer name is Juno."
    assert item.queries[0].meta["category"] == 1


def test_locomo_category_filter_narrows_the_subset(locomo_path: Path) -> None:
    dataset = LoCoMoDataset(locomo_path, revision_id="fixture", categories=(1,))
    info = dataset.info()
    assert info.n_queries == 1
    assert "cat1" in info.subset


def test_locomo_gold_evidence_becomes_retrieval_ground_truth(locomo_path: Path) -> None:
    item = next(LoCoMoDataset(locomo_path, revision_id="fixture").items())
    assert item.queries[0].gold_turn_ids == ("D1:3",)


def test_auto_revision_identifies_the_bytes(locomo_path: Path) -> None:
    info = LoCoMoDataset(locomo_path, revision_id="auto").info()
    assert info.revision_id.startswith("sha256:")
    assert info.content_sha256.startswith(info.revision_id.split(":")[1])


def test_missing_file_says_where_to_get_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"evals/README\.md"):
        LoCoMoDataset(tmp_path / "nope.json", revision_id="x")


def test_longmemeval_requires_an_explicit_revision(lme_path: Path) -> None:
    with pytest.raises(ValueError, match="September 2025 cleaned"):
        LongMemEvalDataset(lme_path, revision_id="")


def test_longmemeval_one_question_per_item(lme_path: Path) -> None:
    dataset = LongMemEvalDataset(lme_path, revision_id="2025-09-cleaned")
    items = list(dataset.items())
    assert len(items) == 1
    assert len(items[0].queries) == 1
    assert dataset.info().dataset_id == "longmemeval-s"
    assert dataset.info().revision_id == "2025-09-cleaned"


def test_longmemeval_gold_turns_come_from_has_answer(lme_path: Path) -> None:
    item = next(LongMemEvalDataset(lme_path, revision_id="2025-09-cleaned").items())
    assert item.queries[0].gold_turn_ids == ("sess_b:0",)
    assert [t.turn_id for t in item.history][:2] == ["sess_a:0", "sess_a:1"]
    assert item.history[2].timestamp == "2026/02/11"


def test_longmemeval_type_filter(lme_path: Path) -> None:
    dataset = LongMemEvalDataset(
        lme_path, revision_id="2025-09-cleaned", question_types=("multi-session",)
    )
    assert list(dataset.items()) == []
    assert "types:multi-session" in dataset.info().subset


def test_synthetic_is_labelled_as_unquotable() -> None:
    info = SyntheticDataset(n_items=1, turns_per_item=8).info()
    assert info.dataset_id == "synthetic-smoke"
    assert "never quote" in info.notes
    assert info.licence.startswith("none")
