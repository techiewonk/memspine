"""A row produced by ``evals/forensics_report.py`` validates against its JSON schema (PROC-2)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")

EVALS = Path(__file__).resolve().parents[2] / "evals"

LOCOMO = [
    {
        "sample_id": "conv-syn",
        "conversation": {
            "speaker_a": "Ana",
            "speaker_b": "Ben",
            "session_1_date_time": "2:00 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Ana", "dia_id": "D1:1", "text": "I adopted a greyhound."},
                {"speaker": "Ben", "dia_id": "D1:2", "text": "What did you name her?"},
                {"speaker": "Ana", "dia_id": "D1:3", "text": "Her name is Juno."},
            ],
        },
        "qa": [
            {
                "question": "What is the name of Ana's dog?",
                "answer": "Juno",
                "evidence": ["D1:3"],
                "category": 4,
            },
            {
                "question": "What did Ana adopt?",
                "answer": "a greyhound",
                "evidence": ["D1:1"],
                "category": 4,
            },
        ],
    }
]


def _rank(*turns: str) -> list[dict[str, Any]]:
    return [{"r": i + 1, "turn": t, "score": round(1.0 / (i + 1), 5)} for i, t in enumerate(turns)]


@pytest.fixture
def report() -> Any:
    # H6: restore sys.path exactly as it was. Importing forensics_report trims and re-appends
    # evals/, and a bare ``remove`` afterwards dropped the entry for the whole session, so
    # ``import run_chunked`` failed in every evals test that ran later in the same process.
    saved_path = list(sys.path)
    sys.path.insert(0, str(EVALS))
    try:
        spec = importlib.util.spec_from_file_location(
            "forensics_report_under_test", EVALS / "forensics_report.py"
        )
        assert spec
        assert spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # harness dependencies not installed
        pytest.skip(f"evals harness not importable: {exc}")
    finally:
        sys.path[:] = saved_path
    return mod


def test_report_rows_validate_against_schema(tmp_path: Path, report: Any) -> None:
    data = tmp_path / "locomo.json"
    data.write_text(json.dumps(LOCOMO), encoding="utf-8")

    # Learn the ids the dataset adapter assigns, then build matching results/forensics.
    ds = report.LoCoMoDataset(str(data), revision_id="auto", categories=(1, 2, 3, 4))
    items = list(ds.items())
    assert items, "synthetic dataset produced no items"
    item = items[0]
    queries = list(item.queries)
    assert len(queries) == 2

    run = tmp_path / "run-x--memspine"
    run.mkdir()
    rows: list[dict[str, Any]] = [{"kind": "manifest"}]
    fx_rows: list[dict[str, Any]] = []
    for i, q in enumerate(queries):
        gold = next(iter(q.gold_turn_ids))
        retrieved = [gold] if i == 0 else ["D1:2"]
        question = LOCOMO[0]["qa"][i]["question"]
        rows.append(
            {
                "kind": "result",
                "item_id": item.item_id,
                "query_id": q.query_id,
                "question": question,
                "type_label": "cat4",
                "gold": "Juno" if i == 0 else "a greyhound",
                "answer": "Juno" if i == 0 else "a cat",  # the second one is wrong on purpose
                "score": 1.0 if i == 0 else 0.0,
                "status": "completed",
                "retrieved_ids": retrieved,
                "context_tokens": 12,
                "latency_retrieve_ms": 3.0,
                "latency_answer_ms": 5.0,
                "meta": {},
            }
        )
        fx_rows.append(
            {
                "item": item.item_id,
                "query": question,
                "vector": _rank(gold, "D1:2"),
                "lexical": _rank("D1:2"),
                "extra_legs": {},
                "fused": _rank(gold, "D1:2"),
                "pool": _rank(gold, "D1:2"),
                "reranker": None,
                "rerank_scores": [],
                "final": _rank(gold, "D1:2") if i == 0 else _rank("D1:2"),
                "context_records": [{"turn": t, "text": f"text of {t}"} for t in retrieved],
                "context_tokens": 12,
            }
        )
    (run / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "forensics.jsonl").write_text("\n".join(json.dumps(r) for r in fx_rows), encoding="utf-8")

    out = tmp_path / "out"
    report.build(
        argparse.Namespace(
            run=str(run), forensics=str(fx), data=str(data), out=str(out), only_wrong_md=False
        )
    )

    schemas = EVALS / "schemas"
    q_schema = json.loads((schemas / "forensic_question.schema.json").read_text(encoding="utf-8"))
    r_schema = json.loads((schemas / "forensic_run.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(q_schema)
    validator = jsonschema.Draft202012Validator(q_schema)
    lines = (out / "per_question.jsonl").read_text(encoding="utf-8").splitlines()
    produced = [json.loads(line) for line in lines]
    assert len(produced) == 2
    for row in produced:
        assert not list(validator.iter_errors(row)), row
        assert row["schema_version"] == "forensic_question/v1"
        assert row["has_stage_log"] is True
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    jsonschema.validate(summary, r_schema)

    right, wrong = produced
    assert right["correct"] is True
    assert right["primary_gap"] is None
    assert wrong["correct"] is False
    assert wrong["primary_gap"] is not None


def test_schema_rejects_a_malformed_row() -> None:
    path = EVALS / "schemas" / "forensic_question.schema.json"
    validator = jsonschema.Draft202012Validator(json.loads(path.read_text(encoding="utf-8")))
    assert list(validator.iter_errors({"schema_version": "forensic_question/v1"}))
