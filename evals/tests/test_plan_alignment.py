"""Requirements taken from `paper_spine/EVALUATION_PLAN_2026-09.md`.

Each test here exists because the plan names the failure it prevents.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ContainsJudge, ExactMatchJudge, JudgeScale
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ContextOnlyReader
from memspine_evals.results import ResultRow, RowStatus, cluster_bootstrap_ci, read_run
from memspine_evals.runner import EvalRunner, ModelCallBudgetExceeded, RunConfig
from memspine_evals.split import Split, SplitView, make_split
from memspine_evals.systems import BM25Retriever, NoMemorySystem, VerbatimSystem
from memspine_evals.systems.retrievers import HybridRetriever, Unit

PROTOCOL = RunProtocol(protocol_id="plan", budget_tokens=4096, top_k=10, seed=11)


class CountingReader:
    """Answers for a fixed number of calls, then keeps asking for more."""

    reader_id = "counting"
    model = "local-test"
    makes_model_calls = True

    def describe(self):
        return {"reader_id": self.reader_id}

    async def answer(self, question: str, context: str) -> ReaderAnswer:
        return ReaderAnswer(text="an answer", model_calls=1, completion_tokens=2)


def test_stopping_early_still_accounts_for_every_scheduled_question(tmp_path: Path) -> None:
    """The native run lost 14 slots to an abort; the denominator has to survive."""
    dataset = SyntheticDataset(n_items=3, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id="capped",
        protocol=PROTOCOL,
        out_dir=tmp_path,
        expect_model_calls=True,
        max_model_calls=3,
    )
    runner = EvalRunner(dataset, VerbatimSystem(), CountingReader(), ExactMatchJudge(), config)
    with pytest.raises(ModelCallBudgetExceeded):
        asyncio.run(runner.run())

    _, rows, summary = read_run(tmp_path / "capped" / "results.jsonl")
    assert len(rows) == 6  # 3 items x 2 questions: every scheduled slot is present
    statuses = [r["status"] for r in rows]
    assert statuses.count(RowStatus.COMPLETED.value) == 3
    assert statuses.count(RowStatus.UNATTEMPTED.value) == 3
    assert summary["n_scheduled"] == 6
    assert summary["n_unattempted"] == 3
    assert summary["n_queries"] == 3  # only completed rows enter the mean


def test_errors_are_recorded_not_scored_as_wrong(tmp_path: Path) -> None:
    class BrokenSystem(VerbatimSystem):
        async def query(self, text: str, budget_tokens: int, top_k: int):
            raise RuntimeError("retrieval backend down")

    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id="broken", protocol=PROTOCOL, out_dir=tmp_path, expect_model_calls=False
    )
    runner = EvalRunner(dataset, BrokenSystem(), ContextOnlyReader(), ContainsJudge(), config)
    summary = asyncio.run(runner.run())
    assert summary.n_scheduled == 2
    assert summary.n_errors == 2
    assert summary.n_queries == 0  # nothing measurable, and the mean says so
    assert summary.score_mean == 0.0


def test_rows_are_self_describing(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=1)
    config = RunConfig(
        run_id="ident", protocol=PROTOCOL, out_dir=tmp_path, expect_model_calls=False
    )
    runner = EvalRunner(dataset, VerbatimSystem(), ContextOnlyReader(), ContainsJudge(), config)
    asyncio.run(runner.run())
    _, rows, _ = read_run(tmp_path / "ident" / "results.jsonl")
    row = rows[0]
    assert row["protocol_id"] == "plan"
    assert row["dataset_revision"].startswith("gen-v1")
    assert row["system_id"] == "verbatim-bm25"
    assert row["seed"] == 11


def test_confidence_interval_is_clustered_by_item() -> None:
    """Two conversations, one perfect and one failing, is a wide interval —
    not the narrow one you get from pretending questions are independent."""
    rows = [
        ResultRow(
            run_id="r",
            item_id="convA",
            query_id=f"a{i}",
            question="q",
            gold="g",
            answer="a",
            score=1.0,
            scale="binary",
        )
        for i in range(50)
    ] + [
        ResultRow(
            run_id="r",
            item_id="convB",
            query_id=f"b{i}",
            question="q",
            gold="g",
            answer="a",
            score=0.0,
            scale="binary",
        )
        for i in range(50)
    ]
    lo, hi = cluster_bootstrap_ci(rows, JudgeScale.BINARY, seed=1)
    assert lo == 0.0 and hi == 1.0  # the design supports no more precision than this


def test_single_cluster_reports_a_degenerate_interval() -> None:
    rows = [
        ResultRow(
            run_id="r",
            item_id="only",
            query_id="q1",
            question="q",
            gold="g",
            answer="a",
            score=1.0,
            scale="binary",
        )
    ]
    assert cluster_bootstrap_ci(rows, JudgeScale.BINARY) == (1.0, 1.0)


def test_no_memory_control_returns_nothing() -> None:
    system = NoMemorySystem()

    async def go():
        await system.reset("i")
        from memspine_evals.contracts import Turn

        await system.insert(Turn(turn_id="t0", session_id="s", speaker="user", text="secret"))
        return await system.query("what is the secret", 4096, 5)

    context = asyncio.run(go())
    assert context.text == ""
    assert context.evidence == ()


def test_hybrid_fuses_both_legs_by_rank() -> None:
    class StubDense:
        retriever_id = "stub-dense"

        def __init__(self) -> None:
            self.units: list[Unit] = []

        def describe(self):
            return {"retriever_id": self.retriever_id, "dense": True}

        def add(self, unit: Unit) -> None:
            self.units.append(unit)

        def clear(self) -> None:
            self.units.clear()

        def search(self, query: str, top_k: int):
            # deliberately the reverse of lexical relevance
            return [(unit, 1.0) for unit in reversed(self.units)][:top_k]

    sparse, dense = BM25Retriever(), StubDense()
    hybrid = HybridRetriever(sparse, dense)
    for i, text in enumerate(["walnut allergy", "weather today", "lisbon holiday"]):
        hybrid.add(Unit(unit_id=f"u{i}", text=text, turn_ids=(f"t{i}",)))
    hits = hybrid.search("walnut allergy", top_k=3)
    assert {unit.unit_id for unit, _ in hits} == {"u0", "u1", "u2"}
    assert hits[0][0].unit_id in {"u0", "u2"}  # both legs' top-1 survive fusion
    assert "hybrid-rrf" in hybrid.describe()["retriever_id"]


def test_split_reserves_whole_items_and_is_reproducible(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=10, turns_per_item=6, facts_per_item=1)
    a = make_split(dataset, n_dev_items=2, seed=3)
    b = make_split(dataset, n_dev_items=2, seed=3)
    assert a.dev_items == b.dev_items
    assert len(a.dev_items) == 2
    assert not set(a.dev_items) & set(a.heldout_items)
    assert len(a.heldout_items) == 8
    assert a.category_balance["dev"]


def test_split_round_trips_and_refuses_other_bytes(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=6, turns_per_item=6, facts_per_item=1)
    split = make_split(dataset, n_dev_items=2, seed=5, note="pre-tuning reservation")
    path = split.save(tmp_path / "split.json")
    loaded = Split.load(path)
    assert loaded.dev_items == split.dev_items
    assert json.loads(path.read_text(encoding="utf-8"))["note"] == "pre-tuning reservation"

    other = SyntheticDataset(n_items=6, turns_per_item=7, facts_per_item=1)
    with pytest.raises(ValueError, match="does not transfer"):
        SplitView(other, loaded)


def test_split_view_labels_the_side_in_the_subset() -> None:
    dataset = SyntheticDataset(n_items=6, turns_per_item=6, facts_per_item=1)
    split = make_split(dataset, n_dev_items=2, seed=5)
    heldout = SplitView(dataset, split, side="heldout")
    dev = SplitView(dataset, split, side="dev")
    assert heldout.info().n_items == 4
    assert dev.info().n_items == 2
    assert heldout.info().subset.endswith("split:heldout")
    assert {item.item_id for item in dev.items()} == set(split.dev_items)


def test_split_cannot_keep_everything() -> None:
    dataset = SyntheticDataset(n_items=3, turns_per_item=4, facts_per_item=1)
    with pytest.raises(ValueError, match="not a split"):
        make_split(dataset, n_dev_items=3)


def test_truncated_answers_are_flagged_and_still_scored(tmp_path: Path) -> None:
    class TruncatingReader:
        reader_id = "truncating"
        model = "local-test"
        makes_model_calls = False

        def describe(self):
            return {"reader_id": self.reader_id}

        async def answer(self, question: str, context: str) -> ReaderAnswer:
            return ReaderAnswer(text="Lis", truncated=True, finish_reason="length")

    dataset = SyntheticDataset(n_items=1, turns_per_item=6, facts_per_item=1)
    config = RunConfig(
        run_id="trunc", protocol=PROTOCOL, out_dir=tmp_path, expect_model_calls=False
    )
    runner = EvalRunner(dataset, VerbatimSystem(), TruncatingReader(), ContainsJudge(), config)
    summary = asyncio.run(runner.run())
    assert summary.n_truncated == 1
    assert summary.n_queries == 1  # truncation is a caveat on the row, not a deletion
    _, rows, _ = read_run(tmp_path / "trunc" / "results.jsonl")
    assert rows[0]["answer_truncated"] is True


def test_failed_answers_stay_in_the_headline_denominator(tmp_path: Path) -> None:
    """Contract: a known failed answer takes the declared failure score; it is
    not deleted from the denominator. The measured mean is reported beside it."""

    class HalfBrokenSystem(VerbatimSystem):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def query(self, text: str, budget_tokens: int, top_k: int):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("retrieval backend down")
            return await super().query(text, budget_tokens, top_k)

    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = RunConfig(
        run_id="halfbroken", protocol=PROTOCOL, out_dir=tmp_path, expect_model_calls=False
    )
    runner = EvalRunner(dataset, HalfBrokenSystem(), ContextOnlyReader(), ContainsJudge(), config)
    summary = asyncio.run(runner.run())

    assert summary.n_scheduled == 2
    assert summary.n_errors == 1
    assert summary.n_queries == 1
    assert summary.score_mean_measured == 1.0  # the one gradeable answer was right
    assert summary.score_mean == 0.5  # the failure is still in the denominator
    assert summary.failure_score == 0.0


def test_unknown_deposit_cost_makes_the_total_incomplete() -> None:
    from memspine_evals.metrics import Ledger, Stage

    ledger = Ledger()
    ledger.add(Stage.GENERATE, prompt_tokens=100, calls=1, cost_usd=0.01)
    assert ledger.complete is True
    ledger.mark_unknown(Stage.DEPOSIT, "facade does not report write-path calls")
    assert ledger.complete is False
    payload = ledger.to_dict()
    assert payload["complete"] is False
    assert "facade" in payload["unknown"]["D"]


def test_cpc_denominator_counts_attempted_cycles_only() -> None:
    from memspine_evals.metrics import Ledger, Stage

    ledger = Ledger()
    ledger.add(Stage.GENERATE, cost_usd=1.0)
    # four attempted cycles, whatever else was scheduled but never ran
    assert ledger.cost_per_cycle(4) == pytest.approx(0.25)
    assert ledger.cost_per_cycle(0) == 0.0
