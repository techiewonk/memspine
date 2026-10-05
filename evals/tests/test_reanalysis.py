"""N10: offline re-analysis of finished runs (error_analysis.py, temporal_check.py).

The end-to-end test runs the real harness offline (naive RAG + verbatim, context-only reader,
contains judge), then re-scores the run from its files alone: the rebuilt contexts must match
the trace's SHA-256 and the unit-ranked recall must equal what the runner recorded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import error_analysis as ea
import pytest
import temporal_check
from memspine_evals.contracts import Turn
from memspine_evals.datasets import LoCoMoDataset
from memspine_evals.judge import ContainsJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ContextOnlyReader
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems import NaiveRAGSystem, VerbatimSystem
from memspine_evals.tokens import HeuristicTokenCounter


def _locomo(tmp_path: Path) -> Path:
    words = ["juno", "lyon", "piano", "kayak", "tea", "chess", "paris", "violin"]
    turns = [
        {"speaker": "Ana", "dia_id": f"D1:{i + 1}", "text": f"I like {w} a lot, really {w}."}
        for i, w in enumerate(words)
    ]
    sample = [
        {
            "sample_id": "conv-1",
            "conversation": {
                "speaker_a": "Ana",
                "speaker_b": "Ben",
                "session_1_date_time": "2:00 pm on 8 May, 2023",
                "session_1": turns,
            },
            "qa": [
                {
                    "question": "What about juno?",
                    "answer": "juno",
                    "evidence": ["D1:1"],
                    "category": 4,
                },
                {
                    "question": "violin and paris?",
                    "answer": "violin",
                    "evidence": ["D1:7", "D1:8"],
                    "category": 1,
                },
                {"question": "kayak?", "answer": "kayak", "evidence": ["D1:4"], "category": 2},
                {
                    "question": "Ben's cat?",
                    "adversarial_answer": "a dog",
                    "evidence": [],
                    "category": 5,
                },
            ],
        }
    ]
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    return path


def _run(tmp_path: Path, system: object, run_id: str, budget: int = 400) -> Path:
    ds = LoCoMoDataset(_locomo(tmp_path), revision_id="t")
    protocol = RunProtocol(protocol_id="n10", budget_tokens=budget, top_k=3, seed=1)
    runner = EvalRunner(
        ds,
        system,
        ContextOnlyReader(),
        ContainsJudge(),
        RunConfig(run_id=run_id, protocol=protocol, out_dir=tmp_path, expect_model_calls=False),
    )
    asyncio.run(runner.run())
    return tmp_path / run_id


def test_n10_rebuild_units_splits_chunks_on_overlap_and_repeats() -> None:
    position = {f"t{i}": i for i in range(8)}
    evidence = [
        {"turn_id": "t0", "score": 2.0},
        {"turn_id": "t1", "score": 2.0},
        {"turn_id": "t1", "score": 2.0},
        {"turn_id": "t2", "score": 2.0},  # overlap turn
        {"turn_id": "t5", "score": 1.0},
        {"turn_id": "t6", "score": 1.0},
        {"turn_id": "t5", "score": 1.0},
        {"turn_id": "t6", "score": 1.0},  # tail duplicate
    ]
    units = ea.rebuild_units(evidence, position, chunked=True)
    assert units == [["t0", "t1"], ["t1", "t2"], ["t5", "t6"], ["t5", "t6"]]
    assert ea.tail_duplicates(units, 10) == 1
    assert ea.rebuild_units(evidence[:2], position, chunked=False) == [["t0"], ["t1"]]
    r = ea.unit_recall(units, ("t2", "t5"), (1, 2, 3))
    assert (r["R@1"], r["R@2"], r["R_all@2"], r["R_all@3"]) == (0.0, 1.0, 0.0, 1.0)


def test_n10_truncation_drops_evidence_past_the_cut() -> None:
    turns = {
        "a": Turn("a", "s", "u", "x" * 40),
        "b": Turn("b", "s", "u", "y" * 400),
    }
    text, spans, cut = ea.rebuild_context([["a"], ["b"]], turns, 30, HeuristicTokenCounter())
    assert cut
    visible = {tid for tid, _, end in spans if end <= len(text)}
    assert visible == {"a"}


@pytest.mark.parametrize("system_cls", [NaiveRAGSystem, VerbatimSystem])
def test_n10_reanalysis_reproduces_the_runner(tmp_path: Path, system_cls: type) -> None:
    system = system_cls(chunk_chars=120) if system_cls is NaiveRAGSystem else system_cls()
    run = _run(tmp_path, system, f"r-{system_cls.__name__}")
    ds = LoCoMoDataset(_locomo(tmp_path), revision_id="t")
    report = ea.reanalyse(run, ds, ks=(1, 5, 10))
    assert report["kind"] == ("chunked" if system_cls is NaiveRAGSystem else "turn")
    assert report["n_rows"] == 3  # cat 5 dropped
    assert report["contexts_rebuilt"] == report["contexts_hash_verified"] == 3
    # The current runner already records unit-ranked recall: the offline rebuild agrees.
    assert report["recall_unit_level"] == pytest.approx(
        {k: v for k, v in report["recall_turn_level_old"].items()}
    )
    assert report["evidence_dropped_rows"] == 0


def test_n10_temporal_check_splits_a_run_by_resolver_coverage(tmp_path: Path) -> None:
    sample = [
        {
            "sample_id": "c",
            "conversation": {
                "speaker_a": "A",
                "speaker_b": "B",
                "session_1_date_time": "2:00 pm on 15 July, 2023",
                "session_1": [
                    {"speaker": "A", "dia_id": "D1:1", "text": "I ran a race last Friday."}
                ],
            },
            "qa": [
                {
                    "question": "When?",
                    "answer": "14 July 2023",
                    "evidence": ["D1:1"],
                    "category": 2,
                },
                {"question": "When?", "answer": "sometime", "evidence": [], "category": 2},
            ],
        }
    ]
    path = tmp_path / "l.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    ds = LoCoMoDataset(path, revision_id="t")
    r = temporal_check.evaluate(ds)
    run = tmp_path / "run"
    run.mkdir()
    qids = [q.query_id for q in next(ds.items()).queries]
    rows = [
        {
            "kind": "result",
            "status": "completed",
            "item_id": "c",
            "query_id": qids[0],
            "score": 1.0,
        },
        {
            "kind": "result",
            "status": "completed",
            "item_id": "c",
            "query_id": qids[1],
            "score": 0.0,
        },
    ]
    (run / "results.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    split = temporal_check.run_split(run, r["groups"])
    assert split["covered"] == (1.0, 1) and split["exact_day"] == (1.0, 1)
    assert split["not_covered"] == (0.0, 1)


def test_g13_gold_phrasing_needs_the_anchored_relation() -> None:
    """G13: a relative gold's relation and anchor day are matched by an anchored
    resolution of the evidence; calendar resolutions state no relation."""
    from datetime import date

    from memspine.core.temporal_resolve import resolve

    assert temporal_check.relation_key("The week before 9 June 2023") == (
        "week before",
        date(2023, 6, 9),
    )
    assert temporal_check.relation_key("A few days before May 24, 2023.") == (
        "few days before",
        date(2023, 5, 24),
    )
    assert temporal_check.relation_key("Last week before 13 October 2022.") == (
        "week before",
        date(2022, 10, 13),
    )
    assert temporal_check.relation_key("7 May 2023") is None
    said = date(2023, 6, 9)
    anchored = resolve("I went there last week", said, anchored=True)
    calendar = resolve("I went there last week", said)
    assert temporal_check.has_gold_phrasing("The week before 9 June 2023", anchored)
    assert not temporal_check.has_gold_phrasing("The week before 9 June 2023", calendar)
    assert not temporal_check.has_gold_phrasing("The week before 10 June 2023", anchored)
