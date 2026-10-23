"""RM-73 (ADR-079): model weights are shared per process through the resource registry.

Fake loaders only (no model download, no network). Each test simulates two Engines with two
``ProviderLifecycleManager`` instances: identical weight settings load once, different ones
load separately, stopping one Engine never frees weights the other holds, and the last
release frees them. Results (embedding caches) stay per Engine.
"""

from __future__ import annotations

import sys
import threading
import types
from typing import Any

import pytest

from memspine.clients.weights.shared import SharedWeights
from memspine.config.schema.providers import (
    OnnxEmbeddingSettings,
    SentenceTransformersEmbeddingSettings,
    StaticEmbeddingSettings,
)
from memspine.providers.builtin.models import register
from memspine.providers.contexts import (
    LLMBuildContext,
    RerankerBuildContext,
    SharedBuildContext,
)
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import EMBEDDERS, LLM_PROVIDERS
from memspine.providers.settings import LlamaCppRoleSettings
from memspine.providers.shared import ResourceRegistry, shared_registry
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedEmbedding


def _engine() -> ProviderLifecycleManager:
    return ProviderLifecycleManager(close_timeout_s=2.0)


def _shared(resources: ProviderLifecycleManager) -> SharedBuildContext:
    return SharedBuildContext(resources, "deployment-a", 1.0)


def _held(category: str) -> list[int]:
    return [s.refs for s in shared_registry().stats() if s.key.category == category]


# -- the primitive ---------------------------------------------------------------------------


async def test_identical_keys_load_once_and_last_release_closes() -> None:
    registry = ResourceRegistry()
    loads: list[str] = []
    closed: list[str] = []
    warmed: list[str] = []

    def holder(device: str) -> SharedWeights[str]:
        def load() -> str:
            loads.append(device)
            return f"weights@{device}"

        return SharedWeights(
            "model.test",
            {"model": "m"},
            {"device": device},
            load,
            close=closed.append,
            warm=warmed.append,
            registry=registry,
        )

    a, b, other = holder("cpu"), holder("cpu"), holder("cuda")
    assert await a.get() == await b.get() == "weights@cpu"
    assert await other.get() == "weights@cuda"
    assert loads == ["cpu", "cuda"]
    await a.warm()
    await b.warm()
    assert warmed == ["weights@cpu"]  # warm runs once per shared entry
    await a.aclose()
    assert closed == [] and await b.get() == "weights@cpu"
    await b.aclose()
    await other.aclose()
    assert sorted(closed) == ["weights@cpu", "weights@cuda"]
    assert registry.stats() == ()
    with pytest.raises(RuntimeError):
        await a.get()


async def test_failed_load_is_not_cached_and_no_registry_means_private() -> None:
    attempts = 0

    def load() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient")
        return "w"

    private = SharedWeights("model.test", {"model": "m"}, None, load)
    with pytest.raises(OSError):
        await private.get()
    assert await private.get() == "w"
    other = SharedWeights("model.test", {"model": "m"}, None, load)
    assert await other.get() == "w"
    assert attempts == 3  # separate private registries never share
    await private.aclose()
    await other.aclose()


# -- embedders -------------------------------------------------------------------------------


