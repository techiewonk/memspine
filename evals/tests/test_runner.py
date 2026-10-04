"""End-to-end: a whole run, offline, with the guards proved.

These tests are the reason the harness can be trusted before any dataset is
downloaded — the loop, the budget, the provenance, the trace and the call
guards are all exercised here with zero model calls.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ContainsJudge, ExactMatchJudge, JudgeScale
from memspine_evals.metrics import CostModel, Price
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ContextOnlyReader, ScriptedReader
from memspine_evals.results import ResultRow, aggregate, read_run
from memspine_evals.runner import (
    EvalRunner,
    ModelCallBudgetExceeded,
    RunConfig,
    UnexpectedModelCall,
    run_matrix,
)
from memspine_evals.systems import FullContextSystem, NaiveRAGSystem, VerbatimSystem

PROTOCOL = RunProtocol(protocol_id="smoke", budget_tokens=400, top_k=5, seed=11)


def scripted_from(dataset: SyntheticDataset) -> ScriptedReader:
    answers = {q.text: (q.gold or "") for item in dataset.items() for q in item.queries}
    return ScriptedReader(answers)


def run(system, reader, judge, tmp_path: Path, **config_kwargs):
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=2)
    config = RunConfig(
        run_id=config_kwargs.pop("run_id", "test-run"),
        protocol=PROTOCOL,
        out_dir=tmp_path,
        expect_model_calls=config_kwargs.pop("expect_model_calls", False),
        **config_kwargs,
    )
    runner = EvalRunner(dataset, system, reader, judge, config)
    return asyncio.run(runner.run()), runner, config


def test_full_run_produces_a_provenanced_result_file(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=2)
    summary, _runner, _config = run(
        VerbatimSystem(), scripted_from(dataset), ExactMatchJudge(), tmp_path
    )
    manifest, rows, written_summary = read_run(tmp_path / "test-run" / "results.jsonl")

    assert manifest["kind"] == "manifest"
    assert manifest["dataset"]["revision_id"]
    assert manifest["judge"]["scale"] == "binary"
    assert len(rows) == summary.n_queries == 4
    assert written_summary["score_mean"] == summary.score_mean
    assert summary.model_calls == 0


def test_scripted_answers_score_perfectly_and_retrieval_is_measured(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=2)
    summary, _, _ = run(VerbatimSystem(), scripted_from(dataset), ExactMatchJudge(), tmp_path)
    assert summary.score_mean == 1.0  # the reader is scripted; this tests plumbing only
    assert summary.recall["R@5"] > 0.0  # BM25 does find the planted turns
    assert summary.n_errors == 0


def test_retrieval_sufficiency_mode_needs_no_generation(tmp_path: Path) -> None:
    """Context-only reader + contains judge = 'was the answer retrievable at
    all', which is the measurement MemPalace's headline actually reports."""
    summary, _, _ = run(VerbatimSystem(), ContextOnlyReader(), ContainsJudge(), tmp_path)
    assert 0.0 <= summary.score_mean <= 1.0
    assert summary.model_calls == 0
    assert summary.admissible_d16 is False  # no backbone => not an answer metric
    assert "reader.model" in summary.missing_protocol_fields


def test_full_context_recall_is_unavailable_not_perfect(tmp_path: Path) -> None:
    summary, _, _ = run(FullContextSystem(), ContextOnlyReader(), ContainsJudge(), tmp_path)
    assert summary.recall == {}  # unranked: R@k is not reported at all


def test_offline_run_rejects_a_model_call(tmp_path: Path) -> None:
    class CallingReader:
        reader_id = "fake-api"
        model = "gpt-4o"
        makes_model_calls = True

        def describe(self):
            return {"reader_id": self.reader_id}

        async def answer(self, question: str, context: str) -> ReaderAnswer:
            return ReaderAnswer(text="x", model_calls=1)

    with pytest.raises(UnexpectedModelCall):
        run(
            VerbatimSystem(),
            CallingReader(),
            ExactMatchJudge(),
            tmp_path,
            expect_model_calls=False,
            fail_fast=True,
        )


def test_model_call_cap_stops_the_run(tmp_path: Path) -> None:
    class CallingReader:
        reader_id = "fake-api"
        model = "gpt-4o"
        makes_model_calls = True

        def describe(self):
            return {"reader_id": self.reader_id}

        async def answer(self, question: str, context: str) -> ReaderAnswer:
            return ReaderAnswer(text="x", model_calls=1)

    with pytest.raises(ModelCallBudgetExceeded):
        run(
            VerbatimSystem(),
            CallingReader(),
            ExactMatchJudge(),
            tmp_path,
            expect_model_calls=True,
            max_model_calls=2,
            fail_fast=True,
        )


def test_trace_carries_the_loop_tuple(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=1)
    config = RunConfig(
        run_id="trace-run",
        protocol=PROTOCOL,
        out_dir=tmp_path,
        expect_model_calls=False,
        include_trace_content=True,
    )
    runner = EvalRunner(dataset, VerbatimSystem(), ContextOnlyReader(), ContainsJudge(), config)
    asyncio.run(runner.run())

    raw = (tmp_path / "trace-run" / "trace.jsonl").read_text(encoding="utf-8")
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
    deposits = [e for e in entries if e["kind"] == "deposit"]
    cycles = [e for e in entries if e["kind"] == "cycle"]
    assert len(deposits) == 8
    assert len(cycles) == 1
    cycle = cycles[0]
    assert {"E_t", "M_ctx_t", "y_t", "P_u_t"} <= set(cycle)
    assert cycle["M_ctx_t"]["sha256"] and cycle["M_ctx_t"]["text"]
    assert cycle["y_t"]["sha256"]


def test_summary_file_holds_the_score_matrix_row(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=1)
    _, _runner, _ = run(
        VerbatimSystem(), scripted_from(dataset), ExactMatchJudge(), tmp_path, run_id="row-run"
    )
    payload = json.loads((tmp_path / "row-run" / "summary.json").read_text(encoding="utf-8"))
    row = payload["score_matrix_row"]
    assert row["judge_scale"] == "binary"
    assert row["data_revision"].startswith("gen-v1-seed")
    assert row["evidence_class"] == "harness-run"


def test_matrix_run_shares_one_protocol(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=10, facts_per_item=2)
    systems = [FullContextSystem(), VerbatimSystem(), NaiveRAGSystem(chunk_chars=300)]
    config = RunConfig(
        run_id="matrix",
        protocol=PROTOCOL,
        out_dir=tmp_path,
        expect_model_calls=False,
        cost_model=CostModel(model_prices={"none": Price()}),
    )
    summaries = asyncio.run(
        run_matrix(dataset, systems, ContextOnlyReader(), ContainsJudge(), config)
    )
    assert [s.run_id for s in summaries] == [
        "matrix--full-context",
        "matrix--verbatim-bm25",
        "matrix--naive-rag-bm25",
    ]
    assert all(s.n_errors == 0 for s in summaries)
    for summary in summaries:
        manifest, _, _ = read_run(tmp_path / summary.run_id / "results.jsonl")
        assert manifest["protocol"]["budget_tokens"] == 400
        assert manifest["protocol"]["seed"] == 11


def test_aggregate_refuses_to_pool_scales() -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=4, facts_per_item=1)
    from memspine_evals.judge import JudgeSpec
    from memspine_evals.metrics import Ledger
    from memspine_evals.provenance import ReaderSpec, RunManifest, SystemSpec

    manifest = RunManifest.build(
        run_id="mixed",
        dataset=dataset.info(),
        system=SystemSpec(system_id="s"),
        reader=ReaderSpec(reader_id="r", model="m", makes_model_calls=False),
        judge=JudgeSpec(judge_id="j", scale=JudgeScale.BINARY),
        protocol=PROTOCOL,
        token_counter={"counter_id": "heuristic-chars4"},
    )
    rows = [
        ResultRow(
            run_id="mixed",
            item_id="i",
            query_id="q1",
            question="q",
            gold="g",
            answer="a",
            score=1.0,
            scale="binary",
        ),
        ResultRow(
            run_id="mixed",
            item_id="i",
            query_id="q2",
            question="q",
            gold="g",
            answer="a",
            score=0.7,
            scale="graded_01",
        ),
    ]
    with pytest.raises(ValueError, match="mixed judge scales"):
        aggregate(manifest, rows, Ledger())
