"""MD-01: model tiers, placement, no-model fallbacks, device plan, thinking (wires LIB-42)."""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import EmbeddingSlot, MemspineConfig
from memspine.config.schema.model import GpuDecl, ModelConfig, ModelDecl, PlacementConfig
from memspine.exceptions import ConfigError
from memspine.models.resolve import (
    emit_taps,
    rerank_chain,
    resolve_models,
    resolve_role,
    role_config,
)
from memspine.models.tiers import ROLE_CATALOGUE
from memspine.observability import tap
from memspine.prompts.roles import CATALOGUE_ROLE, PROMPT_ROLES
from memspine.providers.contracts import BuildResult
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.runtime import container
from memspine.services.llm.base import LLMRouter, ThinkingCapped

SMALL = ModelDecl(
    id="ollama/small-3b",
    provider="openai_compatible",
    tier="small-local",
    vram_mb=2500,
    ram_mb=3000,
    context_vram_mb=500,
    timeout_seconds=60.0,
)
NINE = ModelDecl(
    id="ollama/nine-b",
    provider="openai_compatible",
    tier="worker",
    vram_mb=6500,
    ram_mb=7000,
    context_vram_mb=1000,
    supports_thinking=True,
    timeout_seconds=60.0,
)
BIG = ModelDecl(
    id="openai/frontier",
    provider="openai_compatible",
    tier="frontier",
    hosted=True,
    timeout_seconds=60.0,
)
RTX = GpuDecl(name="NVIDIA GeForce RTX 5080", vram_mb=16303)


#: The old ``read.rerank`` default chain, now configured in ``model.rerank.chain`` (ADR-082).
_V2_CHAIN = [{"provider": p, "settings": {}} for p in ("jina", "qwen3", "onnx")]


def _models(**kw: Any) -> PlacementConfig:
    """ADR-082: the planning keys live in ``model.placement`` (``resolve_models`` reads it)."""
    return PlacementConfig(tiers=True, **kw)


def _model(
    llm: dict[str, Any] | None = None, chain: list[Any] | None = None, **placement: Any
) -> ModelConfig:
    """A ``model`` block: placement, explicit ``model.llm`` roles and the rerank chain."""
    return ModelConfig(
        llm=llm or {},
        rerank={"chain": chain or []},
        placement=_models(**placement),
    )


def _runtime() -> dict[str, Any]:
    return {
        "provider_services": {
            "health_timeout_s": 2.0,
            "close_timeout_s": 2.0,
            "startup_timeout_s": None,
        }
    }


def _config(**kwargs: Any) -> MemspineConfig:
    kwargs.setdefault("runtime", _runtime())
    return MemspineConfig(**kwargs)


class _Hooks:
    withheld_texts = None


class _Stub:
    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return f"stub:{self.model}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        return "{}"


@pytest.fixture
def stub_providers(monkeypatch: pytest.MonkeyPatch) -> list[_Stub]:
    made: list[_Stub] = []
    real_build = container._build_provider

    async def build(
        wiring: Any, descriptor: Any, settings: Any, context: Any, *, result_key: str | None = None
    ) -> BuildResult[Any]:
        if descriptor.ref.category != "llm":
            return await real_build(wiring, descriptor, settings, context, result_key=result_key)
        service = _Stub(settings.model)
        made.append(service)
        result = BuildResult(service, descriptor.ref, (), descriptor.declared_capabilities)
        wiring.provider_results[result_key or descriptor.ref.name] = result
        return result

    monkeypatch.setattr(container, "_build_provider", build)
    return made


async def _build(cfg: MemspineConfig) -> LLMRouter:
    wiring = container.Wiring(
        resources=ProviderLifecycleManager(close_timeout_s=2.0),
        health_timeout_s=2.0,
        startup_timeout_s=None,
    )
    return await container.build_llm_router(cfg, _Hooks(), wiring)  # type: ignore[arg-type]


# ---- flag off ----------