class _FakeTextEmbedding:
    loads: list[str] = []

    def __init__(self, spec: Any, **kwargs: Any) -> None:
        from memspine.clients.weights.onnx_catalogue import CATALOGUE

        self.model_name = next(name for name, candidate in CATALOGUE.items() if candidate == spec)
        _FakeTextEmbedding.loads.append(self.model_name)

    def embed(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        dim = 768 if "base" in self.model_name else 384
        return [[float(len(text)), *([1.0] * (dim - 1))] for text in texts]


@pytest.fixture
def fake_onnx(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from memspine.clients.weights import onnx

    _FakeTextEmbedding.loads = []
    monkeypatch.setattr(onnx, "OnnxModel", _FakeTextEmbedding)
    return _FakeTextEmbedding.loads


async def test_onnx_weights_shared_across_engines_caches_stay_per_engine(
    fake_onnx: list[str],
) -> None:
    register()
    builder = EMBEDDERS.get("onnx").builder
    engine_a, engine_b = _engine(), _engine()
    built_a = await builder.build(OnnxEmbeddingSettings(), _shared(engine_a))
    built_b = await builder.build(OnnxEmbeddingSettings(query_instruction="q: "), _shared(engine_b))
    assert engine_a.states and engine_b.states  # each Engine owns its own lease release
    kv_a, kv_b = MemoryKV(), MemoryKV()
    cached_a = CachedEmbedding(built_a.service, kv_a)
    cached_b = CachedEmbedding(built_b.service, kv_b)

    await cached_a.embed(["hello"])
    await cached_b.embed(["hello"])
    assert fake_onnx == ["BAAI/bge-small-en-v1.5"]  # one load for both Engines
    # Results are never shared: Engine B missed even though A embedded the same text.
    assert (cached_a.misses, cached_b.misses, cached_b.hits) == (1, 1, 0)
    assert kv_a._data is not kv_b._data
    assert set(kv_a._data).isdisjoint(kv_b._data)  # instruction-specific space identities

    await cached_a.warm()  # forwards to the shared weights, never fills the cache
    assert len(kv_a._data) == 1
    assert (await engine_a.close_all()).complete
    assert _held("model.embedding.onnx") == [1]  # Engine B still holds the weights
    assert len(await cached_b.embed(["still served"])) == 1
    assert (await engine_b.close_all()).complete
    assert _held("model.embedding.onnx") == []


async def test_different_embedder_settings_load_separately(fake_onnx: list[str]) -> None:
    register()
    builder = EMBEDDERS.get("onnx").builder
    engine = _engine()
    small = await builder.build(OnnxEmbeddingSettings(), _shared(engine))
    other = _engine()
    base = await builder.build(OnnxEmbeddingSettings(model="BAAI/bge-base-en-v1.5"), _shared(other))
    await small.service.embed(["x"])
    await base.service.embed(["x"])
    assert fake_onnx == ["BAAI/bge-small-en-v1.5", "BAAI/bge-base-en-v1.5"]
    await engine.close_all()
    await other.close_all()
    assert _held("model.embedding.onnx") == []


async def test_st_weight_key_excludes_prompts_and_includes_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, Any]] = []

    class FakeST:
        max_seq_length = 512

        def __init__(self, name: str, device: Any = None, **_: Any) -> None:
            loads.append((name, device))

        def encode(self, texts: list[str], **_: Any) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeST  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    register()
    builder = EMBEDDERS.get("st").builder
    engines = [_engine() for _ in range(3)]
    settings = [
        SentenceTransformersEmbeddingSettings(model="m", dim=2, query_instruction="a"),
        SentenceTransformersEmbeddingSettings(model="m", dim=2, query_instruction="b"),
        SentenceTransformersEmbeddingSettings(model="m", dim=2, device="cuda"),
    ]
    built = [await builder.build(s, _shared(e)) for s, e in zip(settings, engines, strict=True)]
    for result in built:
        await result.service.embed(["x"])
    assert loads == [("m", None), ("m", "cuda")]
    for engine in engines:
        await engine.close_all()
    assert _held("model.embedding.st") == []


# -- static prefilter ------------------------------------------------------------------------


async def test_static_prefilter_reuses_the_configured_static_embedder_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.read.pipeline.rerank import _static_embedding_prefilter
    from memspine.services.embedding.static import DEFAULT_MODEL as _DEFAULT_MODEL
    from memspine.store.records import MemoryRecord

    loads: list[str] = []

    class StaticModel:
        @staticmethod
        def from_pretrained(name: str) -> StaticModel:
            loads.append(name)
            return StaticModel()

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(t)), *([1.0] * 255)] for t in texts]

    module = types.ModuleType("model2vec")
    module.StaticModel = StaticModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "model2vec", module)
    register()
    resources = _engine()
    configured = await EMBEDDERS.get("static").builder.build(
        StaticEmbeddingSettings(model=_DEFAULT_MODEL, dim=256), _shared(resources)
    )
    await configured.service.embed(["configured"])

    eng = types.SimpleNamespace(
        _static_embedder=None, _static_unavailable=False, _provider_resources=resources
    )
    candidates = [
        (MemoryRecord(namespace="ns", memory_type="semantic", content=f"memory {i}"), 1.0)
        for i in range(40)
    ]
    kept = await _static_embedding_prefilter(eng, "memory", candidates, top_k=2)
    assert len(kept) < len(candidates)
    assert loads == [_DEFAULT_MODEL]  # the prefilter joined the configured embedder's load
    assert _held("model.embedding.static") == [2]
    assert "embedding.weights:static-prefilter" in resources.states
    assert (await resources.close_all()).complete
    assert _held("model.embedding.static") == []


# -- llama.cpp -------------------------------------------------------------------------------


class _FakeLlama:
    built: list[dict[str, Any]] = []
    closed: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        self.path = str(kwargs["model_path"])
        _FakeLlama.built.append(kwargs)

    def create_chat_completion(self, **kwargs: Any) -> dict[str, object]:
        return {"choices": [{"message": {"content": f"t={kwargs['temperature']}"}}]}

    def close(self) -> None:
        _FakeLlama.closed.append(self.path)


@pytest.fixture
def fake_llama(monkeypatch: pytest.MonkeyPatch) -> type[_FakeLlama]:
    _FakeLlama.built, _FakeLlama.closed = [], []
    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=_FakeLlama))
    return _FakeLlama


