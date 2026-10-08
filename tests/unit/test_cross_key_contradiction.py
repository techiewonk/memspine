"""G-2: a new state fact retracts a contradicted fact on ANOTHER key of the same subject
when the ``invalidate_edge`` adjudicator says update / invalidate (opt-in)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import extract_graph

T0 = datetime(2023, 5, 1, tzinfo=UTC)


async def _run(verdict: str, contradictions: bool = True) -> tuple[list, dict, list]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"contradictions": contradictions}},
            },
            "episodic": {"enabled": True},
        },
    )
    await eng.start()
    calls: list[tuple[str, str]] = []

    async def adjudicate(old: str, old_from: str, new: str, new_from: str) -> str:
        calls.append((old, new))
        return verdict

    eng._build_edge_adjudicator = lambda: adjudicate  # type: ignore[method-assign]
    table = {
        "Ana: I work at Acme": [
            ExtractedEdge(
                src_entity="Ana",
                rel="works_at",
                dst_entity="Acme",
                fact="Ana works at Acme",
                kind="state",
            )
        ],
        "Ana: I retired last month": [
            ExtractedEdge(
                src_entity="Ana",
                rel="employment_status",
                dst_entity="retired",
                fact="Ana is retired",
                kind="state",
            )
        ],
    }

    async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
        return list(table.get(content, []))

    eng._extract_edges = fake
    try:
        await eng.write("Ana: I work at Acme", namespace="a", memory_type="episodic", valid_from=T0)
        stats1 = await extract_graph(eng._pipeline_ctx())
        await eng.write(
            "Ana: I retired last month",
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(days=60),
        )
        stats = await extract_graph(eng._pipeline_ctx())
        storage = eng._require_started()
        facts = [
            r
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "extract_graph" and "retract" not in r.tags
        ]
    finally:
        await eng.stop()
    assert stats1["edges_written"] == 1
    return facts, stats, calls


def _works_at(facts: list) -> object:
    return next(f for f in facts if f.attribute == "works_at")


async def test_invalidate_verdict_closes_the_old_key() -> None:
    facts, stats, calls = await _run("invalidate")
    assert calls == [("Ana works at Acme", "Ana is retired")]
    assert stats["edges_invalidated"] == 1
    assert _works_at(facts).valid_to is not None  # retracted through the ladder


@pytest.mark.parametrize("verdict", ["add", "noop"])
async def test_add_or_noop_keeps_both(verdict: str) -> None:
    facts, stats, _ = await _run(verdict)
    assert stats["edges_invalidated"] == 0
    assert _works_at(facts).valid_to is None


async def test_off_never_asks() -> None:
    _facts, stats, calls = await _run("invalidate", contradictions=False)
    assert calls == [] and stats["edges_invalidated"] == 0
