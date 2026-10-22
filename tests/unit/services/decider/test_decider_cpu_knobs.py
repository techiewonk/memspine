"""I28 / I38: the decider CPU knobs (``decider_model``, ``decider_threads``, ``decider_backend``,
``decider_dtype``, ``decider_workers``) with fakes, plus an optional real parity check that is
skipped when the model is not downloaded. No network, no GPU."""

from __future__ import annotations

import asyncio
import importlib
import sys
import threading
import time
import types
from typing import Any

import pytest

from memspine import Engine
from memspine.clients.weights import opendecider as nano
from memspine.config.schema import ReadConfig
from memspine.exceptions import MissingServiceError
from memspine.observability.trace import search_forensics
from memspine.providers.decider import build_decider
from memspine.services.decider import decider as decider_mod
from memspine.services.decider.decider import TASKS, OpenDeciderDecider

# -- config plumbing -------------------------------------------------------------------------


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


def test_knob_defaults_are_the_evaluated_configuration() -> None:
    cfg = ReadConfig()
    assert (cfg.decider_threads, cfg.decider_backend, cfg.decider_workers) == (0, "torch", 1)
    assert (cfg.decider_dtype, cfg.decider_model) == ("float32", decider_mod.DEFAULT_MODEL)


def test_knob_values_are_validated() -> None:
    for bad in (
        {"decider_backend": "tensorrt"},
        {"decider_dtype": "float16"},
        {"decider_workers": 0},
        {"decider_threads": -1},
    ):
        with pytest.raises(ValueError):
            ReadConfig(**bad)


async def test_engine_passes_every_knob_to_build_decider(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    class Stub:
        decider_id = "stub"

    from memspine.providers.shared import shared_registry

    def build(kind: str, model: str, device: str, **kw: Any) -> Stub:
        # RM-73: an Engine build shares the decider weights through the process registry.
        assert kw.pop("registry") is shared_registry()
        seen.append((kind, model, device, kw))
        return Stub()

    import memspine.api.engine as engine_mod

    monkeypatch.setattr(engine_mod, "build_decider", build)
    eng = _engine(
        decider="opendecider",
        decider_model="my/model",
        decider_threads=3,
        decider_backend="onnx",
        decider_workers=2,
        decider_dtype="bfloat16",
    )
    await eng.start()
    a = eng._decider_adapter()
    assert seen == [
        (
            "opendecider",
            "my/model",
            "cpu",
            {"threads": 3, "backend": "onnx", "workers": 2, "dtype": "bfloat16"},
        )
    ]
    assert eng._decider_adapter() is a  # cached while the knobs are unchanged
    await eng.stop()

    # An immutable Engine config is selected at startup; a second instance exercises
    # the alternate setting without mutating the first engine's snapshot.
    changed = _engine(
        decider="opendecider",
        decider_model="my/model",
        decider_threads=3,
        decider_backend="onnx",
        decider_workers=2,
        decider_dtype="float32",
    )
    await changed.start()
    try:
        b = changed._decider_adapter()
        assert b is not a and len(seen) == 2
        assert seen[1][3]["dtype"] == "float32"
        assert changed._decider_adapter() is b
    finally:
        await changed.stop()


def test_build_decider_forwards_the_knobs() -> None:
    d = build_decider(
        "opendecider", "m", "cpu", threads=2, backend="onnx", workers=2, dtype="bfloat16"
    )
    assert isinstance(d, OpenDeciderDecider)
    assert (d.model, d.threads, d.backend, d.workers, d.dtype) == ("m", 2, "onnx", 2, "bfloat16")


# -- thread-count capping ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cores", "workers", "threads", "expected"),
    [
        (16, 1, 0, 16),  # default: every physical core
        (16, 4, 0, 4),  # the cores are split between the workers
        (16, 3, 0, 5),  # integer split, never oversubscribed
        (2, 8, 0, 1),  # never below one thread
        (16, 4, 6, 6),  # an explicit value is honoured
    ],
)
def test_threads_default_to_cores_split_by_workers(
    monkeypatch: pytest.MonkeyPatch, cores: int, workers: int, threads: int, expected: int
) -> None:
    monkeypatch.setattr(decider_mod, "physical_cores", lambda: cores)
    d = OpenDeciderDecider("m", "cpu", threads=threads, workers=workers)
    assert d.threads == expected
    assert d.threads * d.workers <= max(cores, d.workers) or threads > 0


