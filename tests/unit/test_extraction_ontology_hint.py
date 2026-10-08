"""G-5/G-6 declared ontology (relation_types filter, prompt variables) and G-8 per-write
extraction hint (``write(extraction_hint=...)``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import extract_graph

T0 = datetime(2023, 5, 1, tzinfo=UTC)


async def test_relation_types_drop_undeclared_edges_and_hints_reach_the_extractor() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"relation_types": ["works_at"]}},
            },
            "episodic": {"enabled": True},
        },
    )
    await eng.start()
    contexts: list[Any] = []

    async def fake(content: str, context: Any = None) -> list[ExtractedEdge]:
        contexts.append(context)
        return [
            ExtractedEdge(
                src_entity="Ana",
                rel="works_at",
                dst_entity="Acme",
                fact="Ana works at Acme",
                kind="state",
            ),
            ExtractedEdge(
                src_entity="Ana", rel="likes", dst_entity="tea", fact="Ana likes tea", kind="event"
            ),
        ]

    eng._extract_edges = fake
    try:
        await eng.write(
            "Ana: I work at Acme and I like tea",
            namespace="a",
            memory_type="episodic",
            valid_from=T0,
            extraction_hint="only employment facts",
        )
        stats = await extract_graph(eng._pipeline_ctx())
        storage = eng._require_started()
        facts = [
            r.content
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "extract_graph"
        ]
    finally:
        await eng.stop()
    assert facts == ["Ana works at Acme"]
    assert stats["edges_written"] == 1
    assert contexts and contexts[0].hint == "only employment facts"


def test_prompt_renders_the_ontology_and_hint() -> None:
    from memspine.prompts.registry import PromptRegistry

    prompt = PromptRegistry().get("extract_edges")
    messages = prompt.render(
        {
            "content": "x",
            "reference_time": "",
            "previous_episodes": [],
            "entities": [],
            "hint": "only employment facts",
            "entity_types": ["person", "company"],
            "relation_types": ["works_at"],
        }
    )
    text = "\n".join(m["content"] for m in messages)
    assert "relation types (use only these rel values): works_at" in text
    assert "entity types (use only these kinds of entities): person, company" in text
    assert "extraction instructions from the writer: only employment facts" in text
