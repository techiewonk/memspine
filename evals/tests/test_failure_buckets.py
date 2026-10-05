"""failure_buckets.py: date parsing, bucket rules and gold flags on synthetic rows."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import failure_buckets as fb
import pytest
from memspine_evals.datasets import LoCoMoDataset


@pytest.mark.parametrize(
    ("text", "lo", "hi", "granularity"),
    [
        ("7 May 2023", date(2023, 5, 7), date(2023, 5, 7), "day"),
        ("on 2023-05-07.", date(2023, 5, 7), date(2023, 5, 7), "day"),
        ("May 7, 2023", date(2023, 5, 7), date(2023, 5, 7), "day"),
        ("15April, 2022", date(2022, 4, 15), date(2022, 4, 15), "day"),
        ("Januarty 5, 2024", date(2024, 1, 5), date(2024, 1, 5), "day"),
        ("The sunday before 3` July 2023", date(2023, 7, 2), date(2023, 7, 2), "relative_day"),
        ("Saturday after 11 September, 2023.", date(2023, 9, 16), date(2023, 9, 16),
         "relative_day"),
        ("The week before 9 June 2023", date(2023, 5, 31), date(2023, 6, 8), "range"),
        ("A few days before May 24, 2023.", date(2023, 5, 18), date(2023, 5, 23), "range"),
        ("November 5-6, 2022", date(2022, 11, 5), date(2022, 11, 6), "range"),
        ("between October 19 and 24, 2023", date(2023, 10, 19), date(2023, 10, 24), "range"),
        ("from 2023-06-26 to 2023-07-02", date(2023, 6, 26), date(2023, 7, 2), "range"),
        ("first week of May 2023", date(2023, 5, 1), date(2023, 5, 7), "range"),
        ("last week of August 2023", date(2023, 8, 24), date(2023, 8, 31), "range"),
        ("June 2023", date(2023, 6, 1), date(2023, 6, 30), "month"),
        ("in 2022-04.", date(2022, 4, 1), date(2022, 4, 30), "month"),
        ("2019", date(2019, 1, 1), date(2019, 12, 31), "year"),
    ],
)  # fmt: skip
def test_parse_interval(text: str, lo: date, hi: date, granularity: str) -> None:
    iv = fb.parse_interval(text)
    assert iv is not None
    assert (iv.lo, iv.hi, iv.granularity) == (lo, hi, granularity)


def test_parse_interval_none_without_a_date() -> None:
    assert fb.parse_interval("Woodhaven") is None
    assert fb.parse_interval("Decided 5 things") is None


@pytest.mark.parametrize(
    ("question", "gold", "answer", "bucket"),
    [
        ("When did X go?", "7 May 2023", "I do not know.", "refusal"),
        ("When did X go?", "7 May 2023", "The context does not mention it.", "refusal"),
        ("What did Maria donate?", "old car", "Maria did not donate anything.", "refusal"),
        ("How many weeks passed between A and B?", "two weeks", "1 week passed.",
         "date_arithmetic"),
        ("How long did it take?", "four months", "About 4 months and 20 days.",
         "date_arithmetic"),
        ("When did X start?", "July 2023", "from 2023-06-26 to 2023-07-02.", "wrong_granularity"),
        ("When did X start?", "2023", "on 2023-05-07.", "wrong_granularity"),
        ("When did X go?", "7 May 2023", "X went on Sun 2023-05-07.", "date_matches_gold"),
        ("When did X go?", "27 May, 2023", "X went on 2023-05-10.", "wrong_absolute_date"),
        ("Which year did X start?", "2020", "X started in 2022.", "wrong_absolute_date"),
        ("When did X go?", "7 May 2023", "X went after the party.", "no_date_in_answer"),
        ("What habits does J practice?", "yoga, meditation, walks", "Yoga and meditation.",
         "list_missing_item"),
        ("What books has M read?", "Sapiens and Avalanche", "Sapiens, Avalanche and Dune.",
         "list_extra_item"),
        ("What city did T suggest?", "Edinburgh, Scotland", "Orlando.", "wrong_fact"),
        ("What did N do on 25 May, 2022?", "a stuffed animal", "N met a couple.",
         "wrong_fact_dated_question"),
        ("What is N's favourite game?", "Xenoblade", "Zelda.", "wrong_fact"),
    ],
)  # fmt: skip
def test_bucket_rules(question: str, gold: str, answer: str, bucket: str) -> None:
    assert fb.bucket_failure(question, gold, answer)[0] == bucket


def test_wrong_absolute_date_reports_the_midpoint_gap() -> None:
    _, detail = fb.bucket_failure("When did X?", "January 9, 2023", "on 2024-01-09")
    assert detail == "off by 365 d"


def test_gold_flags() -> None:
    turns = {
        "D1:1": SimpleNamespace(timestamp="1:00 pm on 3 July, 2023", text="We went to the lake."),
        "D2:1": SimpleNamespace(timestamp="2:00 pm on 20 July, 2023", text="I love pottery."),
    }
    last = date(2023, 7, 20)
    flags = fb.gold_flags(
        "When did X go?", "The week before 9 June 2023", "", ["D1:1"], turns, last
    )
    assert "anchor_not_session_date" in flags
    flags = fb.gold_flags(
        "When did X go?", "The week before 3 July 2023", "", ["D1:1"], turns, last
    )
    assert "anchor_not_session_date" not in flags
    flags = fb.gold_flags("When did X go?", "5 August 2023", "", ["D1:1"], turns, last)
    assert "gold_after_conversation" in flags
    flags = fb.gold_flags("What does X do?", "pottery", "X does pottery", ["D2:1"], turns, last)
    assert flags == ["answer_contains_gold"]
    flags = fb.gold_flags(
        "What does X do?", "kayaking trips", "golf", ["D2:1", "D9:9"], turns, last
    )
    assert flags == ["evidence_id_missing", "gold_not_in_evidence"]


def _dataset(tmp_path: Path) -> Path:
    sample = {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Ana",
            "speaker_b": "Ben",
            "session_1_date_time": "2:00 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ana", "dia_id": "D1:1", "text": "I went hiking yesterday."},
                {"speaker": "Ben", "dia_id": "D1:2", "text": "I like chess."},
            ],
        },
        "qa": [
            {"question": "When did Ana hike?", "answer": "7 May 2023", "evidence": ["D1:1"],
             "category": 2},
            {"question": "What does Ben like?", "answer": "chess", "evidence": ["D1:2"],
             "category": 4},
            {"question": "What game does Ben like?", "answer": "chess", "evidence": ["D1:2"],
             "category": 4},
        ],
    }  # fmt: skip
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]), encoding="utf-8")
    return path


def test_run_buckets_end_to_end(tmp_path: Path) -> None:
    ds = LoCoMoDataset(_dataset(tmp_path), revision_id="t")
    run = tmp_path / "run"
    run.mkdir()
    rows = [
        {"kind": "manifest"},
        {"kind": "result", "item_id": "conv-1", "query_id": "0-0", "type_label": "cat2",
         "status": "completed", "score": 0.0, "question": "When did Ana hike?",
         "gold": "7 May 2023", "answer": "Ana hiked on 2023-05-08.", "retrieved_ids": ["D1:1"]},
        {"kind": "result", "item_id": "conv-1", "query_id": "0-1", "type_label": "cat4",
         "status": "completed", "score": 0.0, "question": "What does Ben like?",
         "gold": "chess", "answer": "I do not know.", "retrieved_ids": []},
        {"kind": "result", "item_id": "conv-1", "query_id": "0-2", "type_label": "cat4",
         "status": "completed", "score": 1.0, "question": "What game does Ben like?",
         "gold": "chess", "answer": "chess", "retrieved_ids": ["D1:2"]},
    ]  # fmt: skip
    (run / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    failures = fb.run_buckets(run, ds, (2, 4))
    by_q = {f.query_id: f for f in failures}
    assert set(by_q) == {"0-0", "0-1"}  # the correct answer is not a failure
    assert by_q["0-0"].bucket == "wrong_absolute_date"
    assert by_q["0-0"].outcome == "read_fail"
    assert "answer is a session date" in by_q["0-0"].detail
    assert by_q["0-1"].bucket == "refusal"
    assert by_q["0-1"].outcome == "retrieval_miss"
    report = fb.render(failures, sample=15, seed=7)
    assert "| wrong_absolute_date | 1 | 0 | 0 | 1 |" in report
    assert "**cat4 / refusal** (1)" in report
