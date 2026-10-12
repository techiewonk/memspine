"""LIB-42: model tiers, placement, fallbacks, thinking per role."""

from __future__ import annotations

from dataclasses import replace

import pytest

from memspine.models.tiers import (
    ROLE_CATALOGUE,
    GpuInfo,
    Hardware,
    Inventory,
    ModelInfo,
    PlanPrefs,
    Tier,
    explain,
    plan_models,
)

JINA = ModelInfo("jina-v3.5", "reranker", family="jina-v3.5", vram_mb=1200, ram_mb=1500)
QWEN_RR = ModelInfo("qwen3-rr-0.6b", "reranker", family="qwen3-reranker-0.6b", vram_mb=1300)
MINI = ModelInfo("minilm-rr", "reranker", family="minilm", ram_mb=150, gpu_ok=False)
EMB_CPU = ModelInfo("multi-onnx", "embedder", profile="cpu", ram_mb=500, gpu_ok=False)
EMB_GPU = ModelInfo("instr-0.6b", "embedder", profile="gpu", vram_mb=1500, cpu_ok=False)
SMALL = ModelInfo(
    "small-3b",
    "llm",
    provider="ollama",
    tier=Tier.SMALL,
    vram_mb=2500,
    ram_mb=3000,
    context_vram_mb=500,
)
NINE = ModelInfo(
    "nine-b",
    "llm",
    provider="ollama",
    tier=Tier.MEDIUM,
    vram_mb=6500,
    ram_mb=7000,
    context_vram_mb=1000,
    supports_thinking=True,
)
BIG = ModelInfo(
    "big-14b",
    "llm",
    provider="ollama",
    tier=Tier.LARGE,
    vram_mb=10000,
    ram_mb=11000,
    context_vram_mb=1500,
    supports_thinking=True,
)
CLOUD = ModelInfo("cloud-x", "llm", provider="openai", tier=Tier.LARGE, hosted=True)


def inv(*ms):
    return Inventory(tuple(ms))


CPU_BOX = Hardware(ram_mb=16000, cpu_threads=8)
GPU16 = Hardware(gpus=(GpuInfo("g0", 16000),), ram_mb=32000)
GPU8 = Hardware(gpus=(GpuInfo("g0", 8192),), ram_mb=32000)


def test_nothing_installed_is_pure_l0():
    plan = plan_models(inv(), CPU_BOX)
    assert plan.layers == ("L0",)
    assert all(b.status == "fallback" for b in plan.roles)
    assert plan.reranker == "" and plan.rerank_unscored and plan.rerank_gate == ""
    assert plan.embedder == ""
    caps = dict(plan.unavailable)
    for c in (
        "layer.L1",
        "layer.L3",
        "read.query_rewrite",
        "read.relevance_model_gate",
        "rerank",
        "answer",
    ):
        assert c in caps
    assert plan.errors == ()
    assert "layer.L0" in plan.available
    text = "\n".join(explain(plan))
    assert "layers: L0" in text and "reranker: none, results unscored" in text


def test_relevance_fallback_is_store_calibrated():
    plan = plan_models(inv(), CPU_BOX)
    b = plan.binding("relevance")
    assert b.fallback == "calibrated" and "store-calibrated" in b.note
    assert plan.binding("query_rewrite").fallback == "skip"


def test_cpu_only_box_runs_small_worker_on_cpu():
    plan = plan_models(inv(SMALL, MINI, EMB_CPU), CPU_BOX)
    b = plan.binding("extract")
    assert b.status == "bound" and b.device == "cpu" and b.model_id == "small-3b"
    assert plan.layers == ("L0", "L1", "L3")
    # the reader tier needs a medium model: not served by the small one
    assert plan.binding("chat").status == "fallback"
    assert "answer" in dict(plan.unavailable)
    assert (
        plan.reranker == "minilm-rr"
        and plan.reranker_device == "cpu"
        and plan.rerank_gate == "gated"
    )
    assert plan.embedder == "multi-onnx" and plan.embedder_device == "cpu"


def test_single_model_is_shared_across_roles():
    plan = plan_models(inv(NINE), GPU16)
    assert {b.model_id for b in plan.roles if b.status == "bound"} == {"nine-b"}
    ext = plan.binding("extract")
    assert "chat" in ext.shared_with and "judge" in ext.shared_with
    assert sum(1 for p in plan.placements if p.kind == "llm") == 1
    assert plan.binding("chat").device == "cuda:0"


def test_two_tiers_worker_small_reader_large():
    plan = plan_models(inv(SMALL, BIG), Hardware(gpus=(GpuInfo("g", 24000),)))
    assert plan.binding("extract").model_id == "small-3b"
    assert plan.binding("chat").model_id == "big-14b"
    assert plan.binding("query_rewrite").model_id == "small-3b"