async def test_default_is_off_and_config_roles_resolve_as_before(
    stub_providers: list[_Stub],
) -> None:
    cfg = _config(
        model={
            "llm": {
                "extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "ollama/x"},
                }
            },
            "placement": {"catalogue": [NINE]},
        },
    )
    assert cfg.model.placement.tiers is False
    router = await _build(cfg)
    assert router.roles == ["extract"]  # the catalogue is ignored while the flag is off
    assert router.model_plan is None
    assert [s.model for s in stub_providers] == ["ollama/x"]


# ---- each tier resolves ----------


def test_each_tier_resolves_per_role() -> None:
    r = resolve_models(
        _models(
            catalogue=[SMALL, NINE, BIG],
            gpus=[RTX],
            allow_hosted=True,
            role_tier={"extract": "small-local", "judge": "worker", "chat": "frontier"},
        )
    )
    ext = resolve_role(r, "window_extract")
    assert (ext.status, ext.model_id, ext.tier) == ("bound", "ollama/small-3b", "small-local")
    assert resolve_role(r, "judge").tier == "worker"
    chat = resolve_role(r, "chat")
    assert (chat.model_id, chat.tier, chat.device) == ("openai/frontier", "frontier", "remote")
    # an engine role maps onto its catalogue role
    assert resolve_role(r, "resolve_entity").catalogue_role == "resolve"
    assert resolve_role(r, "no_such_role").status == "unmapped"


def test_tier_none_turns_the_role_off_with_its_fallback() -> None:
    r = resolve_models(_models(catalogue=[NINE], gpus=[RTX], role_tier={"extract": "none"}))
    res = resolve_role(r, "window_extract")
    assert res.status == "disabled" and res.fallback == "skip" and res.reason_code == "disabled"
    assert resolve_role(r, "judge").status == "bound"  # the other roles still resolve


def test_tier_without_a_declared_model_turns_the_role_off() -> None:
    r = resolve_models(_models(catalogue=[SMALL], gpus=[RTX], role_tier={"judge": "frontier"}))
    assert resolve_role(r, "judge").status == "disabled"
    assert any("no declared model" in n for n in r.notes)


def test_unknown_role_names_fail_the_check() -> None:
    r = resolve_models(_models(role_tier={"bogus": "none"}, pins={"nope": "x"}))
    with pytest.raises(ConfigError, match="unknown role"):
        r.check()


def test_hosted_model_is_refused_for_a_raw_turn_role() -> None:
    r = resolve_models(
        _models(catalogue=[BIG], allow_hosted=True, pins={"extract": "openai/frontier"})
    )
    with pytest.raises(ConfigError, match="must stay local"):
        r.check()


# ---- fallback (P-03) ----------


def test_missing_required_role_fails_start_unless_degraded() -> None:
    with pytest.raises(ConfigError, match="no model for role extract"):
        resolve_models(_models(required_roles=["extract"])).check()
    r = resolve_models(_models(required_roles=["extract"], on_missing_model="rules"))
    r.check()
    res = resolve_role(r, "window_extract")
    assert (res.status, res.fallback) == ("fallback", "rules")
    assert resolve_role(r, "extract_edges").fallback == "skip"
    assert resolve_role(r, "relevance").fallback == "calibrated"


async def test_window_extract_adds_the_extract_requirement(stub_providers: list[_Stub]) -> None:
    cfg = _config(model=_model(), write={"window_extract": "facts"})
    with pytest.raises(ConfigError, match="extract"):
        await _build(cfg)


# ---- no-model template: zero calls ----------


def _engine(model: ModelConfig, **extra: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model=model.model_copy(update={"embedding": EmbeddingSlot(provider="hash")}),
        runtime=_runtime(),
        **extra,
    )


