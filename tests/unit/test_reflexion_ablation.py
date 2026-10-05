"""#32 (GR-16): the reflexion ablation, ``memories.semantic.policies.write.reflexion``.

A counting stub ``extract_edges`` provider stands in for the LLM; no model is called.
"""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.memories.semantic.write_pipeline import SemanticWriteOptions, extraction_rounds
from memspine.services.llm.base import LLMRouter, LLMService


class _Edges:
    """Counts ``extract_edges`` calls; replies with one edge."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "stub:edges"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        return (
            "edges:\n"
            "  - {src_entity: Alice, rel: works_at, kind: state, dst_entity: Acme,"
            " fact: Alice works at Acme, confidence: 0.9}\n"
        )


def _engine(monkeypatch: pytest.MonkeyPatch, stub: _Edges, write: dict[str, Any] | None) -> Engine:
    stubs: dict[str, LLMService] = {"extract_edges": stub}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    policies: dict[str, Any] = {"write_pipeline": "graph", "extract_graph": {"max_rounds": 2}}
    if write is not None:
        policies["write"] = write
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True, "policies": policies}},
    )


@pytest.mark.parametrize(
    ("write", "calls_per_source"),
    [(None, 2), ({"reflexion": True}, 2), ({"reflexion": False}, 1)],
)
async def test_reflexion_off_saves_one_call_per_source(
    monkeypatch: pytest.MonkeyPatch, write: dict[str, Any] | None, calls_per_source: int
) -> None:
    stub = _Edges()
    eng = _engine(monkeypatch, stub, write)
    await eng.start()
    try:
        await eng.write("Alice joined Acme in March", namespace="a", memory_type="semantic")
        await eng.write("Bob moved to Lisbon last year", namespace="a", memory_type="semantic")
        assert stub.calls == 2 * calls_per_source
        edges = [
            r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if r.source.channel == "write_pipeline"
        ]
        assert edges  # the edges still land with one round
    finally:
        await eng.stop()


def test_extraction_rounds() -> None:
    assert SemanticWriteOptions().reflexion is True
    assert extraction_rounds({}) == 1
    assert extraction_rounds({"extract_graph": True}) == 1
    assert extraction_rounds({"extract_graph": {"max_rounds": 3}}) == 3
    assert extraction_rounds({"extract_graph": {"max_rounds": 0}}) == 1
    off = {"extract_graph": {"max_rounds": 3}, "write": {"reflexion": False}}
    assert extraction_rounds(off) == 1