def test_limited_vram_spills_lower_priority_to_cpu():
    # 8 GB card: a GPU-only 14B fits nowhere and is dropped; nine-b (7.5 GB with ctx) fits
    big_gpu_only = replace(BIG, cpu_ok=False)
    plan = plan_models(inv(big_gpu_only, NINE, SMALL, JINA, EMB_CPU), GPU8)
    assert plan.binding("chat").model_id == "nine-b"
    assert plan.binding("chat").device == "cuda:0"
    assert (
        plan.binding("extract").model_id == "small-3b" and plan.binding("extract").device == "cpu"
    )
    # budget never exceeded on the gpu
    for dev, budget, used in plan.memory:
        if dev.startswith("cuda"):
            assert used <= budget
    # jina does not fit next to the LLM on 8 GB, so it lands on CPU and is gated
    assert plan.reranker == "jina-v3.5"
    assert plan.reranker_device == "cpu" and plan.rerank_gate == "gated"


def test_model_that_fits_nowhere_is_dropped_and_roles_reresolve():
    hw = Hardware(gpus=(GpuInfo("g", 4096),), ram_mb=9000)
    plan = plan_models(inv(BIG, NINE, SMALL), hw)
    # 14B (11.5 GB cpu) too big for RAM, 9B (8 GB cpu) fits RAM
    assert plan.binding("chat").model_id == "nine-b"
    assert any("big-14b" in n and "dropped" in n for n in plan.notes)


def test_reader_unplaceable_leaves_reader_roles_off():
    hw = Hardware(gpus=(), ram_mb=4000)
    plan = plan_models(inv(NINE, SMALL), hw)
    assert plan.binding("chat").status == "fallback"
    assert plan.binding("extract").model_id == "small-3b"


def test_thinking_off_for_extraction_even_if_requested():
    prefs = PlanPrefs(thinking_optin=frozenset({"extract", "extract_edges", "chat"}))
    plan = plan_models(inv(NINE), GPU16, prefs)
    for r in ("extract", "extract_edges", "summarize", "chat", "sufficiency", "query_rewrite"):
        assert plan.binding(r).thinking.mode == "off"
    assert "not allowed" in plan.binding("extract").note
    assert plan.binding("extract").thinking.provider_options == {"think": False}


def test_capped_thinking_only_on_optin_and_support():
    base = plan_models(inv(NINE), GPU16)
    assert base.binding("judge").thinking.mode == "off"
    plan = plan_models(inv(NINE), GPU16, PlanPrefs(thinking_optin=frozenset({"judge", "reflect"})))
    j = plan.binding("judge").thinking
    assert j.mode == "capped" and j.cap_tokens == 1024
    assert plan.binding("reflect").thinking.cap_tokens == 2048
    nosupport = ModelInfo("nine-b", "llm", tier=Tier.MEDIUM, vram_mb=6500)
    plan = plan_models(inv(nosupport), GPU16, PlanPrefs(thinking_optin=frozenset({"judge"})))
    assert plan.binding("judge").thinking.mode == "off"
    assert "does not support" in plan.binding("judge").note


def test_non_ollama_gets_no_think_option():
    m = ModelInfo("qwen3-local", "llm", provider="llamacpp", tier=Tier.MEDIUM, vram_mb=3000)
    plan = plan_models(inv(m), GPU16)
    assert plan.binding("extract").thinking.provider_options == {"no_think": True}


@pytest.mark.parametrize(
    "models, expect",
    [
        ((JINA, QWEN_RR, MINI), "jina-v3.5"),
        ((QWEN_RR, MINI), "qwen3-rr-0.6b"),
        ((MINI,), "minilm-rr"),
        ((), ""),
    ],
)
def test_reranker_fallback_chain(models, expect):
    plan = plan_models(inv(*models), GPU16)
    assert plan.reranker == expect
    assert plan.rerank_unscored == (expect == "")


def test_reranker_falls_through_when_memory_short():
    hw = Hardware(gpus=(GpuInfo("g", 1000),), ram_mb=1000)
    # jina (1500 cpu) and qwen (1300) fit nowhere; minilm (150 cpu) fits
    plan = plan_models(inv(JINA, QWEN_RR, MINI), hw)
    assert plan.reranker == "minilm-rr"
    assert any("jina-v3.5" in n for n in plan.notes)


def test_jina_on_gpu_runs_always_on_cpu_gated():
    assert plan_models(inv(JINA), GPU16).rerank_gate == "always"
    assert plan_models(inv(JINA), CPU_BOX).rerank_gate == "gated"


def test_rerank_pref_off():
    plan = plan_models(inv(JINA), GPU16, PlanPrefs(rerank=False))
    assert plan.reranker == "" and plan.rerank_unscored