async def test_llamacpp_same_gguf_two_roles_share_one_load(fake_llama: type[_FakeLlama]) -> None:
    register()
    builder = LLM_PROVIDERS.get("llamacpp").builder
    engine = _engine()
    chat = await builder.build(
        LlamaCppRoleSettings(model="llamacpp/shared.gguf", temperature=0.1, timeout_seconds=2.0),
        LLMBuildContext(_shared(engine), "chat"),
    )
    extract = await builder.build(
        LlamaCppRoleSettings(model="llamacpp/shared.gguf", temperature=0.7, timeout_seconds=2.0),
        LLMBuildContext(_shared(engine), "extract", resource_key="llm.profile:other-name"),
    )
    assert len(fake_llama.built) == 1  # role and profile names are not part of the key
    # Generation defaults stay per holder.
    assert await chat.service.chat([{"role": "user", "content": "q"}]) == "t=0.1"
    assert await extract.service.chat([{"role": "user", "content": "q"}]) == "t=0.7"

    await chat.service.close()  # one holder released: the other keeps the native model
    assert fake_llama.closed == []
    assert await extract.service.chat([{"role": "user", "content": "q"}]) == "t=0.7"
    assert (await engine.close_all()).complete
    assert fake_llama.closed == ["shared.gguf"]
    assert _held("model.llm.llamacpp") == []


async def test_llamacpp_native_options_separate_loads(fake_llama: type[_FakeLlama]) -> None:
    register()
    builder = LLM_PROVIDERS.get("llamacpp").builder
    engine_a, engine_b = _engine(), _engine()
    await builder.build(
        LlamaCppRoleSettings(model="llamacpp/m.gguf", timeout_seconds=2.0),
        LLMBuildContext(_shared(engine_a), "chat"),
    )
    await builder.build(
        LlamaCppRoleSettings(model="llamacpp/m.gguf", n_ctx=4096, timeout_seconds=2.0),
        LLMBuildContext(_shared(engine_b), "chat"),
    )
    assert [b["n_ctx"] for b in fake_llama.built] == [8192, 4096]
    await engine_a.close_all()
    await engine_b.close_all()
    assert fake_llama.closed == ["m.gguf", "m.gguf"]


# -- rerankers -------------------------------------------------------------------------------


async def test_onnx_reranker_shared_across_engines_with_warm_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.clients.weights import onnx as cross_encoder
    from memspine.providers.rerank import RerankSettings, build_reranker_result

    loads: list[str] = []
    scored: list[str] = []
    lock = threading.Lock()

    class FakeCrossEncoder:
        def __init__(self, spec: Any, **kwargs: Any) -> None:
            loads.append(spec.repo)

        def rerank(self, query: str, documents: list[str], **kwargs: Any) -> list[float]:
            with lock:
                scored.append(query)
            return [float(len(d)) for d in documents]

    monkeypatch.setattr(cross_encoder, "OnnxModel", FakeCrossEncoder)
    engine_a, engine_b = _engine(), _engine()
    built = [
        await build_reranker_result(
            RerankSettings(mode="onnx"), RerankerBuildContext(_shared(engine))
        )
        for engine in (engine_a, engine_b)
    ]
    assert all(result is not None and result.resources for result in built)
    services = [result.service for result in built if result is not None]
    await services[0].warm()
    await services[1].warm()
    assert scored == ["warm-up"]  # warm-up runs once per shared entry
    assert await services[1].rerank("q", ["ab", "a"]) == [2.0, 1.0]
    assert len(loads) == 1
    await engine_a.close_all()
    assert await services[1].rerank("q", ["abc"]) == [3.0]
    await engine_b.close_all()
    assert _held("model.reranker.onnx") == []


async def test_standalone_reranker_build_stays_private(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.clients.weights import onnx as cross_encoder
    from memspine.providers.rerank import RerankSettings, build_reranker

    monkeypatch.setattr(
        cross_encoder,
        "OnnxModel",
        lambda spec, **kwargs: types.SimpleNamespace(rerank=lambda q, d, **kw: [1.0] * len(d)),
    )
    reranker = await build_reranker(RerankSettings(mode="onnx"))
    assert reranker is not None and await reranker.rerank("q", ["x"]) == [1.0]
    assert _held("model.reranker.onnx") == []


# -- OpenDecider -----------------------------------------------------------------------------


async def test_opendecider_weights_shared_and_freed_on_last_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.services.decider import decider as decider_mod

    loads: list[tuple[Any, ...]] = []

    class FakeNano:
        def yes_probability(self, states: list[str], instructions: str) -> list[float]:
            return [0.9 for _ in states]

    def load(name: str, device: str, dtype: str, **kw: Any) -> FakeNano:
        loads.append((name, device, dtype, kw["backend"], kw["threads"]))
        return FakeNano()

    monkeypatch.setattr(decider_mod, "load_nano", load)
    registry = shared_registry()
    first = decider_mod.OpenDeciderDecider("m", "cpu", threads=2, registry=registry)
    second = decider_mod.OpenDeciderDecider("m", "cpu", threads=2, workers=2, registry=registry)
    other = decider_mod.OpenDeciderDecider(
        "m", "cpu", threads=2, dtype="bfloat16", registry=registry
    )
    for decider in (first, second, other):
        assert (await decider.decide("relevance", "q", "c")).label
    assert loads == [("m", "cpu", "float32", "torch", 2), ("m", "cpu", "bfloat16", "torch", 2)]
    await first.aclose()
    assert (await second.decide("relevance", "q", "c")).label  # still loaded
    await second.aclose()
    await other.aclose()
    assert _held("model.decider.opendecider") == []
    with pytest.raises(RuntimeError):
        await first.decide("relevance", "q", "c")
