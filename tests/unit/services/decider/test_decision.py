"""H24: decision port, GLiNER2.5 adapter (fake SDK), and the read planner."""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from memspine import Engine
from memspine.config.schema.llm import GlinerSettings
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.services.decider import DecisionProvider
from memspine.services.decider.gliner2 import (
    DEFAULT_MODEL,
    GLiNER2Decision,
    auto_extractor_class,
    parse_choice,
)
from memspine.services.extraction.gliner2 import GLiNERModelPool

OPTS = {"a": "first", "b": "second"}


def test_parse_choice_shapes() -> None:
    assert parse_choice({"choice": {"label": "b", "confidence": 0.8}}, OPTS) == ("b", 0.8)
    assert parse_choice({"choice": "a"}, OPTS) == ("a", None)
    assert parse_choice({"choice": {"label": "a"}}, OPTS) == ("a", None)
    with pytest.raises(ValueError):
        parse_choice({"choice": "zzz"}, OPTS)


def test_gliner_settings_reject_invalid_or_coerced_values() -> None:
    with pytest.raises(ValueError, match="at least one task"):
        GlinerSettings(tasks=())
    with pytest.raises(ValueError):
        GlinerSettings(local_files_only="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GlinerSettings(inference_concurrency=True)  # type: ignore[arg-type]


def test_gliner2_satisfies_the_port() -> None:
    assert isinstance(GLiNER2Decision(GlinerSettings(), GLiNERModelPool()), DecisionProvider)


# ── fake gliner2 module ──────────────────────────────────────────────────────


class _Schema:
    def __init__(self) -> None:
        self.fields: dict[str, Any] = {}

    def classification(self, name: str, labels: Any) -> _Schema:
        self.fields[name] = labels
        return self


class _FakeModel:
    """Mimics the architecture-dispatched ``AutoExtractor`` API."""

    loaded: ClassVar[list[str]] = []

    def create_schema(self) -> _Schema:
        return _Schema()

    def extract(self, text: str, schema: _Schema, **kw: Any) -> dict[str, Any]:
        assert schema.fields["choice"] == OPTS
        return {"choice": {"label": "b", "confidence": 0.7}}

    def extract_entities(
        self,
        text: str,
        labels: list[str],
        *,
        include_confidence: bool = False,
        include_spans: bool = False,
    ) -> dict[str, Any]:
        start = text.find("Mira")
        found: list[Any] = []
        if start >= 0:
            item: dict[str, Any] = {"text": "Mira", "start": start, "end": start + 4}
            if include_confidence:
                item["confidence"] = 0.88
            found.append(item if include_spans else "Mira")
        return {"entities": {"person": found}}


class _ClassifyOnly:
    """A gliner2 build exposing only ``classify_text`` (and no confidence kwarg)."""

    def classify_text(self, text: str, tasks: Mapping[str, Any]) -> dict[str, Any]:
        return {"choice": "a"}


def _fake_gliner2(
    monkeypatch: pytest.MonkeyPatch, cls: type, *, legacy_class: type | None = None
) -> None:
    module = types.ModuleType("gliner2")
    module.AutoExtractor = cls
    if legacy_class is not None:
        module.GLiNER2 = legacy_class
    monkeypatch.setitem(sys.modules, "gliner2", module)


def _no_gliner2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gliner2", None)  # import raises ImportError


async def test_adapter_uses_autoextractor_and_25_default_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeModel.loaded = []

    class _LegacyMustNotLoad:
        @classmethod
        def from_pretrained(cls, model_id: str) -> Any:
            raise AssertionError("legacy GLiNER2 class must not be loaded")

    _fake_gliner2(monkeypatch, _FakeModel, legacy_class=_LegacyMustNotLoad)
    settings = GlinerSettings()
    pool = GLiNERModelPool(
        loader=lambda effective: (_FakeModel.loaded.append(effective.model), _FakeModel())[1]
    )
    provider = GLiNER2Decision(settings, pool)
    assert await provider.choose("q", OPTS) == ("b", 0.7)
    assert _FakeModel.loaded == [DEFAULT_MODEL] == ["fastino/gliner2.5-base-v1"]
    await pool.aclose()


async def test_adapter_falls_back_to_autoextractor_and_classify_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gliner2(monkeypatch, _ClassifyOnly)
    pool = GLiNERModelPool(loader=lambda _settings: _ClassifyOnly())
    assert await GLiNER2Decision(GlinerSettings(), pool).choose("q", OPTS) == ("a", None)
    await pool.aclose()


def test_autoextractor_missing_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_gliner2(monkeypatch)
    with pytest.raises(MissingServiceError) as info:
        auto_extractor_class()
    assert info.value.extra == "ner"


def test_gliner2_lazy_class_import_error_names_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gliner2>=2.0 base install: ``import gliner2`` works, but resolving ``AutoExtractor``
    imports the absent local stack (peft) and raises ImportError from ``__getattr__``."""
    module = types.ModuleType("gliner2")

    def _lazy(name: str) -> Any:
        if name == "AutoExtractor":
            raise ModuleNotFoundError("No module named 'peft'")
        raise AttributeError(name)

    module.__getattr__ = _lazy  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "gliner2", module)
    with pytest.raises(MissingServiceError) as info:
        auto_extractor_class()
    assert info.value.extra == "ner"


async def test_load_failure_reaches_the_caller() -> None:
    calls = {"n": 0}

    def _broken(_settings: GlinerSettings) -> Any:
        calls["n"] += 1
        raise OSError("no weights")

    pool = GLiNERModelPool(loader=_broken)
    provider = GLiNER2Decision(GlinerSettings(), pool)
    with pytest.raises(OSError):
        await provider.choose("q", OPTS)
    assert calls["n"] == 1
    await pool.aclose()


async def test_load_is_serialised_by_pool() -> None:
    calls = {"n": 0}

    def _load(_settings: GlinerSettings) -> _FakeModel:
        calls["n"] += 1
        return _FakeModel()

    pool = GLiNERModelPool(loader=_load)
    provider = GLiNER2Decision(GlinerSettings(), pool)
    await asyncio.gather(*(provider.choose("q", OPTS) for _ in range(4)))
    assert calls["n"] == 1
    await pool.aclose()


# ── engine start validation + planner ─────────────────────────────────────────


def _engine(**extra: Any) -> Engine:
    models = {
        "profiles": {"gliner-default": {"kind": "gliner", "provider": "gliner2", "settings": {}}},
        "bindings": {"gliner_decision": "gliner-default"},
    }
    overrides = extra.pop("models", {})
    for key in ("profiles", "bindings"):
        if key in overrides:
            models[key].update(overrides[key])
    models.update({key: value for key, value in overrides.items() if key not in models})
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "decider": {"route": {"provider": "gliner2"}},
        },
        read={"hybrid": False, "planner": "decision"},
        models=models,
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


async def test_core_start_does_not_import_or_load_gliner_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_gliner2(monkeypatch)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
    )
    await eng.start()
    try:
        assert eng._gliner_pool is None
    finally:
        await eng.stop()


async def test_enabled_consumer_requires_its_configured_gliner_task() -> None:
    eng = _engine()
    with pytest.raises(ConfigError, match="classification"):
        await eng.start()


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
        fake = _Fake("count or list")
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


async def test_ner_adapter_uses_shared_settings_and_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.derive.gliner_mentions import MentionSpan
    from memspine.memories.semantic.entities import GlinerEntityExtractor

    _FakeModel.loaded = []
    settings = GlinerSettings()
    pool = GLiNERModelPool(
        loader=lambda effective: (_FakeModel.loaded.append(effective.model), _FakeModel())[1]
    )
    extractor = GlinerEntityExtractor(pool, settings)
    assert await extractor.extract("Mira") == []
    assert await extractor.extract_mentions("Mira") == (
        MentionSpan(text="Mira", label="person", start=0, end=4, confidence=0.88),
    )
    assert extractor.model_id.startswith(f"gliner2:{DEFAULT_MODEL}@default:cpu:local=True:tasks=")
    assert _FakeModel.loaded == [DEFAULT_MODEL]
    await pool.aclose()


async def test_engine_consumers_share_one_lazy_model_instance() -> None:
    from memspine.derive.prepass import GlinerHints
    from memspine.memories.semantic.entities import GlinerEntityExtractor

    loads: list[str] = []

    def loader(settings: GlinerSettings) -> _FakeModel:
        loads.append(settings.model)
        return _FakeModel()

    settings = GlinerSettings()
    pool = GLiNERModelPool(loader=loader)
    decision = GLiNER2Decision(settings, pool)
    semantic = GlinerEntityExtractor(pool, settings)
    prepass = GlinerHints(pool, settings)
    assert loads == []  # construction and engine wiring do not load weights

    assert await decision.choose("q", OPTS) == ("b", 0.7)
    from memspine.derive.gliner_mentions import MentionSpan

    assert await semantic.extract("Mira") == []
    mentions = await semantic.extract_mentions("Mira")
    assert mentions == (MentionSpan(text="Mira", label="person", start=0, end=4, confidence=0.88),)
    assert await prepass.aentities("Mira") == mentions
    assert loads == [DEFAULT_MODEL]
    await pool.aclose()


async def test_engine_injects_one_owned_pool_into_enabled_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from memspine.derive.prepass import GlinerHints
    from memspine.ingest.windows import _prepass
    from memspine.memories.semantic.entities import GlinerEntityExtractor
    from memspine.services.extraction import gliner2 as extraction_gliner2

    _fake_gliner2(monkeypatch, _FakeModel)
    loads: list[str] = []
    pools: list[GLiNERModelPool] = []

    def make_pool(**kw: Any) -> GLiNERModelPool:
        pool = GLiNERModelPool(
            max_models=kw["max_models"],
            loader=lambda settings: (loads.append(settings.model), _FakeModel())[1],
        )
        pools.append(pool)
        return pool

    # RM-113: the extraction descriptor builds the pool from this module.
    monkeypatch.setattr(extraction_gliner2, "GLiNERModelPool", make_pool)
    eng = _engine(
        memories={"semantic": {"enabled": True, "policies": {"entity_extraction": "gliner"}}},
        write={
            "prepass": "gliner",
            "window_extract": "facts",
            "online_extractor_v2": True,
        },
        models={
            "bindings": {
                "gliner_semantic": "gliner-default",
                "gliner_prepass": "gliner-default",
            },
        },
        model={"placement": {"max_loaded_models": 2}},
    )
    await eng.start()
    try:
        pool = pools[-1]
        assert eng._gliner_pool is pool
        assert isinstance(eng._semantic._extractor, GlinerEntityExtractor)
        assert loads == []

        assert await eng._semantic._extractor.extract("Mira") == []
        assert await eng._semantic._extractor.extract_mentions("Mira")
        decision = eng._decision_provider()
        assert await decision.choose("q", OPTS) == ("b", 0.7)
        prepass = _prepass(eng, eng._config())
        assert isinstance(prepass._gliner, GlinerHints)
        hints = await prepass.ahints(
            [SimpleNamespace(record_id="source-1", content="Mira")],
            namespace="private-a",
        )
        assert any(span.text == "Mira" for span in hints.entities)
        assert loads == [DEFAULT_MODEL]
    finally:
        await eng.stop()


async def test_engine_profiles_select_distinct_models_and_share_equal_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from memspine.ingest.windows import _prepass
    from memspine.memories.semantic.entities import GlinerEntityExtractor
    from memspine.services.extraction import gliner2 as extraction_gliner2

    _fake_gliner2(monkeypatch, _FakeModel)
    loaded: list[str] = []
    pools: list[GLiNERModelPool] = []

    def make_pool(*, max_models: int, registry: Any = None) -> GLiNERModelPool:
        pool = GLiNERModelPool(
            max_models=max_models,
            loader=lambda settings: (loaded.append(settings.model), _FakeModel())[1],
            registry=registry,
        )
        pools.append(pool)
        return pool

    # RM-113: the extraction descriptor builds the pool from this module.
    monkeypatch.setattr(extraction_gliner2, "GLiNERModelPool", make_pool)
    shared_entities = {"model": "fake/entities", "tasks": ["entities"]}
    eng = _engine(
        memories={"semantic": {"enabled": True, "policies": {"entity_extraction": "gliner"}}},
        write={
            "prepass": "gliner",
            "window_extract": "facts",
            "online_extractor_v2": True,
        },
        model={
            "extraction": {
                "semantic": {"provider": "gliner2", "settings": shared_entities},
                "prepass": {"provider": "gliner2", "settings": shared_entities},
            },
            "placement": {"max_loaded_models": 2},
        },
    )
    await eng.start()
    try:
        assert pools[-1]._max_models == 2
        assert isinstance(eng._semantic._extractor, GlinerEntityExtractor)
        assert await eng._semantic._extractor.extract("Mira") == []
        assert await eng._semantic._extractor.extract_mentions("Mira")
        decision = eng._decision_provider()
        assert await decision.choose("q", OPTS) == ("b", 0.7)
        prepass = _prepass(eng, eng._config())
        hints = await prepass.ahints(
            [SimpleNamespace(record_id="source-1", content="Mira")], namespace="private-a"
        )
        assert any(span.text == "Mira" for span in hints.entities)
        assert loaded.count("fake/entities") == 1
        assert loaded.count("fake/classification") == 1
    finally:
        await eng.stop()
    assert eng._gliner_pool is None
    await eng.start()
    try:
        assert eng._gliner_pool is pools[-1] and eng._gliner_pool is not pools[0]
        assert eng._decision_provider() is not decision
    finally:
        await eng.stop()


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
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "decider": {"route": {"provider": "gliner2", "settings": {}}},
        },
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "planner": "decision", "planner_min_confidence": gate},
    )
    await eng.start()
    try:
        monkeypatch.setattr(eng, "_decision_provider", lambda: _Scored("count or list", confidence))
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


@pytest.mark.parametrize(("gate", "expected"), [(0.9, "replay"), (0.0, "compose")])
async def test_bare_label_confidence_is_unknown_not_sure(
    monkeypatch: pytest.MonkeyPatch, gate: float, expected: str
) -> None:
    """A-8: a bare-label result (no confidence) does not pass a positive gate."""
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "decider": {"route": {"provider": "gliner2", "settings": {}}},
        },
        read={"hybrid": False, "planner": "decision", "planner_min_confidence": gate},
    )
    await eng.start()
    try:

        class _BareModel(_ClassifyOnly):
            def classify_text(self, text: str, tasks: Mapping[str, Any]) -> dict[str, Any]:
                return {"choice": "count or list"}

        bare = GLiNER2Decision(
            GlinerSettings(), GLiNERModelPool(loader=lambda _settings: _BareModel())
        )
        _fake_gliner2(monkeypatch, _BareModel)
        monkeypatch.setattr(eng, "_decision_provider", lambda: bare)
        assert await eng._plan_read_mode("where does Ana live") == expected
    finally:
        await eng.stop()


# ── G24: rules first, then the provider's labelled options ────────────────────


class _Recording:
    provider_id = "recording"

    def __init__(self, label: str, confidence: float | None = 0.9) -> None:
        self.label, self.confidence = label, confidence
        self.options: list[dict[str, str]] = []

    async def choose(self, text: str, options: Mapping[str, str]) -> tuple[str, float | None]:
        self.options.append(dict(options))
        return self.label, self.confidence


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("How many times has Ana been to Lisbon?", "compose"),
        ("What books has Ana read?", "compose"),
        ("What was the first book Ana read?", "replay"),
    ],
)
async def test_rules_route_before_the_provider(
    monkeypatch: pytest.MonkeyPatch, query: str, expected: str
) -> None:
    """G24: questions the query_shape rules settle never reach the provider, and a rule
    choice is not subject to the confidence gate."""
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "decider": {"route": {"provider": "gliner2", "settings": {}}},
        },
        read={"hybrid": False, "planner": "decision", "planner_min_confidence": 0.99},
    )
    await eng.start()
    try:
        fake = _Recording("single fact", 0.1)
        monkeypatch.setattr(eng, "_decision_provider", lambda: fake)
        assert await eng._plan_read_mode(query) == expected
        assert fake.options == []
    finally:
        await eng.stop()


@pytest.mark.parametrize(
    ("label", "expected"),
    [("count or list", "compose"), ("reason or feeling", "replay"), ("single fact", "retrieve")],
)
async def test_provider_labels_map_to_read_modes(
    monkeypatch: pytest.MonkeyPatch, label: str, expected: str
) -> None:
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = _engine()
    await eng.start()
    try:
        fake = _Recording(label)
        monkeypatch.setattr(eng, "_decision_provider", lambda: fake)
        assert await eng._plan_read_mode("Why did Ana move to Lisbon?") == expected
        assert list(fake.options[0]) == ["count or list", "reason or feeling", "single fact"]
        assert all(desc for desc in fake.options[0].values())
    finally:
        await eng.stop()


async def test_unknown_provider_label_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gliner2(monkeypatch, _FakeModel)
    eng = _engine()
    await eng.start()
    try:
        monkeypatch.setattr(eng, "_decision_provider", lambda: _Recording("compose"))
        assert await eng._plan_read_mode("Where does Ana live?") is None
    finally:
        await eng.stop()
