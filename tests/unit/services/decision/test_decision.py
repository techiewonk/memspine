"""H24: decision port, GLiNER2 adapter (fake gliner2 module), and the read planner."""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from memspine import Engine
from memspine.exceptions import MissingServiceError
from memspine.services.decision import DecisionProvider
from memspine.services.decision.gliner2_decision import (
    DEFAULT_MODEL,
    GLiNER2Decision,
    gliner2_class,
    parse_choice,
)

OPTS = {"a": "first", "b": "second"}


def test_parse_choice_shapes() -> None:
    assert parse_choice({"choice": {"label": "b", "confidence": 0.8}}, OPTS) == ("b", 0.8)
    assert parse_choice({"choice": "a"}, OPTS) == ("a", 1.0)
    with pytest.raises(ValueError):
        parse_choice({"choice": "zzz"}, OPTS)


def test_gliner2_satisfies_the_port() -> None:
    assert isinstance(GLiNER2Decision(), DecisionProvider)


# ── fake gliner2 module ──────────────────────────────────────────────────────


class _Schema:
    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def classification(self, name: str, labels: Any) -> _Schema:
        self.fields[name] = labels
        return self


class _FakeModel:
    """Mimics ``GLiNER2``: ``from_pretrained`` + ``create_schema`` + ``extract``."""

    loaded: ClassVar[list[str]] = []

    @classmethod
    def from_pretrained(cls, model_id: str) -> _FakeModel:
        cls.loaded.append(model_id)
        return cls()

    def create_schema(self) -> _Schema:
        return _Schema()

    def extract(self, text: str, schema: _Schema, **kw: Any) -> dict[str, Any]:
        assert schema.fields["choice"] == OPTS
        return {"choice": {"label": "b", "confidence": 0.7}}


class _ClassifyOnly:
    """A gliner2 build exposing only ``classify_text`` (and no confidence kwarg)."""

    @classmethod
    def from_pretrained(cls, model_id: str) -> _ClassifyOnly:
        return cls()

    def classify_text(self, text: str, tasks: Mapping[str, Any]) -> dict[str, Any]:
        return {"choice": "a"}


def _fake_gliner2(monkeypatch: pytest.MonkeyPatch, cls: type, name: str = "GLiNER2") -> None:
    module = types.ModuleType("gliner2")
    setattr(module, name, cls)
    monkeypatch.setitem(sys.modules, "gliner2", module)


def _no_gliner2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gliner2", None)  # import raises ImportError


async def test_adapter_uses_gliner2_class_and_default_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeModel.loaded = []
    _fake_gliner2(monkeypatch, _FakeModel)
    provider = GLiNER2Decision()
    assert await provider.choose("q", OPTS) == ("b", 0.7)
    assert _FakeModel.loaded == [DEFAULT_MODEL] == ["fastino/gliner2-base-v1"]


async def test_adapter_falls_back_to_autoextractor_and_classify_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gliner2(monkeypatch, _ClassifyOnly, name="AutoExtractor")
    assert await GLiNER2Decision().choose("q", OPTS) == ("a", 1.0)


def test_gliner2_class_missing_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_gliner2(monkeypatch)
    with pytest.raises(MissingServiceError) as info:
        gliner2_class()
    assert info.value.extra == "ner"


async def test_load_failure_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class _Broken:
        @classmethod
        def from_pretrained(cls, model_id: str) -> Any:
            calls["n"] += 1
            raise OSError("no weights")

    _fake_gliner2(monkeypatch, _Broken)
    provider = GLiNER2Decision()
    for _ in range(3):
        with pytest.raises(OSError):
            await provider.choose("q", OPTS)
    assert calls["n"] == 1


def test_load_is_serialised_by_a_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    calls = {"n": 0}

    class _Slow(_FakeModel):
        @classmethod
        def from_pretrained(cls, model_id: str) -> _Slow:
            calls["n"] += 1
            gate.wait(0.2)
            return cls()

    _fake_gliner2(monkeypatch, _Slow)
    provider = GLiNER2Decision()
    threads = [threading.Thread(target=provider._load) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert calls["n"] == 1


# ── engine start validation + planner ─────────────────────────────────────────


def _engine(**extra: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "planner": "decision"},
        decision={"provider": "gliner2"},
        **extra,
    )


async def test_start_raises_when_gliner2_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_gliner2(monkeypatch)
    with pytest.raises(MissingServiceError) as info:
        await _engine().start()
    assert info.value.extra == "ner"


async def test_start_lenient_turns_decision_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_gliner2(monkeypatch)
    eng = _engine(strict_services=False)
    await eng.start()
    try:
        assert eng._decision_provider() is None
        assert await eng._plan_read_mode("how many cities") is None
    finally:
        await eng.stop()


class _Fake:
    provider_id = "fake"

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        self.calls += 1
        return self.label, 0.9


async def test_planner_routes_read_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = _engine(memories={"episodic": {"enabled": True}})
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
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = _engine()
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


def test_ner_adapter_uses_the_same_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.memories.semantic.entities import GlinerEntityExtractor

    _FakeModel.loaded = []
    _fake_gliner2(monkeypatch, _FakeModel)
    GlinerEntityExtractor()
    assert _FakeModel.loaded == [DEFAULT_MODEL]


class _Scored:
    provider_id = "scored"

    def __init__(self, label: str, confidence: float) -> None:
        self.label, self.confidence = label, confidence

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float]:
        return self.label, self.confidence


@pytest.mark.parametrize(
    ("confidence", "gate", "expected"),
    [(0.4, 0.6, "replay"), (0.9, 0.6, "compose"), (0.1, 0.0, "compose"), (0.6, 0.6, "compose")],
)
async def test_confidence_gate_keeps_the_default_mode(
    monkeypatch: pytest.MonkeyPatch, confidence: float, gate: float, expected: str
) -> None:
    """G2b: below ``read.planner_min_confidence`` the choice does not route."""
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "planner": "decision", "planner_min_confidence": gate},
        decision={"provider": "gliner2"},
    )
    await eng.start()
    try:
        monkeypatch.setattr(eng, "_decision_provider", lambda: _Scored("compose", confidence))
        assert await eng._plan_read_mode("where does Ana live") == expected
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
        assert out.mode == expected
    finally:
        await eng.stop()
