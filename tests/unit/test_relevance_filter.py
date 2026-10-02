"""H17: 3-way relevance filter with a safety net (LLM call stubbed, filter logic real)."""

from __future__ import annotations

from typing import Any

import pytest

import memspine.engine as engine_mod
from memspine import Engine
from memspine.core.records import MemoryRecord
from memspine.prompts.models import RelevanceLabel, RelevanceLabels


def _engine(**read: object) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, **read},
    )


class _StubLLM:
    roles = ("relevance",)

    def for_role(self, role: str) -> object:
        return object()


async def test_drops_only_irrelevant_outside_the_safety_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(relevance_filter=True, relevance_safety_net=1)
    await eng.start()
    try:
        cands = [
            (MemoryRecord(namespace="a", memory_type="semantic", content=f"note {i}"), s)
            for i, s in enumerate([0.9, 0.8, 0.7, 0.6])
        ]

        async def fake_call(llm: Any, prompt: Any, ctx: dict[str, Any], model: Any) -> Any:
            assert "[3] note 3" in ctx["notes"]
            return RelevanceLabels(
                labels=[
                    RelevanceLabel(index=0, label="irrelevant"),  # best-scored: safety net keeps it
                    RelevanceLabel(index=1, label="related"),
                    RelevanceLabel(index=2, label="irrelevant"),
                    RelevanceLabel(index=3, label="relevant"),
                ]
            )

        monkeypatch.setattr(engine_mod, "structured_call", fake_call)
        monkeypatch.setattr(eng, "_llm", _StubLLM())
        out = await eng._relevance_filter("q", cands)
        assert [r.content for r, _ in out] == ["note 0", "note 1", "note 3"]
    finally:
        await eng.stop()


async def test_no_role_bound_leaves_candidates_unchanged() -> None:
    eng = _engine(relevance_filter=True)
    await eng.start()
    try:
        cands = [(MemoryRecord(namespace="a", memory_type="semantic", content="x"), 0.5)]
        assert await eng._relevance_filter("q", cands) == cands
    finally:
        await eng.stop()