async def test_no_model_template_makes_zero_calls_across_write_sleep_read(
    stub_providers: list[_Stub],
) -> None:
    eng = _engine(
        _model(on_missing_model="skip", role_tier={r.name: "none" for r in ROLE_CATALOGUE}),
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, "reflect_profile": True}},
            },
            "semantic": {"enabled": True},
            "reflective": {"enabled": True},
        },
        read={"hybrid": False, "compose_rewrites": True},
    )
    await eng.start()
    try:
        msgs = [
            {"role": "user", "content": c, "timestamp": f"2023-05-08T13:0{i}:00+00:00"}
            for i, c in enumerate(["Ilse: I run every morning", "Ilse: no coffee after noon"])
        ]
        await eng.write("Ilse lives in Bergen", namespace="a", memory_type="semantic")
        await eng.ingest_messages(msgs, namespace="a", session_id="s1", group_id="s1")
        hits = await eng.read("How often does Ilse run?", namespace="a", mode="compose", top_k=2)
        await eng.sleep()
        assert hits is not None  # the legs returned (possibly empty) rather than erroring
        assert eng.model_calls() == {}
        assert stub_providers == []  # no provider was even built
        assert eng.describe()["llm_roles"] == []
        plan = eng.describe()["models"]
        assert plan["layers"] == ["L0"]
        assert all(r["status"] == "disabled" for r in plan["roles"])
    finally:
        await eng.stop()


async def test_bound_roles_do_make_calls_counted_by_the_stub(stub_providers: list[_Stub]) -> None:
    eng = _engine(
        _model(catalogue=[NINE], gpus=[RTX], role_tier={"query_rewrite": "worker"}),
        read={"hybrid": False, "compose_rewrites": True},
    )
    await eng.start()
    try:
        assert "query_rewrite" in eng.describe()["llm_roles"]
        await eng.write("Ilse lives in Bergen", namespace="a", memory_type="semantic")
        await eng.read("Where does Ilse live?", namespace="a", mode="compose", top_k=2)
        assert sum(s.calls for s in stub_providers) == sum(eng.model_calls().values())
        assert eng.model_calls().get("query_rewrite", 0) >= 1
    finally:
        await eng.stop()


async def test_explicit_llm_roles_win_over_the_plan(stub_providers: list[_Stub]) -> None:
    cfg = _config(
        model=_model(
            llm={
                "extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 60.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "ollama/pinned"},
                }
            },
            catalogue=[NINE],
            gpus=[RTX],
        ),
    )
    router = await _build(cfg)
    assert router.provider("extract").model == "ollama/pinned"
    assert router.provider("judge").model == "ollama/nine-b"


# ---- placement (P-19) ----------


def test_placement_never_exceeds_the_vram_budget() -> None:
    rng = random.Random(7)
    for _ in range(200):
        llms = [
            ModelDecl(
                id=f"ollama/m{i}",
                provider="openai_compatible",
                tier=rng.choice(["small-local", "worker", "frontier"]),
                vram_mb=rng.randint(500, 14000),
                ram_mb=rng.randint(500, 14000),
                context_vram_mb=rng.randint(0, 2500),
                timeout_seconds=60.0,
            )
            for i in range(rng.randint(1, 4))
        ]
        rr = [
            ModelDecl(
                id=f"rr{i}", kind="rerank", family=f, vram_mb=rng.randint(500, 3000), ram_mb=500
            )
            for i, f in enumerate(["jina-v3.5", "qwen3-reranker-0.6b"])
        ]
        emb = [
            ModelDecl(
                id="e-gpu",
                kind="embedding",
                profile="gpu",
                vram_mb=rng.randint(500, 5000),
                placement="gpu",
            ),
            ModelDecl(id="e-cpu", kind="embedding", profile="cpu", ram_mb=500, placement="cpu"),
        ]
        gpu = GpuDecl(name="NVIDIA RTX", vram_mb=rng.choice([4096, 8192, 16303]))
        r = resolve_models(_models(catalogue=[*llms, *rr, *emb], gpus=[gpu], ram_mb=32768))
        for device, budget, used in r.plan.memory:
            if budget:
                assert used <= budget, (device, used, budget)
        on_gpu = sum(p.mb for p in r.plan.placements if p.device.startswith("cuda"))
        assert on_gpu <= gpu.vram_mb - 512


