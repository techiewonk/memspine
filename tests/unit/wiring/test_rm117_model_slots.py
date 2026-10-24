"""RM-117 WP2 (ADR-082): the engine wiring reads the ``model`` block slots.

* one GLiNER pool keyed without per-consumer fields: six consumers of one checkpoint = one load;
* rerank is one ``model.rerank.chain`` path; a chain of one orders like the old single reranker;
* ``model.decider.route`` / ``model.decider.read`` select the H24 / I28 deciders;
* ``model.vision: null`` is the no-description vision port;
* escalation targets are inline LLM slots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.config.schema.model import gliner_settings, weights_settings
from memspine.exceptions import ConfigError
from memspine.providers.contracts import BuildResult
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.read.pipeline import rerank as rr
from memspine.read.pipeline.fusion import _minmax_normalize
from memspine.runtime import container
from memspine.services.extraction.gliner2 import GLiNERModelPool
from memspine.services.llm.escalation import EscalatingLLM
from memspine.store.records import MemoryRecord

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
CHECKPOINT = "fastino/gliner2.5-base-v1"
RUNTIME = {
    "provider_services": {
        "health_timeout_s": 2.0,
        "close_timeout_s": 2.0,
        "startup_timeout_s": None,
    }
}


def _engine(**kwargs: Any) -> Engine:
    kwargs.setdefault(
        "storage",
        {
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
    )
    return Engine(**kwargs)


def _cfg(**sections: Any) -> MemspineConfig:
    return MemspineConfig.model_validate({"runtime": RUNTIME, **sections})


def _gliner(tasks: list[str], **extra: Any) -> dict[str, Any]:
    return {"provider": "gliner2", "settings": {"model": CHECKPOINT, "tasks": tasks}, **extra}


def _six_consumer_config() -> MemspineConfig:
    return _cfg(
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True, "policies": {"entity_extraction": "gliner"}},
        },
        write={"prepass": "gliner", "window_extract": "facts"},
        model={
            "extraction": {
                "semantic": _gliner(["entities"]),
                "prepass": _gliner(["entities", "relations"]),
                "topic": _gliner(["classification"], labels={"cardiology": "health/condition"}),
                "sensitivity": {
                    "provider": "gliner2",
                    "settings": {
                        "model": CHECKPOINT,
                        "tasks": ["entities"],
                        "inference_concurrency": 2,
                    },
                },
            },
            "decider": {
                "route": _gliner(["classification", "entities"]),
                "read": _gliner(["classification"]),
            },
        },
    )


# ---- one GLiNER load for every consumer (ADR-082 item 4) -----------------------------------


def test_consumers_of_one_checkpoint_have_one_weights_key() -> None:
    cfg = _six_consumer_config()
    slots = [
        cfg.model.extraction.semantic,
        cfg.model.extraction.prepass,
        cfg.model.extraction.topic,
        cfg.model.extraction.sensitivity,
        cfg.model.decider.route,
        cfg.model.decider.read,
    ]
    weights = [weights_settings(s) for s in slots if s is not None]
    assert len(weights) == 6
    assert all(w == weights[0] for w in weights)  # tasks / inference_concurrency differ
    assert "tasks" not in weights[0] and "inference_concurrency" not in weights[0]


async def test_six_gliner_consumers_share_one_load() -> None:
    cfg = _six_consumer_config()
    loads: list[Any] = []

    def loader(settings: Any) -> object:
        loads.append(settings)
        return object()

    pool = GLiNERModelPool(max_models=cfg.model.placement.max_loaded_models, loader=loader)
    leases = [
        await pool.acquire(gliner_settings(cfg, consumer))
        for consumer in ("semantic", "prepass", "topic", "sensitivity", "route", "read")
    ]
    try:
        assert len(loads) == 1  # six consumers, one checkpoint, one load
    finally:
        for lease in leases:
            await lease.aclose()
        await pool.aclose()


def test_active_gliner_consumers_follow_switches_and_slots() -> None:
    cfg = _six_consumer_config()
    assert container.gliner_consumers(cfg) == (
        "semantic",
        "prepass",
        "topic",
        "sensitivity",
        "route",
    )
    # semantic / prepass stay switched by their policy flags even with a slot.
    off = _cfg(
        model={
            "extraction": {"semantic": _gliner(["entities"]), "topic": _gliner(["classification"])}
        }
    )
    assert container.gliner_consumers(off) == ("topic",)
    assert container.gliner_consumers(_cfg()) == ()


# ---- rerank: one chain path ----------------------------------------------------------------


class _FakeReranker:
    reranker_id = "fake"

    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores
        self.calls = 0

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls += 1
        return [next((v for k, v in self.scores.items() if k in d), 0.0) for d in documents]


async def test_a_chain_of_one_orders_like_the_old_single_reranker() -> None:
    cfg = _cfg(model={"rerank": {"chain": [{"provider": "onnx"}]}})
    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    cands = [
        (MemoryRecord(namespace="a", memory_type="episodic", content=t), 1.0 - i / 10)
        for i, t in enumerate(texts)
    ]
    table = {"gamma": 3.0, "alpha": 2.5, "epsilon": 1.0, "beta": 0.5, "delta": 0.1}
    fake = _FakeReranker(table)

    async def first() -> Any:
        return fake

    eng = SimpleNamespace(
        _config=lambda: cfg,
        _rerank_provider=first,
        _rerank_calls=0,
        _rerank_failures=0,
        _rerank_gated=0,
    )
    out, reranked = await rr.rerank_stage(eng, "a", "q", cands, cfg.model.rerank)
    # The old v1 path: every candidate takes the min-max normalised reranker score.
    raw = [table[t] for t in texts]
    old = [(rec, rel) for (rec, _), rel in zip(cands, _minmax_normalize(raw), strict=True)]
    assert reranked and out == old
    assert [r.content for r, _ in sorted(out, key=lambda p: p[1], reverse=True)] == [
        "gamma",
        "alpha",
        "epsilon",
        "beta",
        "delta",
    ]


async def test_the_chain_falls_back_to_the_next_entry() -> None:
    cfg = _cfg(model={"rerank": {"chain": [{"provider": "jina"}, {"provider": "onnx"}]}})
    second = _FakeReranker({"b": 2.0})
    built: list[str] = []

    async def first() -> None:
        return None  # the first entry could not be built

    async def build(settings: Any) -> Any:
        built.append(settings.mode)
        return second

    eng = SimpleNamespace(
        _config=lambda: cfg,
        _rerank_provider=first,
        _build_reranker=build,
        _rerank_calls=0,
        _rerank_failures=0,
    )
    scores, model, fallback, tried = await rr.chain_score(eng, "q", ["a", "b"], cfg.model.rerank)
    assert built == ["onnx"] and scores == [0.0, 2.0]
    assert (model, fallback) == ("fake", "onnx")
    assert tried == [{"mode": "jina", "outcome": "unavailable"}]


def test_stage_settings_carry_the_entry_settings_and_chunking() -> None:
    cfg = _cfg(
        model={
            "rerank": {
                "chain": [{"provider": "qwen3", "settings": {"device": "cuda", "batch_size": 8}}],
                "chunk_chars": 128,
                "chunk_overlap": 16,
            }
        }
    )
    stage = cfg.model.rerank.chain[0]
    settings = rr.stage_settings(stage, cfg.model.rerank)
    assert (settings.mode, settings.device, settings.batch_size) == ("qwen3", "cuda", 8)
    assert (settings.chunk_chars, settings.chunk_overlap) == (128, 16)
    assert rr.stage_settings(stage, cfg.model.rerank, "cuda:1").device == "cuda:1"


async def _search(model: dict[str, Any], fake: _FakeReranker, top_k: int = 3) -> list[str]:
    eng = _engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}, **model},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False},
    )
    await eng.start()
    eng._reranker = fake
    try:
        for i, text in enumerate(("camping by the lake", "the lake trip", "camping gear list")):
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                session_id="s1",
                valid_from=T0 + timedelta(minutes=i),
            )
        hits = await eng.search("camping", namespace="a", top_k=top_k)
        return [r.content for r, _ in hits]
    finally:
        await eng.stop()


async def test_engine_search_reranks_through_the_chain() -> None:
    fake = _FakeReranker({"the lake trip": 9.0})
    hits = await _search({"rerank": {"chain": [{"provider": "onnx"}]}}, fake)
    assert hits[0] == "the lake trip" and fake.calls == 1


async def test_an_empty_chain_is_off() -> None:
    fake = _FakeReranker({"the lake trip": 9.0})
    await _search({}, fake)
    assert fake.calls == 0


async def test_max_top_k_is_honoured() -> None:
    fake = _FakeReranker({"the lake trip": 9.0})
    await _search({"rerank": {"chain": [{"provider": "onnx"}], "max_top_k": 2}}, fake)
    assert fake.calls == 0  # H18: the caller keeps 3 > 2


async def test_an_unknown_chain_provider_fails_at_start() -> None:
    eng = _engine(
        template="core",
        dotenv_path=None,
        model={"embedding": {"provider": "hash"}, "rerank": {"chain": [{"provider": "nope"}]}},
    )
    with pytest.raises(ConfigError, match=r"model\.rerank\.chain\[0\]"):
        await eng.start()


# ---- deciders ------------------------------------------------------------------------------


def test_read_decider_absent_or_heuristic_is_the_rules() -> None:
    from memspine.providers.decider import build_read_decider, read_decider_slot
    from memspine.services.decider.decider import HeuristicDecider

    assert read_decider_slot(_cfg()) is None
    heuristic = _cfg(model={"decider": {"read": {"provider": "heuristic"}}})
    assert read_decider_slot(heuristic) is None
    assert isinstance(build_read_decider(None), HeuristicDecider)


def test_read_decider_opendecider_takes_settings_and_workers() -> None:
    from memspine.providers.decider import build_read_decider, read_decider_slot

    cfg = _cfg(
        model={
            "decider": {
                "read": {
                    "provider": "opendecider",
                    "tasks": ["list_mode"],
                    "min_confidence": 0.7,
                    "workers": 2,
                    "settings": {"device": "cpu", "backend": "onnx", "threads": 3},
                }
            }
        }
    )
    slot = read_decider_slot(cfg)
    assert slot is not None and slot.tasks == ["list_mode"]
    decider: Any = build_read_decider(slot)
    assert (decider.backend, decider.workers, decider.threads) == ("onnx", 2, 3)
    assert decider.model == "manjunathshiva/opendecider-nano"


async def test_decider_active_follows_the_read_slot() -> None:
    eng = _engine(
        template="core",
        dotenv_path=None,
        model={
            "embedding": {"provider": "hash"},
            "decider": {"read": {"provider": "opendecider", "tasks": ["list_mode"]}},
        },
    )
    await eng.start()
    try:
        assert eng._decider_active("list_mode")
        assert not eng._decider_active("bridge_hop")
    finally:
        await eng.stop()
    plain = _engine(template="core", dotenv_path=None, model={"embedding": {"provider": "hash"}})
    await plain.start()
    try:
        assert not plain._decider_active("list_mode")
        assert plain._decision_provider() is None  # no model.decider.route
    finally:
        await plain.stop()


def test_route_and_read_providers_must_be_implemented() -> None:
    route = _cfg(model={"decider": {"route": {"provider": "heuristic"}}})
    with pytest.raises(ConfigError, match=r"model\.decider\.route"):
        container.check_decision_provider(route)
    read = _cfg(model={"decider": {"read": _gliner(["classification"])}})
    with pytest.raises(ConfigError, match=r"model\.decider\.read"):
        container.check_decision_provider(read)
    assert container.check_decision_provider(_cfg()).off is False


# ---- vision --------------------------------------------------------------------------------


def _asset_engine(cfg: MemspineConfig) -> Any:
    return SimpleNamespace(
        _assets=None,
        _asset_rows=None,
        _sql=None,
        _documents=None,
        _config=lambda: cfg,
        _http_connector=lambda: object(),
    )


def test_vision_null_is_noop() -> None:
    from memspine.ingest.assets import _asset_adapter
    from memspine.services.vision import NoopVision

    adapter = _asset_adapter(_asset_engine(_cfg()))
    assert isinstance(adapter.vision, NoopVision)


def test_vision_slot_builds_ollama_from_its_settings() -> None:
    from memspine.ingest.assets import _asset_adapter
    from memspine.services.vision import OllamaVision

    cfg = _cfg(
        model={
            "vision": {
                "provider": "ollama",
                "timeout_s": 30,
                "settings": {"model": "llava:7b", "api_base": "http://gpu:11434"},
            }
        }
    )
    vision = _asset_adapter(_asset_engine(cfg)).vision
    assert isinstance(vision, OllamaVision)
    assert vars(vision).get("model", vars(vision).get("_model")) == "llava:7b"


# ---- LLM slots: inline escalation targets, typed timeout, per-role structured ---------------


class _Provider:
    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def provider_id(self) -> str:
        return self.model

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append(options)
        return "" if self.model == "ollama/small" else "ok"


class _Hooks:
    async def withheld_texts(self) -> list[str]:
        return []


async def test_escalation_targets_are_inline_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    made: dict[str, _Provider] = {}
    timeouts: dict[str, float] = {}
    real_build = container._build_provider

    async def build(
        wiring: Any, descriptor: Any, settings: Any, context: Any, *, result_key: str | None = None
    ) -> BuildResult[Any]:
        if descriptor.ref.category != "llm":
            return await real_build(wiring, descriptor, settings, context, result_key=result_key)
        provider = made.setdefault(settings.model, _Provider(settings.model))
        timeouts[settings.model] = settings.timeout_seconds
        result = BuildResult(provider, descriptor.ref, (), descriptor.declared_capabilities)
        wiring.provider_results[result_key or descriptor.ref.name] = result
        return result

    monkeypatch.setattr(container, "_build_provider", build)
    small = {
        "provider": "openai_compatible",
        "timeout_s": 20,
        "settings": {"api_base": "http://test.invalid/v1", "model": "ollama/small"},
    }
    config = _cfg(
        model={
            "llm": {
                "chat": {
                    **small,
                    "structured": {"retry_on_error": True},
                    "escalation": {
                        "targets": [
                            {
                                "provider": "openai_compatible",
                                "timeout_s": 90,
                                "settings": {
                                    "api_base": "http://test.invalid/v1",
                                    "model": "ollama/big",
                                },
                            }
                        ],
                        "triggers": ["empty"],
                        "max_attempts": 2,
                        "timeout_s": 5,
                        "max_tokens_per_attempt": 64,
                    },
                },
                "extract": small,  # identical slot: shares the built service
            }
        },
    )
    wiring = container.Wiring(
        resources=ProviderLifecycleManager(close_timeout_s=2),
        health_timeout_s=2,
        startup_timeout_s=None,
    )
    router = await container.build_llm_router(config, _Hooks(), wiring)  # type: ignore[arg-type]
    assert isinstance(router.provider("chat"), EscalatingLLM)
    assert set(made) == {"ollama/small", "ollama/big"}
    assert timeouts == {"ollama/small": 20, "ollama/big": 90}  # timeout_s -> timeout_seconds
    assert "llm.extract" in wiring.provider_results and "llm.chat" in wiring.provider_results
    reply = await router.for_role("chat").chat([{"role": "user", "content": "hi"}])
    assert reply == "ok"  # escalated on the empty primary reply
    assert router.for_role("chat").structured_options.retry_on_error is True  # type: ignore[attr-defined]
    assert router.for_role("extract").structured_options.retry_on_error is False  # type: ignore[attr-defined]


def test_tiered_role_config_is_an_llm_slot() -> None:
    from memspine.config.schema.model import LLMSlot, PlacementConfig
    from memspine.models.resolve import resolve_models, role_config

    placement = PlacementConfig.model_validate(
        {
            "tiers": True,
            "catalogue": [
                {
                    "id": "ollama/qwen3.5:9b",
                    "provider": "openai_compatible",
                    "tier": "worker",
                    "timeout_seconds": 45,
                    "vram_mb": 7000,
                }
            ],
            "gpus": [{"name": "NVIDIA RTX", "vram_mb": 16000}],
        }
    )
    slot = role_config(resolve_models(placement), "judge")
    assert isinstance(slot, LLMSlot) and slot.timeout_s == 45
    assert "timeout_seconds" not in slot.settings
