"""MemoryAgentBench FactConsolidation adapter + alias judge."""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.judge import AliasContainsJudge

DATA = Path(__file__).resolve().parents[1] / "data" / "mab" / "Conflict_Resolution.parquet"


async def test_alias_judge_matches_any_alias() -> None:
    judge = AliasContainsJudge()
    assert (await judge.score("q", "It is Rugby union.", "rugby || football")).score == 1.0
    assert (await judge.score("q", "cricket", "rugby || football")).score == 0.0


@pytest.mark.skipif(not DATA.exists(), reason="MemoryAgentBench not fetched")
def test_facts_become_ordered_turns() -> None:
    pytest.importorskip("pyarrow")
    from memspine_evals.datasets import MemoryAgentBenchDataset

    ds = MemoryAgentBenchDataset(DATA, revision_id="7ea0669")
    items = list(ds.items())
    assert items
    first = items[0]
    serials = [int(t.turn_id.split(":")[1]) for t in first.history]
    assert serials == sorted(serials)
    times = [t.timestamp for t in first.history]
    assert times == sorted(times)  # newer serial -> later event time
    assert all(q.gold for q in first.queries)
    assert first.queries[0].type_label.startswith("factconsolidation_")