def test_plan_needs_no_gpu_and_is_deterministic() -> None:
    a = resolve_models(_models(catalogue=[SMALL, NINE], gpus=[RTX]))
    b = resolve_models(_models(catalogue=[SMALL, NINE], gpus=[RTX]))
    assert a.plan.to_dict() == b.plan.to_dict()
    cpu_only = resolve_models(_models(catalogue=[NINE], ram_mb=16000))
    assert {p.device for p in cpu_only.plan.placements if p.kind == "llm"} == {"cpu"}


def test_igpu_is_ignored_only_cuda_is_planned() -> None:
    amd = GpuDecl(name="AMD Radeon(TM) Graphics", vram_mb=2048)
    r = resolve_models(_models(catalogue=[NINE], gpus=[amd]))
    assert all(not p.device.startswith("cuda") for p in r.plan.placements)
    assert any("ignored" in n for n in r.notes)
    both = resolve_models(_models(catalogue=[SMALL], gpus=[amd, RTX]))
    assert {p.device for p in both.plan.placements if p.kind == "llm"} == {"cuda:0"}


def test_encoders_share_the_gpu_only_when_they_fit() -> None:
    emb = ModelDecl(id="instr-0.6b", kind="embedding", profile="gpu", vram_mb=1500, placement="gpu")
    cpu_emb = ModelDecl(
        id="multi-onnx", kind="embedding", profile="cpu", ram_mb=500, placement="cpu"
    )
    roomy = resolve_models(_models(catalogue=[NINE, emb, cpu_emb], gpus=[RTX]))
    assert (roomy.plan.reranker_device, roomy.plan.embedder_device) == ("cuda:0", "cuda:0")
    tight = resolve_models(
        _models(catalogue=[NINE, emb, cpu_emb], gpus=[GpuDecl(name="NVIDIA", vram_mb=9000)])
    )
    assert tight.plan.embedder == "multi-onnx" and tight.plan.embedder_device == "cpu"


# ---- reranker chain ----------


def test_reranker_chain_jina_then_qwen3_then_minilm_on_cpu() -> None:
    r = resolve_models(_models(catalogue=[NINE], gpus=[RTX]))
    assert rerank_chain(r) == [("jina", "cuda:0"), ("qwen3", "cuda:0"), ("onnx", "cpu")]
    assert r.plan.reranker == "jina-reranker-v3.5" and r.plan.rerank_gate == "always"


def test_reranker_chain_without_gpu_is_minilm_on_cpu() -> None:
    r = resolve_models(_models())
    assert rerank_chain(r) == [("onnx", "cpu")]
    assert r.plan.rerank_gate == "gated"


def test_reranker_chain_skips_what_does_not_fit_and_can_be_empty() -> None:
    tight = resolve_models(_models(catalogue=[NINE], gpus=[GpuDecl(name="NVIDIA", vram_mb=9300)]))
    assert [m for m, _ in rerank_chain(tight)] == ["onnx"]
    # ADR-082: ``models.rerank: off`` is removed; the engine plans no reranker for an empty
    # ``model.rerank.chain`` (``rerank=bool(chain)``).
    off = resolve_models(_models(catalogue=[NINE], gpus=[RTX]), rerank=False)
    assert rerank_chain(off) == [] and off.plan.rerank_unscored


async def test_chain_score_follows_the_plan_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.read.pipeline import rerank as rr

    built: list[tuple[str, str | None]] = []

    class _RR:
        def __init__(self, mode: str) -> None:
            self.reranker_id = mode

        async def rerank(self, query: str, documents: list[str]) -> list[float]:
            return [float(len(d)) for d in documents]

    async def fake_build(settings: Any) -> Any:
        built.append((settings.mode, settings.device))
        return None if settings.mode == "jina" else _RR(settings.mode)

    monkeypatch.setattr(rr, "build_reranker", fake_build)
    cfg = _config(model=_model(catalogue=[NINE], gpus=[RTX], chain=_V2_CHAIN))
    eng = SimpleNamespace(_config=lambda: cfg, _rerank_calls=0, _rerank_failures=0)
    scores, model, fallback, tried = await rr.chain_score(eng, "q", ["a", "bb"], cfg.model.rerank)
    assert built == [("jina", "cuda:0"), ("qwen3", "cuda:0")]
    assert (scores, model, fallback) == ([1.0, 2.0], "qwen3", "qwen3")
    assert tried[0] == {"mode": "jina", "outcome": "unavailable"}