def test_physical_cores_falls_back_to_half_the_logical_cpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)  # import raises ImportError
    monkeypatch.setattr(nano.os, "cpu_count", lambda: 12)
    assert nano.physical_cores() == 6
    monkeypatch.setattr(nano.os, "cpu_count", lambda: 1)
    assert nano.physical_cores() == 1


# -- backend selection, fallback, dtype --------------------------------------------------------


@pytest.fixture
def fake_stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[Any, ...]]]:
    """Fake torch / transformers / safetensors / onnxruntime / huggingface_hub and fake model
    classes: ``load_nano`` runs its real selection logic, nothing heavy is imported."""
    built: dict[str, list[tuple[Any, ...]]] = {"torch": [], "onnx": [], "hub": []}

    class FakeTorch(types.ModuleType):
        class cuda:
            @staticmethod
            def is_available() -> bool:
                return False

    for mod in ("torch", "transformers", "safetensors", "onnxruntime"):
        monkeypatch.setitem(
            sys.modules, mod, FakeTorch(mod) if mod == "torch" else types.ModuleType(mod)
        )
    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_download = lambda repo, file: built["hub"].append((repo, file)) or f"/x/{file}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    class FakeTorchModel:
        def __init__(self, path: Any, device: str, dtype: str, threads: Any) -> None:
            built["torch"].append((str(path), device, dtype, threads))

    class FakeOnnxModel:
        def __init__(self, path: Any, onnx_file: Any, threads: Any) -> None:
            built["onnx"].append((str(path), str(onnx_file), threads))

    monkeypatch.setattr(nano, "NanoModel", FakeTorchModel)
    monkeypatch.setattr(nano, "NanoOnnx", FakeOnnxModel)
    monkeypatch.setattr(nano, "_snapshot", lambda name: f"/snap/{name}")
    return built


def test_torch_backend_receives_dtype_threads_and_device(fake_stack: dict[str, list]) -> None:
    nano.load_nano("m", "cpu", "bfloat16", backend="torch", threads=4)
    assert fake_stack["torch"] == [("/snap/m", "cpu", "bfloat16", 4)] and not fake_stack["onnx"]


def test_onnx_backend_downloads_the_published_graph(fake_stack: dict[str, list]) -> None:
    nano.load_nano("m", "cpu", "float32", backend="onnx", threads=2)
    assert fake_stack["onnx"] == [("/snap/m", "/x/onnx/model_q8.onnx", 2)]
    assert fake_stack["hub"] == [(nano.ONNX_REPO, "onnx/model_q8.onnx")] and not fake_stack["torch"]


def test_unknown_backend_is_rejected(fake_stack: dict[str, list]) -> None:
    with pytest.raises(ValueError, match="backend"):
        nano.load_nano("m", "cpu", backend="tensorrt")


async def test_weights_are_shared_per_dtype_backend_and_threads(
    fake_stack: dict[str, list],
) -> None:
    """RM-73: identical (model, device, dtype, backend, threads) share one load through the
    registry; any difference loads separately; the last release frees the entry."""
    from memspine.providers.shared import ResourceRegistry

    registry = ResourceRegistry()

    def lease(dtype: str, backend: str, threads: int) -> Any:
        return nano.nano_weights(
            "m", "cpu", dtype, backend=backend, threads=threads, registry=registry
        )

    a, same = lease("float32", "torch", 4), lease("float32", "torch", 4)
    others = [
        lease("bfloat16", "torch", 4),
        lease("float32", "onnx", 4),
        lease("float32", "torch", 2),
    ]
    first = await a.get()
    assert await same.get() is first
    assert all([await other.get() is not first for other in others])
    assert len(fake_stack["torch"]) == 3 and len(fake_stack["onnx"]) == 1
    for holder in (a, same, *others):
        await holder.aclose()
    assert registry.stats() == ()


