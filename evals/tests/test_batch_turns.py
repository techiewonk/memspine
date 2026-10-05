"""G9: ``--memspine-batch-turns`` buffers turns of one session per write.

Buffered turns must keep their ``turn_id`` mapping (R@k scores on it), must be
written before any query (pinned mid-stream or tail) and must never share a
``write_messages`` call with another session's turns.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.contracts import DepositResult, RetrievedContext, Turn
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.judge import ExactMatchJudge
from memspine_evals.provenance import RunProtocol
from memspine_evals.readers import ScriptedReader
from memspine_evals.runner import EvalRunner, RunConfig
from memspine_evals.systems.memspine_system import MemspineSystem

_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}


def _turns() -> list[Turn]:
    lines = [
        ("s1", "Caroline", "I went to the LGBTQ support group yesterday."),
        ("s1", "Melanie", "I painted a sunrise over the lake."),
        ("s1", "Caroline", "I am researching adoption agencies."),
        ("s1", "Melanie", "My kids love the camping trips."),
        ("s1", "Caroline", "I gave a talk at the school about my journey."),
        ("s2", "Melanie", "We went camping in the mountains last weekend."),
        ("s2", "Caroline", "I bought a new guitar for my birthday."),
    ]
    return [
        Turn(
            turn_id=f"D{session[-1]}:{i}",
            session_id=session,
            speaker=speaker,
            text=text,
            timestamp=f"1:56 pm on {i + 1} May, 2023",
        )
        for i, (session, speaker, text) in enumerate(lines)
    ]


class _SpyEngine:
    """Records every write_messages batch (session id and size)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.batches: list[tuple[str, int]] = []

    async def write_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.batches.append((kwargs["session_id"], len(messages)))
        return await self.inner.write_messages(messages, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


async def _ingest(batch_turns: int) -> tuple[MemspineSystem, list[DepositResult], _SpyEngine]:
    system = MemspineSystem(config=_HASH, batch_turns=batch_turns)
    await system.reset("conv-1")
    spy = _SpyEngine(system._engine)
    system._engine = spy
    deposits = [await system.insert(turn) for turn in _turns()]
    return system, deposits, spy


pytest.importorskip("memspine")


def test_batching_keeps_turn_mapping_and_session_boundaries() -> None:
    async def run() -> tuple[Any, ...]:
        single, _, _ = await _ingest(1)
        batched, deposits, spy = await _ingest(4)
        question = "What did Caroline research about adoption and camping?"
        want = await single.query(question, 2048, 7)
        got = await batched.query(question, 2048, 7)
        await single.close()
        await batched.close()
        return deposits, spy.batches, want, got

    deposits, batches, want, got = asyncio.run(run())
    # s1: 4 turns (full buffer), then 1 turn flushed by the s2 boundary; s2's two
    # turns are flushed by the query.
    assert batches == [("s1", 4), ("s1", 1), ("s2", 2)]
    assert [d.n_records for d in deposits] == [0, 0, 0, 4, 0, 1, 0]
    assert deposits[3].meta["batched_turns"] == ["D1:0", "D1:1", "D1:2", "D1:3"]
    assert [e.turn_id for e in got.evidence] == [e.turn_id for e in want.evidence]
    assert got.text == want.text  # same content and dated rendering
    assert len(got.evidence) == len(_turns())
    assert got.meta["flushed_records"] == 2


def test_query_flushes_a_partial_buffer() -> None:
    async def run() -> RetrievedContext:
        system = MemspineSystem(config=_HASH, batch_turns=10)
        await system.reset("conv-2")
        for turn in _turns()[:2]:
            assert (await system.insert(turn)).n_records == 0
        ctx = await system.query("What did Melanie paint?", 2048, 5)
        await system.close()
        return ctx

    ctx = asyncio.run(run())
    assert {e.turn_id for e in ctx.evidence} == {"D1:0", "D1:1"}


def test_default_is_one_write_per_turn_and_keeps_the_config_hash() -> None:
    async def run() -> tuple[list[DepositResult], list[tuple[str, int]]]:
        system, deposits, spy = await _ingest(1)
        await system.close()
        return deposits, spy.batches

    deposits, batches = asyncio.run(run())
    assert all(d.n_records == 1 and "batched_turns" not in d.meta for d in deposits)
    assert [size for _, size in batches] == [1] * len(_turns())
    assert "batch_turns" not in MemspineSystem().describe()
    assert MemspineSystem(batch_turns=8).describe()["batch_turns"] == 8


class _PinnedDataset(SyntheticDataset):
    """Synthetic items whose queries are pinned to their (last) gold turn."""

    def __init__(self) -> None:
        super().__init__(n_items=2, turns_per_item=12, facts_per_item=2)
        self._items = [
            dataclasses.replace(
                item,
                queries=tuple(
                    dataclasses.replace(q, after_turn=q.gold_turn_ids[-1]) for q in item.queries
                ),
            )
            for item in self._items
        ]


def _run(system: Any, tmp_path: Path, run_id: str) -> Any:
    dataset = _PinnedDataset()
    answers = {q.text: (q.gold or "") for item in dataset.items() for q in item.queries}
    config = RunConfig(
        run_id=run_id,
        protocol=RunProtocol(protocol_id="smoke", budget_tokens=400, top_k=5, seed=11),
        out_dir=tmp_path,
        expect_model_calls=False,
    )
    runner = EvalRunner(dataset, system, ScriptedReader(answers), ExactMatchJudge(), config)

    async def go() -> Any:
        try:
            return await runner.run()
        finally:
            await system.close()

    return asyncio.run(go())


def test_runner_flushes_before_pinned_queries(tmp_path: Path) -> None:
    single = _run(MemspineSystem(config=_HASH), tmp_path, "single")
    batched = _run(MemspineSystem(config=_HASH, batch_turns=50), tmp_path, "batched")
    assert batched.recall == single.recall
    assert batched.recall["R@5"] > 0.0  # the pinned gold turn was written first


def test_runner_calls_flush_before_queries_and_build(tmp_path: Path) -> None:
    events: list[str] = []

    class _Buffering:
        system_id = "buffering"

        def describe(self) -> dict[str, Any]:
            return {"system_id": self.system_id}

        async def reset(self, item_id: str) -> None:
            events.append("reset")

        async def insert(self, turn: Turn) -> DepositResult:
            events.append("insert")
            return DepositResult()

        async def flush(self) -> DepositResult:
            events.append("flush")
            return DepositResult(n_records=1, record_ids=("r",))

        async def build(self) -> DepositResult:
            events.append("build")
            return DepositResult()

        async def query(self, text: str, budget_tokens: int, top_k: int) -> RetrievedContext:
            events.append("query")
            return RetrievedContext(text="", tokens=0)

        async def close(self) -> None:
            events.append("close")

    _run(_Buffering(), tmp_path, "order")
    for i, event in enumerate(events):
        if event in ("query", "build"):
            previous = [e for e in events[:i] if e in ("insert", "flush")]
            assert previous[-1] == "flush", events
