"""F-13c: the adapter on the public API returns what the pre-port adapter returned.

``fixtures/adapter_parity_snapshot.json`` was generated from the base commit (before the port,
engine called directly) with ``python evals/tests/test_adapter_parity.py --write``. The test
re-runs the same capture on the current adapter (hash embedder, synthetic dataset, no model)
and asserts the contexts, evidence and cost fields are identical. A stub reader/judge sits
above the adapter, so the adapter's output is the whole surface that can change.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("memspine")
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.systems.memspine_system import MemspineSystem

SNAPSHOT = Path(__file__).parent / "fixtures" / "adapter_parity_snapshot.json"
_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}
_ARMS: dict[str, dict[str, Any]] = {
    "assemble": {},
    "read-auto": {"read_mode": "auto"},
    "batched": {"batch_turns": 8},
}


async def capture() -> dict[str, Any]:
    out: dict[str, Any] = {}
    items = list(SyntheticDataset(n_items=2, turns_per_item=16, facts_per_item=3).items())
    for arm, kwargs in _ARMS.items():
        rows: list[Any] = []
        system = MemspineSystem(config=dict(_HASH), **kwargs)
        for item in items:
            await system.reset(item.item_id)
            for turn in item.history:
                await system.insert(turn)
            built = await system.build()
            rows.append(["build", built.n_records, built.model_calls])
            for query in item.queries:
                ctx = await system.query(query.text, 1024, 5)
                rows.append(
                    [
                        query.query_id,
                        ctx.text,
                        ctx.tokens,
                        ctx.truncated,
                        [e.turn_id for e in ctx.evidence],
                    ]
                )
        await system.close()
        out[arm] = rows
    return out


def test_adapter_matches_pre_port_snapshot() -> None:
    want = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(asyncio.run(capture())))
    assert got == want


if __name__ == "__main__":  # regenerate only from the base commit
    if "--write" in sys.argv:
        SNAPSHOT.write_text(
            json.dumps(asyncio.run(capture()), indent=1, sort_keys=True), encoding="utf-8"
        )