def test_adapter_hands_its_knobs_to_the_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[tuple[Any, ...]] = []

    class M:
        def yes_probability(self, states: list[str], instructions: str) -> list[float]:
            return [0.9 for _ in states]

    def load(name: str, device: str, dtype: str, **kw: Any) -> M:
        got.append((name, device, dtype, kw))
        return M()

    monkeypatch.setattr(decider_mod, "load_nano", load)
    d = OpenDeciderDecider("m", "cpu", threads=2, backend="onnx", dtype="bfloat16")
    assert asyncio.run(d.decide("relevance", "q", "c")).confidence == pytest.approx(0.9)
    assert got == [("m", "cpu", "bfloat16", {"backend": "onnx", "threads": 2})]


async def test_onnx_without_onnxruntime_is_a_missing_service_and_is_not_cached(
    fake_stack: dict[str, list], monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine.providers.shared import ResourceRegistry

    registry = ResourceRegistry()
    monkeypatch.setitem(sys.modules, "onnxruntime", None)  # import raises ImportError
    weights = nano.nano_weights("m", "cpu", backend="onnx", registry=registry)
    for _ in range(2):  # RM-73: a failed load is not cached; each use retries
        with pytest.raises(MissingServiceError):
            await weights.get()
    assert registry.stats() == ()
    monkeypatch.setitem(sys.modules, "onnxruntime", types.ModuleType("onnxruntime"))
    assert await weights.get() is not None  # the dependency arrived: the retry loads
    assert len(fake_stack["onnx"]) == 1
    await weights.aclose()
    nano.load_nano("m", "cpu", backend="torch")  # the torch backend is unaffected
    assert len(fake_stack["torch"]) == 1


async def test_a_failed_backend_keeps_the_heuristic_answer(fake_stack: dict[str, list]) -> None:
    """The decider is an enhancer: an unavailable backend surfaces as an error to the caller,
    which the engine turns into the heuristic answer (``read.decider_failed``)."""
    sys.modules["onnxruntime"] = None  # type: ignore[assignment]
    eng = _engine(decider="opendecider", decider_backend="onnx", decider_tasks=["list_mode"])
    await eng.start()
    try:
        with search_forensics() as stages:
            label = await eng._decided("list_mode", "What hobbies does Tim have?", None, "single")
        assert label == "single"  # the heuristic label stands
        assert stages["decisions"][0]["used"] is False and "error" in stages["decisions"][0]
    finally:
        await eng.stop()


# -- worker pool -------------------------------------------------------------------------------


class _SlowModel:
    """Records the most threads inside ``yes_probability`` at once."""

    def __init__(self) -> None:
        self.now = 0
        self.peak = 0
        self.lock = threading.Lock()

    def yes_probability(self, states: list[str], instructions: str) -> list[float]:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        time.sleep(0.03)
        with self.lock:
            self.now -= 1
        return [0.8 for _ in states]


async def _burst(d: OpenDeciderDecider, n: int) -> None:
    await asyncio.gather(*(d.decide("relevance", f"q{i}", "c") for i in range(n)))


def test_worker_pool_bounds_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _SlowModel()
    monkeypatch.setattr(decider_mod, "load_nano", lambda *a, **k: model)
    d = OpenDeciderDecider("m", "cpu", workers=2, threads=1)
    asyncio.run(_burst(d, 10))
    assert d._pool is not None and d._pool._max_workers == 2
    assert 1 <= model.peak <= 2


def test_single_worker_creates_no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _SlowModel()
    monkeypatch.setattr(decider_mod, "load_nano", lambda *a, **k: model)
    d = OpenDeciderDecider("m", "cpu", workers=1, threads=1)
    asyncio.run(_burst(d, 3))
    assert d._pool is None


def test_workers_below_one_are_clamped() -> None:
    assert OpenDeciderDecider("m", "cpu", workers=0).workers == 1


# -- optional real parity (skipped when the model is not downloaded) ---------------------------

_PROBES: list[tuple[str, str, str | None]] = [
    ("refusal", "Where did Tim live?", "That is not mentioned."),
    ("refusal", "Where did Tim live?", "Tim lived in Paris."),
    ("refusal", "What is Ana's job?", "I cannot find that in the memories."),
    ("refusal", "What is Ana's job?", "She is a nurse."),
    ("list_mode", "What hobbies does Tim have?", None),
    ("list_mode", "When did Tim move?", None),
    ("list_mode", "Which cities has Ana visited?", None),
    ("relevance", "Write me a poem about autumn", "- Caroline moved from her home country"),
    ("relevance", "Where did Caroline move from?", "- Caroline moved from her home country"),
    ("relevance", "What did Tim eat on Sunday?", "- Tim: football on Sunday"),
    ("relevance", "Explain quantum tunnelling", "- Tim: football on Sunday"),
    ("refusal", "Who is Sam's manager?", "No information is available."),
]


def _optional_runtime_installed(*names: str) -> bool:
    """False only when a named optional top-level package is absent; a broken
    installed runtime propagates instead of reading as "not downloaded" (M-MODEL)."""
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            return False
    return True


def _cached(repo: str, filename: str) -> bool:
    from huggingface_hub import try_to_load_from_cache

    return isinstance(try_to_load_from_cache(repo, filename), str)


def _torch_model_cached() -> bool:
    if not _optional_runtime_installed("safetensors", "torch", "transformers", "huggingface_hub"):
        return False
    return all(
        _cached(decider_mod.DEFAULT_MODEL, f)
        for f in ("opendecider.json", "model.safetensors", "head.safetensors")
    )


def _onnx_cached() -> bool:
    if not _optional_runtime_installed("onnxruntime"):
        return False
    return _torch_model_cached() and _cached(nano.ONNX_REPO, "onnx/model_q8.onnx")


def test_onnx_cache_guard_does_not_hide_a_broken_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent onnxruntime reads as not cached; a transitive import failure surfaces."""

    def absent(name: str) -> None:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(importlib, "import_module", absent)
    assert _onnx_cached() is False

    def broken(name: str) -> None:
        raise ModuleNotFoundError("No module named 'onnx_dep'", name="onnx_dep")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        _onnx_cached()


def _p_yes(**kw: Any) -> list[float]:
    d = OpenDeciderDecider(decider_mod.DEFAULT_MODEL, "cpu", threads=2, **kw)
    return [x.raw["p_yes"] for x in d.decide_many_sync(_PROBES)]


@pytest.mark.skipif(not _torch_model_cached(), reason="opendecider-nano is not downloaded")
def test_real_bf16_agrees_with_fp32() -> None:
    """I38 claim (register): bfloat16 agreed on 300/300 decisions, max probability difference
    0.0075. Re-check on a small fixed probe set: every label agrees and no probability moves
    by more than 0.05."""
    fp32, bf16 = _p_yes(dtype="float32"), _p_yes(dtype="bfloat16")
    assert [p >= 0.5 for p in fp32] == [p >= 0.5 for p in bf16]
    assert max(abs(a - b) for a, b in zip(fp32, bf16, strict=True)) < 0.05
    assert all(t in TASKS for t, _, _ in _PROBES)


@pytest.mark.skipif(not _onnx_cached(), reason="opendecider-nano ONNX build is not downloaded")
def test_real_onnx_agrees_with_fp32() -> None:
    """The 8-bit ONNX build agrees with fp32 on every label of the probe set; the probability
    difference is bounded (weight-only int8, so looser than bf16)."""
    fp32, onnx = _p_yes(dtype="float32"), _p_yes(backend="onnx")
    assert [p >= 0.5 for p in fp32] == [p >= 0.5 for p in onnx]
    assert max(abs(a - b) for a, b in zip(fp32, onnx, strict=True)) < 0.10
