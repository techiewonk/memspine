"""HaluMem, ConvoMem (fixture), the dataset registry, and an offline MemoryAgentBench FC run.

Every fixture under ``tests/fixtures`` is hand-written and synthetic; it copies the public
schema of its benchmark, never its data.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from memspine_evals.datasets import ConvoMemDataset, HaluMemDataset
from memspine_evals.datasets.convomem import official_metric
from memspine_evals.datasets.registry import REGISTRY, DatasetUnavailable, load

FIX = Path(__file__).resolve().parent / "fixtures"


# ── HaluMem ──────────────────────────────────────────────────────────────────


def test_halumem_maps_users_sessions_and_questions() -> None:
    ds = HaluMemDataset(FIX / "HaluMem-Medium.jsonl", revision_id="auto")
    info = ds.info()
    assert info.dataset_id == "halumem_medium"
    assert info.revision_id.startswith("sha256:")
    assert "CC BY-NC-ND" in info.licence
    (item,) = list(ds.items())
    assert item.item_id == "synthetic-user-1"
    assert len(item.history) == 6
    assert len({t.turn_id for t in item.history}) == 6  # user/assistant share dialogue_turn
    assert [t.session_id for t in item.history] == ["s0"] * 4 + ["s1"] * 2
    assert item.history[0].timestamp == "2025-01-03 09:00"
    q0, q1, q2 = item.queries
    assert q0.gold == "Pebble" and q0.type_label == "Basic Fact Recall"
    assert q0.after_turn == item.history[3].turn_id  # asked after its own session
    assert q1.after_turn == item.history[5].turn_id
    assert q1.meta["key_memory_points"] == "Ana now teaches physics."
    assert q2.meta["key_memory_points"] == ""
    assert q0.gold_turn_ids == ()  # memory-point evidence: no turn-level gold
    assert q0.meta["official_labels"] == ["Correct", "Hallucination", "Omission"]
    assert item.meta["n_memory_points"] == 3
    assert info.n_queries == 3


def test_halumem_max_users_and_missing_file(tmp_path: Path) -> None:
    ds = HaluMemDataset(FIX / "HaluMem-Medium.jsonl", revision_id="cb04336", max_users=0)
    assert list(ds.items()) == [] and ds.info().subset == "qa,max_users=0"
    with pytest.raises(FileNotFoundError):
        HaluMemDataset(tmp_path / "HaluMem-Long.jsonl", revision_id="x")


# ── ConvoMem (fixture) ───────────────────────────────────────────────────────


def _convomem_root(tmp_path: Path) -> Path:
    leaf = tmp_path / "core_benchmark" / "evidence_questions" / "user_evidence" / "1_evidence"
    leaf.mkdir(parents=True)
    shutil.copy(FIX / "convomem_user_evidence_1.json", leaf / "synthetic-person_Fixture.json")
    return tmp_path


def test_convomem_fixture_maps_evidence_and_metric(tmp_path: Path) -> None:
    root = _convomem_root(tmp_path)
    items = list(ConvoMemDataset(root, revision_id="fixture", per_stratum=5).items())
    assert len(items) == 3
    first = items[0]
    assert first.queries[0].type_label == "user_evidence/1"
    assert first.queries[0].gold == "Porto"
    assert first.queries[0].gold_turn_ids == ("synthetic-conv-0:2",)
    assert first.queries[0].meta["official_metric"] == "exact_match"
    assert all(t.timestamp is None for t in first.history)


def test_convomem_filler_is_seeded(tmp_path: Path) -> None:
    root = _convomem_root(tmp_path)
    a = list(ConvoMemDataset(root, revision_id="fixture", filler=1, seed=3).items())
    b = list(ConvoMemDataset(root, revision_id="fixture", filler=1, seed=3).items())
    assert [i.history for i in a] == [i.history for i in b]
    assert all(i.meta["filler"] == 1 for i in a)
    # gold stays in the item's own conversation; filler turns never count as gold
    for index, item in enumerate(a):
        assert item.queries[0].gold_turn_ids == (f"synthetic-conv-{index}:2",)
        assert len({t.session_id for t in item.history}) == 2


def test_convomem_official_metric_table() -> None:
    assert official_metric("preference_evidence") == "semantic_match"
    assert official_metric("implicit_connection_evidence") == "semantic_match"
    assert official_metric("changing_evidence") == "exact_match"
    assert official_metric("abstention_evidence") == "unspecified"


# ── registry ─────────────────────────────────────────────────────────────────


def test_registry_lists_statemembench_as_unreleased() -> None:
    entry = REGISTRY["statemembench"]
    assert entry.status == "unreleased" and entry.adapter is None
    with pytest.raises(DatasetUnavailable):
        load("statemembench", "anything")
    with pytest.raises(DatasetUnavailable):
        load("no-such-benchmark")


def test_registry_loads_an_adapted_dataset() -> None:
    ds = load("halumem", FIX / "HaluMem-Medium.jsonl", revision_id="auto")
    assert isinstance(ds, HaluMemDataset)
    for e in REGISTRY.values():
        assert e.licence and e.source
        assert (e.adapter is None) == (e.status == "unreleased")


# ── MemoryAgentBench FC: offline smoke (#71) ─────────────────────────────────


def _mab_parquet(path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    context = (
        "Here is a list of facts (SYNTHETIC FIXTURE):\n"
        "0. Zorbia's capital city is Old Harbour.\n"
        "1. The Quill River flows through Zorbia.\n"
        "2. Zorbia's capital city is New Harbour.\n"
    )
    table = pa.Table.from_pylist(
        [
            {
                "context": context,
                "questions": ["What is the capital of Zorbia?"],
                "answers": [["New Harbour", "NewHarbour"]],
                "metadata": {"qa_pair_ids": ["factconsolidation_sh_6k_no0"]},
            }
        ]
    )
    pq.write_table(table, path)


def test_mab_fact_consolidation_runs_offline_with_stub_llm(tmp_path: Path) -> None:
    pytest.importorskip("memspine")
    from memspine_evals.datasets import MemoryAgentBenchDataset
    from memspine_evals.judge import AliasContainsJudge
    from memspine_evals.provenance import RunProtocol
    from memspine_evals.readers import ContextOnlyReader
    from memspine_evals.runner import EvalRunner, RunConfig
    from memspine_evals.stub_llm import install_stub_litellm
    from memspine_evals.systems.memspine_system import MemspineSystem

    data = tmp_path / "Conflict_Resolution.parquet"
    _mab_parquet(data)
    ds = MemoryAgentBenchDataset(data, revision_id="auto")
    (item,) = list(ds.items())
    assert [t.turn_id for t in item.history] == ["0:0", "0:1", "0:2"]
    assert item.queries[0].type_label == "factconsolidation_sh_6k"

    config = RunConfig(
        run_id="mab-fc-smoke",
        protocol=RunProtocol(protocol_id="mab-smoke", budget_tokens=512, top_k=5, seed=0),
        out_dir=tmp_path / "runs",
        expect_model_calls=False,
    )
    system = MemspineSystem(config={"embedding": {"provider": "hash"}, "dotenv_path": None})

    async def run_and_close():  # type: ignore[no-untyped-def]
        try:
            return await EvalRunner(
                ds, system, ContextOnlyReader(), AliasContainsJudge(), config
            ).run()
        finally:
            await system.close()

    with install_stub_litellm() as stub:
        summary = asyncio.run(run_and_close())
    assert sum(stub.calls.values()) == 0 and summary.model_calls == 0
    assert summary.n_queries == 1 and summary.n_errors == 0
    # the newest fact is in the context, so the alias judge credits it
    assert summary.score_mean == 1.0
