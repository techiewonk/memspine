"""G-1: an edge whose text says it ended is written already closed (``close_ended``)."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine import Engine
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import extract_graph

T0 = datetime(2023, 5, 1, tzinfo=UTC)


async def _run(close_ended: bool) -> list:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"close_ended": close_ended}},
            },
            "episodic": {"enabled": True},
        },
    )
    await eng.start()
    try:
        edge = ExtractedEdge(
            src_entity="Ana",
            rel="lived_in",
            dst_entity="Leeds",
            fact="Ana lived in Leeds until 2022",
            kind="state",
            valid_from="2018-01-01",
            valid_to="2022-06-30",
        )

        async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
            return [edge] if "Leeds" in content else []

        eng._extract_edges = fake
        await eng.write(
            "Ana: I lived in Leeds until 2022", namespace="a", memory_type="episodic", valid_from=T0
        )
        await extract_graph(eng._pipeline_ctx())
        storage = eng._require_started()
        return [
            r
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "extract_graph"
        ]
    finally:
        await eng.stop()


def test_valid_to_parses_from_model_output() -> None:
    edge = ExtractedEdge.model_validate(
        {"src_entity": "a", "rel": "r", "dst_entity": "b", "fact": "f", "valid_to": 2022}
    )
    assert edge.valid_to == "2022"


async def test_off_keeps_the_fact_open() -> None:
    facts = await _run(False)
    assert len(facts) == 1 and facts[0].valid_to is None


async def test_on_writes_the_fact_closed() -> None:
    facts = await _run(True)
    assert len(facts) == 1
    assert facts[0].valid_to == datetime(2022, 6, 30, tzinfo=UTC)
