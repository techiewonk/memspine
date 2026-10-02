"""H24: decision port, GLiNER2 result parsing, and the read planner (fake provider)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from memspine import Engine
from memspine.services.decision import DecisionProvider
from memspine.services.decision.gliner2_decision import GLiNER2Decision, parse_choice

OPTS = {"a": "first", "b": "second"}


def test_parse_choice_shapes() -> None:
    assert parse_choice({"choice": {"label": "b", "confidence": 0.8}}, OPTS) == ("b", 0.8)
    assert parse_choice({"choice": "a"}, OPTS) == ("a", 1.0)
    with pytest.raises(ValueError):
        parse_choice({"choice": "zzz"}, OPTS)


def test_gliner2_satisfies_the_port() -> None:
    assert isinstance(GLiNER2Decision(), DecisionProvider)


class _Fake:
    provider_id = "fake"

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        self.calls += 1
        return self.label, 0.9


async def test_planner_routes_read_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "planner": "decision"},
        decision={"provider": "gliner2"},
    )
    await eng.start()
    try:
        fake = _Fake("compose")
        monkeypatch.setattr(eng, "_decision_provider", lambda: fake)
        for i in range(30):
            await eng.write(
                f"note {i} " + "word " * 30,
                namespace="a",
                memory_type="episodic",
                valid_from=datetime(2023, 5, 1 + i % 20, tzinfo=UTC),
            )
        out = await eng.read(
            "where does Ana live", namespace="a", mode="auto", budget_tokens=200, top_k=3
        )
        assert fake.calls == 1 and out.mode == "compose"
    finally:
        await eng.stop()


async def test_planner_failure_falls_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "planner": "decision"},
        decision={"provider": "gliner2"},
    )
    await eng.start()
    try:

        class _Broken:
            provider_id = "broken"

            async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
                raise RuntimeError("model unavailable")

        monkeypatch.setattr(eng, "_decision_provider", lambda: _Broken())
        assert await eng._plan_read_mode("q") is None
    finally:
        await eng.stop()