async def test_chain_score_flag_off_uses_the_config_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.read.pipeline import rerank as rr

    built: list[str] = []

    async def fake_build(settings: Any) -> None:
        built.append(settings.mode)

    monkeypatch.setattr(rr, "build_reranker", fake_build)
    cfg = _config(model={"rerank": {"chain": _V2_CHAIN}})
    eng = SimpleNamespace(_config=lambda: cfg, _rerank_calls=0, _rerank_failures=0)
    scores, model, _, _ = await rr.chain_score(eng, "q", ["a"], cfg.model.rerank)
    assert built == ["jina", "qwen3", "onnx"] and scores == [None] and model is None


# ---- thinking per role (P-15) ----------


def test_thinking_is_off_by_default_for_every_role() -> None:
    r = resolve_models(_models(catalogue=[NINE], gpus=[RTX]))
    roles = [role for role in PROMPT_ROLES if role in CATALOGUE_ROLE]
    assert {resolve_role(r, role).thinking for role in roles} == {"off"}
    assert role_config(r, "judge").settings["no_think"] is True  # thinking is disabled
    only_small = resolve_models(_models(catalogue=[SMALL], gpus=[RTX]))
    # judge needs a reader-class model
    assert resolve_role(only_small, "judge").status == "fallback"


def test_catalogue_generation_defaults_are_forwarded_only_when_explicit() -> None:
    declared = ModelDecl(
        id="openai/example",
        provider="openai_compatible",
        tier="worker",
        vram_mb=6000,
        timeout_seconds=30.0,
        temperature=0.0,
        top_p=0.75,
        max_tokens=777,
    )
    resolved = resolve_models(_models(catalogue=[declared], gpus=[RTX]))
    configured = role_config(resolved, "chat")
    assert configured is not None
    assert configured.settings["temperature"] == 0.0
    assert configured.settings["top_p"] == 0.75
    assert configured.settings["max_tokens"] == 777

    inherited = resolve_models(_models(catalogue=[NINE], gpus=[RTX]))
    inherited_config = role_config(inherited, "chat")
    assert inherited_config is not None
    assert "temperature" not in inherited_config.settings
    assert "top_p" not in inherited_config.settings
    assert "max_tokens" not in inherited_config.settings


def test_llamacpp_catalogue_binding_validates_without_transport_fields_or_model_load() -> None:
    from memspine.providers.builtin.models import register
    from memspine.providers.registry import LLM_PROVIDERS
    from memspine.runtime.container import llm_settings

    register()
    declared = ModelDecl(
        id="llamacpp/C:/models/test.gguf",
        provider="llamacpp",
        tier="worker",
        vram_mb=6000,
        timeout_seconds=30.0,
    )
    resolved = resolve_models(_models(catalogue=[declared], gpus=[RTX]))
    configured = role_config(resolved, "chat")
    assert configured is not None
    assert not {"api_base", "api_key", "aws_region"}.intersection(configured.settings)

    descriptor = LLM_PROVIDERS.get("llamacpp")
    assert configured.timeout_s == 30.0  # ADR-082: the slot carries the deadline
    typed = llm_settings(descriptor, configured)
    assert typed.model == declared.id
    assert typed.timeout_seconds == 30.0