def test_embedder_profiles():
    both = inv(EMB_CPU, EMB_GPU)
    assert plan_models(both, GPU16).embedder == "instr-0.6b"
    assert plan_models(both, CPU_BOX).embedder == "multi-onnx"
    assert plan_models(both, GPU16, PlanPrefs(embedder_profile="cpu")).embedder == "multi-onnx"
    # GPU profile that does not fit a GPU falls back to the CPU default
    tiny = Hardware(gpus=(GpuInfo("g", 1000),), ram_mb=8000)
    plan = plan_models(both, tiny)
    assert plan.embedder == "multi-onnx" and any("does not fit a GPU" in n for n in plan.notes)


def test_pinned_embedder_wins_and_errors_when_missing():
    both = inv(EMB_CPU, EMB_GPU)
    plan = plan_models(both, GPU16, PlanPrefs(pinned_embedder="multi-onnx"))
    assert plan.embedder == "multi-onnx"
    plan = plan_models(both, GPU16, PlanPrefs(pinned_embedder="absent"))
    assert plan.embedder == "" and plan.errors


def test_required_role_missing_is_an_error_not_silent():
    plan = plan_models(inv(), CPU_BOX, PlanPrefs(required_roles=frozenset({"extract"})))
    assert plan.binding("extract").status == "error"
    assert any("extract" in e for e in plan.errors)
    assert plan_models(inv(), CPU_BOX).errors == ()


def test_hosted_only_when_allowed_and_role_hosted_ok():
    off = plan_models(inv(CLOUD), CPU_BOX)
    assert all(b.status != "bound" for b in off.roles)
    on = plan_models(inv(CLOUD), CPU_BOX, PlanPrefs(allow_hosted=True))
    assert on.binding("chat").model_id == "cloud-x" and on.binding("chat").device == "remote"
    # raw-turn write roles stay local even with a hosted model and the pref on
    assert on.binding("extract").status == "fallback"
    assert on.binding("sufficiency").status == "bound"
    assert on.layers == ("L0",)


def test_pins_and_validation():
    plan = plan_models(inv(SMALL, NINE), GPU16, PlanPrefs(pins={"extract": "nine-b"}))
    assert plan.binding("extract").model_id == "nine-b"
    bad = plan_models(inv(SMALL), GPU16, PlanPrefs(pins={"chat": "small-3b", "extract": "missing"}))
    assert len(bad.errors) == 2
    assert bad.binding("chat").status == "fallback"
    hosted = plan_models(
        inv(CLOUD), GPU16, PlanPrefs(allow_hosted=True, pins={"extract": "cloud-x"})
    )
    assert any("must stay local" in e for e in hosted.errors)


def test_disabled_role():
    plan = plan_models(inv(NINE), GPU16, PlanPrefs(disabled_roles=frozenset({"query_rewrite"})))
    assert plan.binding("query_rewrite").status == "disabled"
    assert "read.query_rewrite" in dict(plan.unavailable)


def test_catalogue_defaults_sane():
    names = [r.name for r in ROLE_CATALOGUE]
    assert len(names) == len(set(names))
    for r in ROLE_CATALOGUE:
        assert r.max_tokens > 0 and r.timeout_s > 0
        if r.thinking_allowed:
            assert r.group == "reader" and r.thinking_cap_tokens > 0
    for must in (
        "extract",
        "extract_edges",
        "summarize",
        "call2",
        "judge",
        "chat",
        "query_rewrite",
        "relevance",
        "reflect",
        "plan",
    ):
        assert must in names
    assert not {r.name for r in ROLE_CATALOGUE if r.thinking_allowed} & {"extract", "chat"}


def test_deterministic_and_input_order_independent():
    a = inv(BIG, NINE, SMALL, JINA, QWEN_RR, MINI, EMB_CPU, EMB_GPU)
    b = inv(*reversed(a.models))
    p1, p2, p3 = plan_models(a, GPU16), plan_models(a, GPU16), plan_models(b, GPU16)
    assert p1 == p2 == p3
    assert explain(p1) == explain(p3)
    assert p1.to_dict() == p3.to_dict()


def test_memory_budget_respects_headroom():
    plan = plan_models(
        inv(NINE), Hardware(gpus=(GpuInfo("g", 8000),)), PlanPrefs(gpu_headroom_mb=1000)
    )
    # 7500 needed, budget 7000 -> cpu
    assert plan.binding("chat").device == "cpu"


def test_explain_mentions_unavailable_and_sharing():
    plan = plan_models(inv(NINE), GPU16)
    text = "\n".join(explain(plan))
    assert "shared with" in text and "thinking off" in text
    assert "unavailable vector_search" in text
