"""ADR-082 / RM-117 WP1: the ``model`` block, its slots, validators and accessors.

This file spells the removed keys on purpose (migrate_config_keys_adr082: skip)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from memspine.config.loader import load_config
from memspine.config.schema import (
    EscalationPolicy,
    GlinerSettings,
    MemspineConfig,
    ModelSlot,
    SharedResourcesConfig,
    decider_slot,
    extraction_slot,
    gliner_settings,
    llm_slot,
    llm_slots,
    weights_settings,
)
from memspine.config.schema.model import MOVED_KEYS
from memspine.exceptions import ConfigError

_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}
_GLINER = {"provider": "gliner2", "settings": {"model": "fastino/gliner2.5-base-v1"}}


def _config(**overrides: Any) -> MemspineConfig:
    return MemspineConfig.model_validate({"runtime": _RUNTIME, **overrides})


def _llm(model: str = "ollama/qwen3.5:9b", **extra: Any) -> dict[str, Any]:
    return {
        "provider": "openai_compatible",
        "timeout_s": 60.0,
        "settings": {"api_base": "http://test.invalid/v1", "model": model},
        **extra,
    }


# ---------------------------------------------------------------------------- old keys

_OLD_KEYS: list[tuple[dict[str, Any], str]] = [
    ({"embedding": {"provider": "hash"}}, "model.embedding.provider"),
    ({"embedding": {"settings": {}}}, "model.embedding.settings"),
    ({"embedding": {"profile": "gpu"}}, "model.embedding.preset"),
    ({"embedding": {"pin_profile": True}}, "model.embedding.pin_preset"),
    ({"embedding": {"migrate_profile": True}}, "model.embedding.migrate_preset"),
    ({"embedding": {"batch_size": 8}}, "model.embedding.batch_size"),
    ({"embedding": {"versioned_spaces": True}}, "model.embedding.versioned_spaces"),
    ({"embedding": {}}, "model.embedding"),
    ({"llm": {"roles": {}}}, "model.llm.<role>"),
    ({"llm": {"structured": {"retry_on_error": True}}}, "model.llm.<role>.structured"),
    ({"models": {"profiles": {}}}, "inline slots"),
    ({"models": {"bindings": {}}}, "model.decider.route"),
    ({"models": {"gliner_max_models": 2}}, "model.placement.max_loaded_models"),
    ({"models": {"escalation": {}}}, "model.llm.<role>.escalation"),
    ({"models": {"thinking": {"judge": 64}}}, "model.llm.<role>.thinking"),
    ({"models": {"topic": {}}}, "model.extraction.topic"),
    ({"models": {"embedder_profile": "gpu"}}, "model.embedding.preset"),
    ({"models": {"rerank": "off"}}, "model.rerank.chain: []"),
    ({"models": {"default_rerankers": "none"}}, "model.placement.catalogue_rerankers"),
    ({"models": {"tiers_v2": True}}, "model.placement.tiers_v2"),
    ({"models": {"catalogue": []}}, "model.placement.catalogue"),
    ({"models": {"gpus": []}}, "model.placement.gpus"),
    ({"models": {"detect_gpus": True}}, "model.placement.detect_gpus"),
    ({"models": {"ram_mb": 1}}, "model.placement.ram_mb"),
    ({"models": {"gpu_headroom_mb": 1}}, "model.placement.gpu_headroom_mb"),
    ({"models": {"role_tier": {}}}, "model.placement.role_tier"),
    ({"models": {"pins": {}}}, "model.placement.pins"),
    ({"models": {"allow_hosted": True}}, "model.placement.allow_hosted"),
    ({"models": {"required_roles": []}}, "model.placement.required_roles"),
    ({"models": {"on_missing_model": "skip"}}, "model.placement.on_missing_model"),
    ({"decision": {"provider": "gliner2"}}, "model.decider.route"),
    ({"read": {"rerank": "onnx"}}, "model.rerank.chain"),
    ({"read": {"rerank_settings": {}}}, "model.rerank.chain[].settings"),
    ({"read": {"rerank_v2": True}}, "model.rerank.chain"),
    ({"read": {"rerank_v2_chain": ["jina"]}}, "model.rerank.chain"),
    ({"read": {"rerank_v2_pool": 32}}, "model.rerank.pool"),
    ({"read": {"rerank_v2_search_pool": 8}}, "model.rerank.search_pool"),
    ({"read": {"rerank_max_top_k": 8}}, "model.rerank.max_top_k"),
    ({"read": {"rerank_keep": 8}}, "model.rerank.keep"),
    ({"read": {"rerank_blend": 0.5}}, "model.rerank.blend"),
    ({"read": {"rerank_gate": 0.5}}, "model.rerank.gate"),
    ({"read": {"rerank_context": 1}}, "model.rerank.context"),
    ({"read": {"rerank_date_prefix": True}}, "model.rerank.date_prefix"),
    ({"read": {"rerank_balanced": True}}, "model.rerank.balanced"),
    ({"read": {"rerank_chunk_chars": 512}}, "model.rerank.chunk_chars"),
    ({"read": {"rerank_chunk_overlap": 8}}, "model.rerank.chunk_overlap"),
    ({"read": {"rerank_floor": "skip"}}, "model.rerank.floor"),
    ({"read": {"skip_rerank_for_ordering": True}}, "model.rerank.skip_for_ordering"),
    ({"read": {"decider": "opendecider"}}, "model.decider.read.provider"),
    ({"read": {"decider_model": "x"}}, "model.decider.read.settings.model"),
    ({"read": {"decider_device": "cpu"}}, "model.decider.read.settings.device"),
    ({"read": {"decider_threads": 2}}, "model.decider.read.settings.threads"),
    ({"read": {"decider_backend": "onnx"}}, "model.decider.read.settings.backend"),
    ({"read": {"decider_dtype": "bfloat16"}}, "model.decider.read.settings.dtype"),
    ({"read": {"decider_tasks": []}}, "model.decider.read.tasks"),
    ({"read": {"decider_min_confidence": 0.6}}, "model.decider.read.min_confidence"),
    ({"read": {"decider_workers": 2}}, "model.decider.read.workers"),
    ({"ingest": {"asset_vision": "ollama"}}, "model.vision.provider"),
    ({"ingest": {"asset_vision_model": "x"}}, "model.vision.settings.model"),
    ({"ingest": {"asset_vision_url": "http://x"}}, "model.vision.settings.api_base"),
    ({"ingest": {"asset_vision_timeout_s": 5.0}}, "model.vision.timeout_s"),
]


@pytest.mark.parametrize(("old", "new_location"), _OLD_KEYS)
def test_every_removed_key_is_rejected_naming_its_new_location(
    old: dict[str, Any], new_location: str
) -> None:
    with pytest.raises(ConfigError, match="removed by ADR-082") as raised:
        load_config(template="base", overrides=old)
    assert new_location in str(raised.value)
    with pytest.raises(ConfigError, match="removed by ADR-082"):
        _config(**old)


def test_the_rejection_table_is_fully_covered() -> None:
    covered = set()
    for old, _ in _OLD_KEYS:
        ((area, body),) = old.items()
        covered.add(area)
        covered.update(f"{area}.{key}" for key in body)
    assert set(MOVED_KEYS) <= covered


def test_old_keys_are_rejected_from_the_environment() -> None:
    with pytest.raises(ConfigError, match="model.rerank.keep"):
        load_config(template="base", env={"MEMSPINE_READ__RERANK_KEEP": "5"})
    with pytest.raises(ConfigError, match="model.embedding.provider"):
        load_config(template="base", env={"MEMSPINE_EMBEDDING__PROVIDER": "hash"})


def test_old_warmup_categories_are_rejected() -> None:
    for old in ("embedder", "reranker", "gliner"):
        with pytest.raises(ValidationError):
            SharedResourcesConfig(warmup_include=[old])
    assert "vision" in SharedResourcesConfig().warmup_categories()


def test_llm_slot_settings_timeout_points_at_timeout_s() -> None:
    with pytest.raises(ConfigError, match=r"model.llm.<role>.timeout_s"):
        _config(
            model={
                "llm": {
                    "extract": {
                        "provider": "openai_compatible",
                        "timeout_s": 5.0,
                        "settings": {
                            "api_base": "http://test.invalid/v1",
                            "model": "m",
                            "timeout_seconds": 5.0,
                        },
                    }
                }
            }
        )


# ---------------------------------------------------------------------------- defaults


def test_defaults() -> None:
    model = _config().model
    assert model.embedding.provider == "onnx" and model.embedding.settings == {}
    assert model.embedding.batch_size == 32 and model.embedding.versioned_spaces is False
    assert model.embedding.preset is None
    assert not model.embedding.pin_preset and not model.embedding.migrate_preset
    assert model.llm == {}
    rerank = model.rerank
    assert rerank.chain == [] and rerank.pool == 64 and rerank.search_pool == 0
    assert rerank.max_top_k is None and rerank.keep is None and rerank.blend is None
    assert rerank.gate is None and rerank.context == 0 and rerank.chunk_chars is None
    assert rerank.chunk_overlap == 0 and rerank.floor == "minmax"
    assert not (rerank.date_prefix or rerank.balanced or rerank.skip_for_ordering)
    extraction = model.extraction
    assert (extraction.semantic, extraction.prepass, extraction.topic) == (None, None, None)
    assert extraction.sensitivity is None
    assert model.decider.route is None and model.decider.read is None
    assert model.vision is None
    placement = model.placement
    assert placement.tiers_v2 is False and placement.catalogue == []
    assert placement.catalogue_rerankers == "shipped" and placement.max_loaded_models == 1
    assert placement.gpu_headroom_mb == 512 and placement.on_missing_model == "error"


def test_slot_defaults_match_the_replaced_fields() -> None:
    model = _config(
        model={
            "decider": {"read": {"provider": "opendecider"}},
            "vision": {"provider": "ollama"},
            "extraction": {
                "topic": {**_GLINER, "settings": {"local_files_only": True}},
            },
        }
    ).model
    read = model.decider.read
    assert read is not None
    assert read.tasks == ["list_mode", "bridge_hop"]
    assert read.min_confidence == 0.5 and read.workers == 1
    assert model.vision is not None and model.vision.timeout_s == 120.0
    topic = model.extraction.topic
    assert topic is not None and topic.min_confidence == 0.5 and topic.labels == {}


def test_sample_from_the_adr_validates() -> None:
    config = _config(
        model={
            "embedding": {
                "provider": "onnx",
                "batch_size": 32,
                "versioned_spaces": False,
                "settings": {"model": "BAAI/bge-small-en-v1.5"},
            },
            "llm": {
                "extract": {
                    **_llm(),
                    "structured": {"retry_on_error": True, "constrained_retry": False},
                    "escalation": {
                        "triggers": ["error", "invalid_json"],
                        "max_attempts": 2,
                        "timeout_s": 120.0,
                        "max_tokens_per_attempt": 2048,
                        "targets": [
                            {
                                "provider": "openai_compatible",
                                "timeout_s": 120.0,
                                "settings": {
                                    "api_base": "http://test.invalid/v1",
                                    "model": "ollama/qwen3.5:27b",
                                },
                            }
                        ],
                    },
                },
                "judge": {
                    "provider": "llamacpp",
                    "timeout_s": 60.0,
                    "thinking": 512,
                    "settings": {"model_path": "C:/models/qwen.gguf", "n_ctx": 8192},
                },
            },
            "rerank": {
                "chain": [
                    {"provider": "jina", "settings": {}},
                    {"provider": "onnx", "settings": {}},
                ],
                "pool": 64,
                "floor": "minmax",
            },
            "extraction": {
                "semantic": {**_GLINER, "settings": {"tasks": ["entities"]}},
                "topic": {
                    "provider": "gliner2",
                    "labels": {"cardiology": "health/condition"},
                    "min_confidence": 0.6,
                    "settings": {"tasks": ["classification"]},
                },
            },
            "decider": {
                "route": _GLINER,
                "read": {
                    "provider": "opendecider",
                    "tasks": ["list_mode", "bridge_hop"],
                    "min_confidence": 0.5,
                    "settings": {"model": "manjunathshiva/opendecider-nano", "device": "cpu"},
                },
            },
            "vision": None,
            "placement": {"tiers_v2": False, "max_loaded_models": 1},
        },
        read={"planner": "decision"},
    )
    extract = llm_slot(config, "extract")
    assert extract is not None and extract.structured.retry_on_error
    assert extract.escalation is not None
    assert extract.escalation.targets[0].settings["model"] == "ollama/qwen3.5:27b"
    assert set(llm_slots(config)) == {"extract", "judge"}


# ---------------------------------------------------------------------------- llm rules


def test_unknown_llm_role_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown role 'nope'"):
        _config(model={"llm": {"nope": _llm()}})


def test_catalogue_role_is_accepted() -> None:
    assert llm_slot(_config(model={"llm": {"window_extract": _llm()}}), "window_extract")


def test_thinking_only_for_judge_and_reflect() -> None:
    for role in ("judge", "reflect"):
        slot = llm_slot(_config(model={"llm": {role: _llm(thinking=256)}}), role)
        assert slot is not None and slot.thinking == 256
    with pytest.raises(ConfigError, match=r"model.llm.chat.thinking"):
        _config(model={"llm": {"chat": _llm(thinking=256)}})


def test_placement_thinking_only_for_judge_and_reflect() -> None:
    """RM-118: ``model.placement.thinking`` replaces ``models.thinking`` for plan-picked roles."""
    cfg = _config(model={"placement": {"thinking": {"judge": 512, "reflect": 0}}})
    assert cfg.model.placement.thinking == {"judge": 512, "reflect": 0}
    with pytest.raises(ConfigError, match=r"model.placement.thinking.chat"):
        _config(model={"placement": {"thinking": {"chat": 256}}})
    with pytest.raises(ConfigError, match=r"model.placement.thinking.judge must be"):
        _config(model={"placement": {"thinking": {"judge": -1}}})


def test_placement_thinking_and_an_explicit_slot_are_one_place_per_role() -> None:
    with pytest.raises(ConfigError, match=r"use model.llm.judge.thinking"):
        _config(model={"llm": {"judge": _llm()}, "placement": {"thinking": {"judge": 512}}})
    # Different roles in the two places are fine: judge explicit, reflect planned.
    cfg = _config(
        model={"llm": {"judge": _llm(thinking=128)}, "placement": {"thinking": {"reflect": 512}}}
    )
    judge = llm_slot(cfg, "judge")
    assert judge is not None and judge.thinking == 128


def test_llm_slot_requires_an_explicit_timeout() -> None:
    with pytest.raises(ValidationError, match="timeout_s"):
        _config(
            model={
                "llm": {
                    "chat": {
                        "provider": "openai_compatible",
                        "settings": {"api_base": "http://test.invalid/v1", "model": "m"},
                    }
                }
            }
        )


def _escalation(**policy: Any) -> dict[str, Any]:
    return {
        "timeout_s": 3.0,
        "max_tokens_per_attempt": 128,
        "targets": [
            {
                "provider": "openai_compatible",
                "timeout_s": 9.0,
                "settings": {"api_base": "http://test.invalid/v1", "model": "b"},
            }
        ],
        **policy,
    }


def test_escalation_targets_are_bounded_unique_and_differ_from_the_primary() -> None:
    ok = _config(model={"llm": {"extract": _llm("a", escalation=_escalation())}})
    extract = llm_slot(ok, "extract")
    assert extract is not None and isinstance(extract.escalation, EscalationPolicy)
    target = {
        "provider": "openai_compatible",
        "timeout_s": 9.0,
        "settings": {"api_base": "http://test.invalid/v1", "model": "b"},
    }
    invalid = [
        _escalation(
            targets=[{**target, "settings": {**target["settings"], "model": "a"}}]
        ),  # same endpoint and model as primary
        _escalation(targets=[target, {**target, "timeout_s": 20.0}]),  # duplicate model
        _escalation(max_attempts=3),  # beyond primary + one target
        _escalation(max_attempts=1),  # targets need two attempts
        _escalation(triggers=["error", "error"]),
        _escalation(triggers=[]),
    ]
    for policy in invalid:
        with pytest.raises((ValidationError, ConfigError)):
            _config(model={"llm": {"extract": _llm("a", escalation=policy)}})


def test_escalation_requires_explicit_positive_limits() -> None:
    with pytest.raises(ValidationError):
        EscalationPolicy.model_validate({"targets": []})
    with pytest.raises(ValidationError):
        EscalationPolicy.model_validate({"timeout_s": 0, "max_tokens_per_attempt": 100})
    with pytest.raises(ValidationError):
        EscalationPolicy.model_validate({"timeout_s": 2.0, "max_tokens_per_attempt": 0})
    with pytest.raises(ValidationError):  # the YAML-unsafe 'on' spelling is not an alias
        EscalationPolicy.model_validate(
            {"on": ["error"], "timeout_s": 2.0, "max_tokens_per_attempt": 100}
        )


# ---------------------------------------------------------------------------- rerank


def test_rerank_bounds_and_unique_chain() -> None:
    for bad in (
        {"pool": 0},
        {"pool": 257},
        {"search_pool": 257},
        {"context": 4},
        {"blend": 1.5},
        {"chunk_chars": 10},
        {"floor": "raw"},
        {"chain": [{"provider": "jina"}, {"provider": "jina"}]},
        {"chain": [{"provider": " jina "}]},
    ):
        with pytest.raises(ValidationError):
            _config(model={"rerank": bad})
    chain = _config(model={"rerank": {"chain": [{"provider": "jina"}]}}).model.rerank.chain
    assert chain[0].provider == "jina" and chain[0].settings == {}


def test_search_pool_needs_a_chain() -> None:
    with pytest.raises(ConfigError, match=r"model.rerank.search_pool needs model.rerank.chain"):
        _config(model={"rerank": {"search_pool": 32}})
    _config(model={"rerank": {"search_pool": 32, "chain": [{"provider": "onnx"}]}})


def test_a_layer_replaces_the_chain_whole() -> None:
    merged = load_config(
        template="base",
        user_config=_PRESET_CHAIN,
        overrides={"model": {"rerank": {"chain": [{"provider": "flashrank"}]}}},
    ).config
    assert [s.provider for s in merged.model.rerank.chain] == ["flashrank"]


_PRESET_CHAIN = {
    "model": {
        "rerank": {
            "chain": [
                {"provider": "jina", "settings": {}},
                {"provider": "qwen3", "settings": {}},
                {"provider": "onnx", "settings": {}},
            ]
        }
    }
}


# ---------------------------------------------------------------------------- extraction


def test_extraction_slots_need_their_task() -> None:
    for consumer in ("semantic", "prepass", "sensitivity"):
        slot = {"provider": "gliner2", "settings": {"tasks": ["classification"]}}
        if consumer == "sensitivity":
            slot["settings"]["local_files_only"] = True
        with pytest.raises(ConfigError, match=rf"model.extraction.{consumer}.*'entities'"):
            _config(model={"extraction": {consumer: slot}})
    with pytest.raises(ConfigError, match=r"model.extraction.topic.*'classification'"):
        _config(
            model={
                "extraction": {
                    "topic": {"provider": "gliner2", "settings": {"tasks": ["entities"]}}
                }
            }
        )


@pytest.mark.parametrize("consumer", ["topic", "sensitivity"])
def test_offline_only_consumers_need_local_files_only(consumer: str) -> None:
    with pytest.raises(ConfigError, match="offline only"):
        _config(
            model={
                "extraction": {
                    consumer: {"provider": "gliner2", "settings": {"local_files_only": False}}
                }
            }
        )
    with pytest.raises(ConfigError, match="offline only"):  # another provider must say so
        _config(model={"extraction": {consumer: {"provider": "other", "settings": {}}}})
    config = _config(model={"extraction": {consumer: _GLINER}})  # gliner2 default: local only
    assert extraction_slot(config, consumer) is not None  # type: ignore[arg-type]


def test_active_consumer_without_its_slot_raises_naming_it() -> None:
    with pytest.raises(ConfigError, match=r"write.prepass: gliner needs model.extraction.prepass"):
        _config(write={"prepass": "gliner", "window_extract": "facts"})
    semantic_on = {"semantic": {"enabled": True, "policies": {"entity_extraction": "gliner"}}}
    with pytest.raises(ConfigError, match=r"needs model.extraction.semantic"):
        _config(memories=semantic_on)
    with pytest.raises(ConfigError, match=r"model.extraction.semantic.provider: gliner2"):
        _config(memories=semantic_on, model={"extraction": {"semantic": {"provider": "other"}}})
    _config(memories=semantic_on, model={"extraction": {"semantic": _GLINER}})
    _config(
        write={"prepass": "gliner", "window_extract": "facts"},
        model={"extraction": {"prepass": _GLINER}},
    )


def test_gliner_settings_are_strict() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _config(
            model={"extraction": {"semantic": {"provider": "gliner2", "settings": {"taskz": []}}}}
        )
    with pytest.raises(ValidationError, match="duplicates"):
        GlinerSettings.model_validate({"tasks": ["entities", "entities"]})


def test_topic_labels_must_map_to_domain_ids() -> None:
    slot = {**_GLINER, "labels": {"cardio": "Not A Domain"}}
    with pytest.raises(ValidationError, match="not a domain id"):
        _config(model={"extraction": {"topic": slot}})


# ---------------------------------------------------------------------------- decider


def test_planner_decision_needs_the_route_decider() -> None:
    with pytest.raises(ConfigError, match=r"read.planner: decision needs model.decider.route"):
        _config(read={"planner": "decision"})
    _config(read={"planner": "decision"}, model={"decider": {"route": _GLINER}})


def test_sensitivity_gate_decider_needs_the_read_decider() -> None:
    with pytest.raises(ConfigError, match=r"sensitivity_gate: decider needs model.decider.read"):
        _config(read={"sensitivity_gate": "decider"})
    _config(
        read={"sensitivity_gate": "decider"},
        model={"decider": {"read": {"provider": "opendecider"}}},
    )


def test_route_gliner_needs_classification() -> None:
    with pytest.raises(ConfigError, match=r"model.decider.route.*'classification'"):
        _config(
            model={
                "decider": {"route": {"provider": "gliner2", "settings": {"tasks": ["entities"]}}}
            }
        )


def test_read_decider_bounds_and_strict_opendecider_settings() -> None:
    for bad in (
        {"min_confidence": 0.4},
        {"min_confidence": 1.1},
        {"workers": 0},
        {"tasks": ["sing"]},
        {"tasks": ["list_mode", "list_mode"]},
        {"settings": {"device": "tpu"}},
        {"settings": {"unknown": 1}},
    ):
        with pytest.raises(ValidationError):
            _config(model={"decider": {"read": {"provider": "opendecider", **bad}}})


# ---------------------------------------------------------------------------- vision


def test_vision_slot() -> None:
    vision = _config(
        model={
            "vision": {
                "provider": "ollama",
                "timeout_s": 30.0,
                "settings": {"model": "qwen2.5vl:3b", "api_base": "http://localhost:11434"},
            }
        }
    ).model.vision
    assert vision is not None and vision.settings["model"] == "qwen2.5vl:3b"
    with pytest.raises(ValidationError):
        _config(model={"vision": {"provider": "openai"}})
    with pytest.raises(ValidationError):
        _config(model={"vision": {"provider": "ollama", "timeout_s": 0}})


def test_required_model_slots_cannot_be_null() -> None:
    for slot in ("embedding", "rerank", "extraction", "decider", "placement"):
        with pytest.raises(ConfigError, match=f"model.{slot} cannot be null"):
            _config(model={slot: None})


def test_catalogue_kind_uses_the_slot_vocabulary() -> None:
    placement = _config(
        model={
            "placement": {
                "catalogue": [
                    {"id": "jinaai/jina-reranker-v3.5", "kind": "rerank"},
                    {"id": "BAAI/bge-small-en-v1.5", "kind": "embedding"},
                ]
            }
        }
    ).model.placement
    assert [d.kind for d in placement.catalogue] == ["rerank", "embedding"]
    for old in ("reranker", "embedder"):
        with pytest.raises(ValidationError):
            _config(model={"placement": {"catalogue": [{"id": "x", "kind": old}]}})


# ---------------------------------------------------------------------------- accessors


def test_accessors_return_the_slots_or_none() -> None:
    config = _config(
        model={
            "llm": {"chat": _llm()},
            "extraction": {"semantic": _GLINER},
            "decider": {"route": _GLINER},
        }
    )
    assert llm_slot(config, "chat") is config.model.llm["chat"]
    assert llm_slot(config, "judge") is None
    assert extraction_slot(config, "semantic") is config.model.extraction.semantic
    assert extraction_slot(config, "prepass") is None
    assert decider_slot(config, "route") is config.model.decider.route
    assert decider_slot(config, "read") is None
    copy = llm_slots(config)
    copy.pop("chat")
    assert "chat" in config.model.llm


def test_gliner_settings_accessor() -> None:
    config = _config(
        model={
            "extraction": {"semantic": {**_GLINER, "settings": {"tasks": ["entities"]}}},
            "decider": {"route": _GLINER, "read": {"provider": "opendecider"}},
        }
    )
    assert gliner_settings(config, "semantic").tasks == ("entities",)
    assert "classification" in gliner_settings(config, "route").tasks
    with pytest.raises(ConfigError, match="model.extraction.prepass is not configured"):
        gliner_settings(config, "prepass")
    with pytest.raises(ConfigError, match="not 'gliner2'"):
        gliner_settings(config, "read")


def test_weights_settings_drop_consumer_fields() -> None:
    a = ModelSlot(
        provider="gliner2",
        settings={"model": "fastino/gliner2.5-base-v1", "tasks": ["entities"]},
    )
    b = ModelSlot(
        provider="gliner2",
        settings={"tasks": ["classification"], "inference_concurrency": 4},
    )
    assert weights_settings(a) == weights_settings(b)
    assert "tasks" not in weights_settings(a) and "inference_concurrency" not in weights_settings(a)
    assert weights_settings(a)["local_files_only"] is True  # effective defaults included
    other_device = ModelSlot(provider="gliner2", settings={"device": "cuda"})
    assert weights_settings(other_device) != weights_settings(a)
    assert weights_settings(ModelSlot(provider="opendecider", settings={})) == {
        "model": "manjunathshiva/opendecider-nano",
        "device": "cpu",
        "threads": 0,
        "backend": "torch",
        "dtype": "float32",
    }
    assert weights_settings(ModelSlot(provider="jina", settings={"x": 1})) == {"x": 1}


def test_five_consumers_and_the_route_decider_share_one_weights_key() -> None:
    config = _config(
        model={
            "extraction": {
                "semantic": {**_GLINER, "settings": {"tasks": ["entities"]}},
                "prepass": {**_GLINER, "settings": {"tasks": ["entities", "relations"]}},
                "topic": {**_GLINER, "settings": {"tasks": ["classification"]}},
                "sensitivity": {**_GLINER, "settings": {"inference_concurrency": 2}},
            },
            "decider": {"route": _GLINER},
        }
    )
    slots = [getattr(config.model.extraction, c) for c in ("semantic", "prepass", "topic")]
    slots += [config.model.extraction.sensitivity, config.model.decider.route]
    keys = {tuple(sorted(weights_settings(s).items())) for s in slots}
    assert len(keys) == 1


# ---------------------------------------------------------------------------- templates


def test_benchmark_template_pins_the_embedder_in_the_model_block() -> None:
    config = load_config(template="benchmark").config
    assert config.model.embedding.settings["model"] == "BAAI/bge-small-en-v1.5"
    assert config.model.llm == {} and config.model.rerank.chain == []


def test_read_v2_presets_carry_the_shipped_rerank_chain() -> None:
    from memspine.config import presets

    for name in ("read-v2-core", "read-v2-graph"):
        config = load_config(template="base", overrides=presets.load_preset(name)).config
        assert [s.provider for s in config.model.rerank.chain] == ["jina", "qwen3", "onnx"]


def test_environment_layers_into_the_model_block() -> None:
    config = load_config(
        template="base", env={"MEMSPINE_MODEL__EMBEDDING__PROVIDER": "hash"}
    ).config
    assert config.model.embedding.provider == "hash"