def test_capped_thinking_only_for_judge_and_reflect() -> None:
    r = resolve_models(
        _models(
            catalogue=[NINE],
            gpus=[RTX],
        ),
        thinking={"judge": 512, "reflect": 900, "chat": 300, "extract": 99},
    )
    judge, reflect = resolve_role(r, "judge"), resolve_role(r, "reflect")
    assert (judge.thinking, judge.thinking_cap) == ("capped", 512)
    assert (reflect.thinking, reflect.thinking_cap) == ("capped", 900)
    assert resolve_role(r, "chat").thinking == "off"
    assert resolve_role(r, "window_extract").thinking == "off"
    assert role_config(r, "judge").settings["no_think"] is False
    assert role_config(r, "chat").settings["no_think"] is True


def test_capped_thinking_needs_a_model_that_can_think() -> None:
    plain = ModelDecl(
        id="ollama/plain-9b",
        provider="openai_compatible",
        tier="worker",
        vram_mb=6000,
        timeout_seconds=60.0,
    )
    r = resolve_models(_models(catalogue=[plain], gpus=[RTX]), thinking={"judge": 512})
    res = resolve_role(r, "judge")
    assert res.thinking == "off" and res.reason_code == "thinking_not_applied"
    assert role_config(r, "judge").settings.get("no_think") is None


async def test_router_wraps_a_capped_role_and_leaves_others(stub_providers: list[_Stub]) -> None:
    """RM-118: ``model.placement.thinking`` caps a plan-picked role (was ``models.thinking``)."""
    cfg = _config(model=_model(catalogue=[NINE], gpus=[RTX], thinking={"judge": 512}))
    router = await _build(cfg)
    judge = router.provider("judge")
    assert isinstance(judge, ThinkingCapped) and judge.cap_tokens == 512
    assert not isinstance(router.provider("chat"), ThinkingCapped)


async def test_explicit_slot_thinking_and_placement_thinking_cap_their_own_roles(
    stub_providers: list[_Stub],
) -> None:
    """RM-118: an explicit judge slot keeps its own budget; the planned reflect role takes
    ``model.placement.thinking``."""
    judge_slot = {
        "provider": "openai_compatible",
        "timeout_s": 30.0,
        "thinking": 128,
        "settings": {"model": "ollama/explicit-judge"},
    }
    cfg = _config(
        model=_model(
            llm={"judge": judge_slot}, catalogue=[NINE], gpus=[RTX], thinking={"reflect": 700}
        )
    )
    router = await _build(cfg)
    judge, reflect = router.provider("judge"), router.provider("reflect")
    assert isinstance(judge, ThinkingCapped) and judge.cap_tokens == 128
    assert isinstance(reflect, ThinkingCapped) and reflect.cap_tokens == 700
    assert not isinstance(router.provider("chat"), ThinkingCapped)


async def test_thinking_cap_sets_the_reply_budget() -> None:
    seen: dict[str, Any] = {}

    class _Inner(_Stub):
        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            seen.update(options)
            return "ok"

    capped = ThinkingCapped(_Inner("m"), cap_tokens=512, max_tokens=512)
    await capped.chat([])
    assert seen["max_tokens"] == 1024
    await capped.chat([], max_tokens=64)
    assert seen["max_tokens"] == 64
    assert capped.model == "m"


# ---- taps ----------


def test_taps_report_resolve_and_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "enabled", lambda: True)
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: events.append((stage, f)))
    r = resolve_models(_models(catalogue=[NINE], gpus=[RTX], role_tier={"extract": "none"}))
    emit_taps(r, ("window_extract", "judge"))
    assert [s for s, _ in events] == ["models.placement", "models.resolve", "models.resolve"]
    placement = events[0][1]
    assert placement["devices"][0]["device"] == "cuda:0"
    assert placement["devices"][0]["used_mb"] <= placement["devices"][0]["budget_mb"]
    ext, judge = events[1][1], events[2][1]
    assert (ext["status"], ext["fallback"], ext["fallback_reason"]) == (
        "disabled",
        "skip",
        "disabled",
    )
    assert (judge["model"], judge["device"], judge["thinking"], judge["tier"]) == (
        "ollama/nine-b",
        "cuda:0",
        "off",
        "worker",
    )
