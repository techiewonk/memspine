"""Tests for scripts/migrate_config_keys_adr082.py (ADR-082 / RM-117 WP4).

One case per row of docs/MODEL_SLOTS_MIGRATION_PLAN.md section 1, then the input forms
(Python kwargs / dict / assignment / attributes / dotted strings / env names, YAML, JSON),
idempotence, the skip marker and the CLI modes.

migrate_config_keys_adr082: skip  (this file spells the old keys on purpose)
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migrate_config_keys_adr082", REPO / "scripts" / "migrate_config_keys_adr082.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def mig(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = copy.deepcopy(data)
    notes = mod.migrate_config(out)
    return out, notes


def py(source: str) -> tuple[str, list[str]]:
    text = textwrap.dedent(source)
    out, notes = mod.rewrite_python(Path("case.py"), text)
    ast.parse(out)  # the rewrite is always valid Python
    return out, notes


def py_value(source: str, name: str = "cfg") -> Any:
    """Rewrite ``source`` and return the literal value bound to ``name``."""
    out, _ = py(source)
    for node in ast.parse(out).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not assigned in:\n{out}")


GLINER = {
    "kind": "gliner",
    "provider": "gliner2",
    "settings": {"model": "g", "tasks": ["entities"]},
}
SLOT_G = {"provider": "gliner2", "settings": {"model": "g", "tasks": ["entities"]}}


# ── map rows ───────────────────────────────────────────────────────────────────────────


def test_embedding_provider_settings_and_space_keys() -> None:
    out, notes = mig(
        {
            "embedding": {
                "provider": "onnx",
                "settings": {"model": "bge"},
                "batch_size": 16,
                "versioned_spaces": True,
            }
        }
    )
    assert out == {
        "model": {
            "embedding": {
                "provider": "onnx",
                "settings": {"model": "bge"},
                "batch_size": 16,
                "versioned_spaces": True,
            }
        }
    }
    assert notes == []


def test_embedding_profile_keys_become_preset() -> None:
    out, _ = mig({"embedding": {"profile": "gpu", "pin_profile": True, "migrate_profile": True}})
    assert out == {
        "model": {"embedding": {"preset": "gpu", "pin_preset": True, "migrate_preset": True}}
    }


@pytest.mark.parametrize(
    ("value", "expected"), [("gpu", {"preset": "gpu"}), ("cpu", {"preset": "cpu"})]
)
def test_models_embedder_profile_folds_into_preset(value: str, expected: dict[str, Any]) -> None:
    out, _ = mig({"models": {"embedder_profile": value}})
    assert out == {"model": {"embedding": expected}}


def test_models_embedder_profile_auto_is_unset() -> None:
    out, _ = mig({"models": {"embedder_profile": "auto"}})
    assert out == {}


def test_embedding_profile_wins_over_embedder_profile() -> None:
    out, notes = mig({"embedding": {"profile": "gpu"}, "models": {"embedder_profile": "cpu"}})
    assert out == {"model": {"embedding": {"preset": "gpu"}}}
    assert any("embedder_profile" in n for n in notes)


def test_llm_roles_become_model_llm() -> None:
    role = {"provider": "litellm", "settings": {"model": "ollama/q"}}
    out, _ = mig({"llm": {"roles": {"extract": role, "judge": role}}})
    assert out == {"model": {"llm": {"extract": role, "judge": role}}}


def test_llm_binding_and_profile_expand_inline() -> None:
    out, notes = mig(
        {
            "models": {
                "profiles": {
                    "small": {
                        "kind": "llm",
                        "provider": "litellm",
                        "settings": {"m": 1, "timeout_seconds": 30.0},
                    }
                },
                "bindings": {"chat": "small"},
            }
        }
    )
    assert out == {
        "model": {"llm": {"chat": {"provider": "litellm", "timeout_s": 30.0, "settings": {"m": 1}}}}
    }
    assert notes == []


def test_llm_settings_timeout_seconds_becomes_slot_timeout_s() -> None:
    out, notes = mig(
        {
            "llm": {
                "roles": {
                    "extract": {
                        "provider": "litellm",
                        "settings": {"model": "ollama/q", "timeout_seconds": 60.0},
                    }
                }
            }
        }
    )
    slot = out["model"]["llm"]["extract"]
    assert slot == {"provider": "litellm", "timeout_s": 60.0, "settings": {"model": "ollama/q"}}
    assert list(slot) == ["provider", "timeout_s", "settings"]
    assert notes == []
    again = copy.deepcopy(out)
    assert mod.migrate_config(again) == [] and again == out


def test_llm_slot_without_timeout_is_reported() -> None:
    out, notes = mig({"llm": {"roles": {"chat": {"provider": "fake"}}}})
    assert out == {"model": {"llm": {"chat": {"provider": "fake"}}}}
    assert any("model.llm.chat: no settings.timeout_seconds" in n for n in notes)


def test_python_llm_timeout_is_hoisted() -> None:
    value = py_value(
        """
        cfg = {"llm": {"roles": {"extract": {"provider": "litellm",
            "settings": {"model": "m", "timeout_seconds": 5.0}}}}}
        """
    )
    assert value == {
        "model": {
            "llm": {
                "extract": {"provider": "litellm", "timeout_s": 5.0, "settings": {"model": "m"}}
            }
        }
    }


def test_llm_structured_copied_into_every_role() -> None:
    structured = {"retry_on_error": True, "constrained_retry": False}
    out, _ = mig(
        {
            "llm": {"roles": {"extract": {"provider": "a"}}, "structured": structured},
            "models": {
                "profiles": {"p": {"kind": "llm", "provider": "b"}},
                "bindings": {"judge": "p"},
            },
        }
    )
    roles = out["model"]["llm"]
    assert roles["extract"] == {"provider": "a", "structured": structured}
    assert roles["judge"] == {"provider": "b", "structured": structured}
    assert "llm" not in out


def test_llm_structured_without_roles_is_left_and_reported() -> None:
    out, notes = mig({"llm": {"structured": {"retry_on_error": True}}})
    assert out == {"llm": {"structured": {"retry_on_error": True}}}
    assert any("llm.structured" in n for n in notes)


def test_models_thinking_moves_into_the_role() -> None:
    out, _ = mig(
        {
            "llm": {"roles": {"judge": {"provider": "x"}}},
            "models": {"thinking": {"judge": 512}},
        }
    )
    assert out == {"model": {"llm": {"judge": {"provider": "x", "thinking": 512}}}}


def test_models_thinking_for_a_plan_picked_role_moves_into_placement() -> None:
    """RM-118: a role with no explicit slot in the config is picked by the tier plan."""
    out, _ = mig(
        {
            "llm": {"roles": {"judge": {"provider": "x"}}},
            "models": {"tiers_v2": True, "thinking": {"judge": 512, "reflect": 900}},
        }
    )
    assert out == {
        "model": {
            "llm": {"judge": {"provider": "x", "thinking": 512}},
            "placement": {"tiers_v2": True, "thinking": {"reflect": 900}},
        }
    }
    again, _ = mig(copy.deepcopy(out))
    assert again == out  # a second run is a no-op


def test_models_thinking_follows_a_binding_expanded_in_the_same_config() -> None:
    out, _ = mig(
        {
            "models": {
                "profiles": {"p": {"kind": "llm", "provider": "b"}},
                "bindings": {"reflect": "p"},
                "thinking": {"reflect": 300},
            }
        }
    )
    assert out == {"model": {"llm": {"reflect": {"provider": "b", "thinking": 300}}}}


def test_migrate_env_thinking_goes_to_placement() -> None:
    new, note = mod.migrate_env("MEMSPINE_MODELS__THINKING__JUDGE")
    assert new == "MEMSPINE_MODEL__PLACEMENT__THINKING__JUDGE"
    assert note is not None and "model.llm.judge.thinking" in note


def test_escalation_targets_expand_to_inline_slots() -> None:
    out, notes = mig(
        {
            "models": {
                "profiles": {
                    "small": {
                        "kind": "llm",
                        "provider": "litellm",
                        "settings": {"model": "9b", "timeout_seconds": 60.0},
                    },
                    "big": {
                        "kind": "llm",
                        "provider": "litellm",
                        "settings": {"model": "27b", "timeout_seconds": 120.0},
                    },
                },
                "bindings": {"extract": "small"},
                "escalation": {
                    "extract": {
                        "targets": ["big"],
                        "triggers": ["error", "invalid_json"],
                        "max_attempts": 2,
                        "timeout_s": 120,
                        "max_tokens_per_attempt": 2048,
                    }
                },
            }
        }
    )
    assert notes == []
    assert out == {
        "model": {
            "llm": {
                "extract": {
                    "provider": "litellm",
                    "timeout_s": 60.0,
                    "settings": {"model": "9b"},
                    "escalation": {
                        "targets": [
                            {
                                "provider": "litellm",
                                "timeout_s": 120.0,
                                "settings": {"model": "27b"},
                            }
                        ],
                        "triggers": ["error", "invalid_json"],
                        "max_attempts": 2,
                        "timeout_s": 120,
                        "max_tokens_per_attempt": 2048,
                    },
                }
            }
        }
    }


def test_binding_to_unknown_profile_is_left_and_reported() -> None:
    data = {"models": {"bindings": {"chat": "elsewhere"}}}
    out, notes = mig(data)
    assert out == data
    assert any("'elsewhere' is not defined" in n for n in notes)


def test_profiles_without_any_binding_here_are_left() -> None:
    data = {"models": {"profiles": {"g": GLINER}}}
    out, notes = mig(data)
    assert out == data
    assert any("no binding" in n for n in notes)


def test_unreferenced_profile_beside_bindings_is_dropped() -> None:
    out, notes = mig(
        {
            "models": {
                "profiles": {"g": GLINER, "spare": GLINER},
                "bindings": {"gliner_prepass": "g"},
            }
        }
    )
    assert out == {"model": {"extraction": {"prepass": SLOT_G}}}
    assert any("spare" in n for n in notes)


def test_python_non_literal_old_root_is_reported() -> None:
    _, notes = py('cfg = _config(embedding={"provider": "hash"}, models=models)\n')
    assert any("models is not a literal" in n for n in notes)


def test_catalogue_kinds_renamed_and_timeout_unchanged() -> None:
    out, _ = mig(
        {
            "models": {
                "catalogue": [
                    {"id": "q", "kind": "llm", "provider": "litellm", "timeout_seconds": 30.0},
                    {"id": "r", "kind": "reranker", "family": "jina"},
                    {"id": "e", "kind": "embedder", "profile": "gpu"},
                ]
            }
        }
    )
    assert out == {
        "model": {
            "placement": {
                "catalogue": [
                    {"id": "q", "kind": "llm", "provider": "litellm", "timeout_seconds": 30.0},
                    {"id": "r", "kind": "rerank", "family": "jina"},
                    {"id": "e", "kind": "embedding", "profile": "gpu"},
                ]
            }
        }
    }


@pytest.mark.parametrize(
    ("role", "consumer"),
    [
        ("gliner_semantic", "semantic"),
        ("gliner_prepass", "prepass"),
        ("gliner_sensitivity", "sensitivity"),
    ],
)
def test_gliner_bindings_become_extraction_slots(role: str, consumer: str) -> None:
    out, _ = mig({"models": {"profiles": {"g": GLINER}, "bindings": {role: "g"}}})
    assert out == {"model": {"extraction": {consumer: SLOT_G}}}


def test_gliner_topic_merges_topic_config() -> None:
    out, _ = mig(
        {
            "models": {
                "profiles": {"g": GLINER},
                "bindings": {"gliner_topic": "g"},
                "topic": {"labels": {"cardiology": "health/condition"}, "min_confidence": 0.6},
            }
        }
    )
    assert out == {
        "model": {
            "extraction": {
                "topic": {
                    **SLOT_G,
                    "labels": {"cardiology": "health/condition"},
                    "min_confidence": 0.6,
                }
            }
        }
    }


def test_topic_without_binding_is_dropped_with_a_note() -> None:
    out, notes = mig({"models": {"topic": {"min_confidence": 0.7}}})
    assert out == {}
    assert any("models.topic" in n for n in notes)


def test_five_gliner_consumers_share_one_profile() -> None:
    bindings = {r: "g" for r in mod.GLINER_TARGET}
    out, _ = mig(
        {
            "decision": {"provider": "gliner2"},
            "models": {"profiles": {"g": GLINER}, "bindings": bindings},
        }
    )
    model = out["model"]
    assert set(model["extraction"]) == {"semantic", "prepass", "topic", "sensitivity"}
    assert model["decider"] == {"route": SLOT_G}
    assert "decision" not in out and "models" not in out


def test_gliner_max_models_becomes_max_loaded_models() -> None:
    out, _ = mig({"models": {"gliner_max_models": 2}})
    assert out == {"model": {"placement": {"max_loaded_models": 2}}}


def test_decision_gliner2_with_binding_becomes_route() -> None:
    out, _ = mig(
        {
            "decision": {"provider": "gliner2"},
            "models": {"profiles": {"g": GLINER}, "bindings": {"gliner_decision": "g"}},
        }
    )
    assert out == {"model": {"decider": {"route": SLOT_G}}}


def test_decision_off_means_no_route_slot() -> None:
    out, notes = mig(
        {
            "decision": {"provider": "off"},
            "models": {"profiles": {"g": GLINER}, "bindings": {"gliner_decision": "g"}},
        }
    )
    assert out == {}
    assert any("inert" in n for n in notes)


def test_decision_binding_without_decision_provider_here_is_left() -> None:
    """A helper's ``models=`` kwarg may bind gliner_decision while decision.provider is set
    by the helper itself: absent is not off, so the binding is kept and reported."""
    data = {"models": {"profiles": {"g": GLINER}, "bindings": {"gliner_decision": "g"}}}
    out, notes = mig(data)
    assert out == data
    assert any("decision.provider is not in this config" in n for n in notes)


def test_read_decider_opendecider_becomes_slot() -> None:
    out, _ = mig(
        {
            "read": {
                "decider": "opendecider",
                "decider_model": "m/nano",
                "decider_device": "cuda",
                "decider_threads": 4,
                "decider_backend": "onnx",
                "decider_dtype": "bfloat16",
                "decider_tasks": ["list_mode"],
                "decider_min_confidence": 0.7,
                "decider_workers": 2,
                "planner": "decision",
            }
        }
    )
    assert out == {
        "read": {"planner": "decision"},
        "model": {
            "decider": {
                "read": {
                    "provider": "opendecider",
                    "tasks": ["list_mode"],
                    "min_confidence": 0.7,
                    "workers": 2,
                    "settings": {
                        "model": "m/nano",
                        "device": "cuda",
                        "threads": 4,
                        "backend": "onnx",
                        "dtype": "bfloat16",
                    },
                }
            }
        },
    }


def test_read_decider_opendecider_keeps_the_old_default_model() -> None:
    out, _ = mig({"read": {"decider": "opendecider"}})
    assert out["model"]["decider"]["read"]["settings"] == {"model": mod.DECIDER_DEFAULT_MODEL}


def test_read_decider_heuristic_drops_every_decider_key() -> None:
    out, _ = mig({"read": {"decider": "heuristic", "decider_model": "x", "decider_workers": 3}})
    assert out == {}


def test_decider_keys_without_selector_are_left() -> None:
    data = {"read": {"decider_workers": 3}}
    out, notes = mig(data)
    assert out == data
    assert notes


def test_read_rerank_with_settings_becomes_chain() -> None:
    out, _ = mig({"read": {"rerank": "litellm", "rerank_settings": {"model": "cohere"}}})
    assert out == {
        "model": {"rerank": {"chain": [{"provider": "litellm", "settings": {"model": "cohere"}}]}}
    }


def test_read_rerank_off_is_an_empty_chain() -> None:
    out, _ = mig({"read": {"rerank": "off"}})
    assert out == {"model": {"rerank": {"chain": []}}}


def test_rerank_v2_chain_names_become_providers() -> None:
    out, _ = mig({"read": {"rerank_v2": True, "rerank_v2_chain": ["qwen3", "onnx"]}})
    assert out == {
        "model": {
            "rerank": {
                "chain": [
                    {"provider": "qwen3", "settings": {}},
                    {"provider": "onnx", "settings": {}},
                ]
            }
        }
    }


def test_rerank_v2_without_chain_uses_the_old_default() -> None:
    out, _ = mig({"read": {"rerank_v2": True}})
    names = [e["provider"] for e in out["model"]["rerank"]["chain"]]
    assert names == ["jina", "qwen3", "onnx"]


def test_rerank_v2_wins_over_v1_and_the_conflict_is_reported() -> None:
    out, notes = mig(
        {"read": {"rerank": "onnx", "rerank_settings": {"a": 1}, "rerank_v2": True}}
    )
    assert [e["provider"] for e in out["model"]["rerank"]["chain"]] == [
        "jina",
        "qwen3",
        "onnx",
    ]
    assert "read" not in out
    assert any("rerank_v2 wins" in n for n in notes)


def test_rerank_v2_false_keeps_v1() -> None:
    out, _ = mig({"read": {"rerank": "onnx", "rerank_v2": False, "rerank_v2_chain": ["x"]}})
    assert out == {"model": {"rerank": {"chain": [{"provider": "onnx", "settings": {}}]}}}


def test_rerank_pools_move() -> None:
    out, _ = mig({"read": {"rerank_v2_pool": 32, "rerank_v2_search_pool": 30}})
    assert out == {"model": {"rerank": {"pool": 32, "search_pool": 30}}}


@pytest.mark.parametrize("suffix", mod.RERANK_SUFFIXES)
def test_rerank_prefixed_keys_lose_the_prefix(suffix: str) -> None:
    out, _ = mig({"read": {f"rerank_{suffix}": 1}})
    assert out == {"model": {"rerank": {suffix: 1}}}


def test_skip_rerank_for_ordering() -> None:
    out, _ = mig({"read": {"skip_rerank_for_ordering": True}})
    assert out == {"model": {"rerank": {"skip_for_ordering": True}}}


def test_asset_vision_ollama_becomes_vision_slot() -> None:
    out, _ = mig(
        {
            "ingest": {
                "assets": "on",
                "asset_vision": "ollama",
                "asset_vision_model": "llava",
                "asset_vision_url": "http://h:1",
                "asset_vision_timeout_s": 30.0,
            }
        }
    )
    assert out == {
        "ingest": {"assets": "on"},
        "model": {
            "vision": {
                "provider": "ollama",
                "timeout_s": 30.0,
                "settings": {"model": "llava", "api_base": "http://h:1"},
            }
        },
    }


def test_asset_vision_none_is_dropped() -> None:
    out, _ = mig({"ingest": {"asset_vision": "none", "asset_vision_model": "x"}})
    assert out == {}


@pytest.mark.parametrize("key", mod.PLACEMENT_KEYS)
def test_planning_keys_move_to_placement(key: str) -> None:
    out, _ = mig({"models": {key: 1}})
    assert out == {"model": {"placement": {key: 1}}}


def test_default_rerankers_become_catalogue_rerankers() -> None:
    out, _ = mig({"models": {"default_rerankers": "none"}})
    assert out == {"model": {"placement": {"catalogue_rerankers": "none"}}}


def test_models_rerank_is_removed() -> None:
    out, _ = mig({"models": {"rerank": "off"}})
    assert out == {}


def test_empty_old_roots_are_removed() -> None:
    out, _ = mig(
        {
            "embedding": {"provider": "hash"},
            "llm": {"roles": {}},
            "models": {"tiers_v2": True},
            "decision": {"provider": "off"},
            "strict_services": False,
        }
    )
    assert out == {
        "strict_services": False,
        "model": {"embedding": {"provider": "hash"}, "placement": {"tiers_v2": True}},
    }


def test_warmup_categories_renamed() -> None:
    out, _ = mig(
        {
            "runtime": {
                "shared_resources": {
                    "warmup_include": ["embedder", "reranker", "gliner", "llm"],
                    "warmup_exclude": ["lexical"],
                }
            }
        }
    )
    assert out["runtime"]["shared_resources"] == {
        "warmup_include": ["embedding", "rerank", "extraction", "llm"],
        "warmup_exclude": ["lexical"],
    }


def test_unknown_keys_are_left_in_place_and_reported() -> None:
    out, notes = mig(
        {"embedding": {"provider": "hash", "dim": 3}, "models": {"planning": {"x": 1}}}
    )
    assert out == {
        "embedding": {"dim": 3},
        "models": {"planning": {"x": 1}},
        "model": {"embedding": {"provider": "hash"}},
    }
    assert any("embedding.dim" in n for n in notes)
    assert any("models.planning" in n for n in notes)


def test_existing_model_block_merges_and_conflicts_are_reported() -> None:
    out, notes = mig(
        {
            "model": {"rerank": {"chain": []}},
            "read": {"rerank": "onnx", "rerank_keep": 4},
        }
    )
    assert out["model"]["rerank"] == {"chain": [], "keep": 4}
    assert out["read"] == {"rerank": "onnx"}
    assert any("conflict" in n for n in notes)


def test_stays_where_it_is() -> None:
    data = {
        "read": {"planner": "decision", "relevance_filter": "on", "sensitivity_gate": "decider"},
        "write": {"prepass": "gliner"},
        "runtime": {"model_concurrency": 2},
    }
    out, notes = mig(data)
    assert out == data
    assert notes == []


# ── Python forms ───────────────────────────────────────────────────────────────────────


def test_python_kwargs_keep_untouched_entries_and_comments() -> None:
    out, _ = py(
        """
        eng = Engine(
            template="core",
            # extraction role
            llm={"roles": {"extract": {"provider": "litellm"}}},
            read={"top_k": 5, "rerank": "onnx"},  # keep me
            strict_services=False,
        )
        """
    )
    assert "# extraction role" in out
    assert "# keep me" in out
    assert 'template="core",' in out
    assert 'read={"top_k": 5}' in out
    call = ast.parse(out).body[0].value  # type: ignore[attr-defined]
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg != "template"}
    assert kwargs["model"] == {
        "llm": {"extract": {"provider": "litellm"}},
        "rerank": {"chain": [{"provider": "onnx", "settings": {}}]},
    }
    assert "llm" not in kwargs


def test_python_dict_literal() -> None:
    value = py_value(
        """
        cfg = {
            "models": {"profiles": {"g": GLINER_JSON}, "bindings": {"gliner_prepass": "g"}},
            "write": {"prepass": "gliner"},
        }
        """.replace("GLINER_JSON", json.dumps(GLINER))
    )
    assert value == {
        "model": {"extraction": {"prepass": SLOT_G}},
        "write": {"prepass": "gliner"},
    }


def test_python_non_literal_values_keep_their_source() -> None:
    out, _ = py(
        """
        cfg = {"llm": {"roles": {"extract": role_cfg}}, "read": {"rerank_blend": blend}}
        """
    )
    assert '"llm": {"extract": role_cfg}' in out
    assert '"blend": blend' in out


def test_python_report_dict_is_not_a_config() -> None:
    source = 'row = {"embedding": {"provider": "x", "latency_ms": 3}, "llm": "chat"}\n'
    out, _ = py(source)
    assert out == source


def test_python_item_assignment_and_setdefault_chain() -> None:
    out, _ = py(
        """
        over["read"]["rerank_blend"] = 0.4
        over.setdefault("llm", {})["roles"] = {"chat": {"provider": "inline"}}
        over["ingest"] = {"asset_vision": "none", "assets": "on"}
        """
    )
    assert 'over.setdefault("model", {}).setdefault("rerank", {})["blend"] = 0.4' in out
    assert (
        'over.setdefault("model", {}).setdefault("llm", {})["chat"] = {"provider": "inline"}' in out
    )
    assert 'over["ingest"] = {"assets": "on"}' in out


def test_python_attribute_paths() -> None:
    out, notes = py(
        """
        a = cfg.read.rerank_blend, cfg.read.rerank_v2_pool, cfg.read.skip_rerank_for_ordering
        b = cfg.embedding.provider, cfg.embedding.profile, cfg.llm.roles["chat"]
        c = cfg.models.tiers_v2, cfg.models.gliner_max_models, cfg.read.decider_tasks
        d = cfg.read.rerank
        """
    )
    assert (
        "cfg.model.rerank.blend, cfg.model.rerank.pool, cfg.model.rerank.skip_for_ordering" in out
    )
    assert 'cfg.model.embedding.provider, cfg.model.embedding.preset, cfg.model.llm["chat"]' in out
    assert (
        "cfg.model.placement.tiers_v2, cfg.model.placement.max_loaded_models, "
        "cfg.model.decider.read.tasks" in out
    )
    assert "d = cfg.read.rerank" in out
    assert any(".read.rerank changes shape" in n for n in notes)


def test_python_dotted_strings_but_not_event_names() -> None:
    out, _ = py(
        """
        keys = ["read.rerank_keep", "llm.roles.extract.provider", "models.gliner_max_models"]
        tap.event("read.rerank_v2_search_pool", pool=3)
        with tap.span("read.rerank"):
            pass
        names = ("read.rerank.components", "models.resolve")
        """
    )
    assert (
        '["model.rerank.keep", "model.llm.extract.provider", "model.placement.max_loaded_models"]'
        in out
    )
    assert 'tap.event("read.rerank_v2_search_pool"' in out
    assert 'tap.span("read.rerank")' in out
    assert '("read.rerank.components", "models.resolve")' in out


def test_python_dotted_embedding_string_is_rewritten_once() -> None:
    out, _ = py(
        """
        a = sources["embedding.settings.model"], sources["embedding.provider"]
        b = cfg.embedding.settings
        """
    )
    assert 'sources["model.embedding.settings.model"], sources["model.embedding.provider"]' in out
    assert "b = cfg.model.embedding.settings" in out
    assert "model.model." not in out


def test_python_env_names() -> None:
    out, _ = py(
        """
        env = {
            "MEMSPINE_READ__RERANK_BLEND": "0.3",
            "MEMSPINE_EMBEDDING__SETTINGS__region": "a",
            "MEMSPINE_LLM__ROLES__EXTRACT__PROVIDER": "litellm",
            "MEMSPINE_READ__CANDIDATE_POOL": "4",
        }
        """
    )
    assert "MEMSPINE_MODEL__RERANK__BLEND" in out
    assert "MEMSPINE_MODEL__EMBEDDING__SETTINGS__region" in out
    assert "MEMSPINE_MODEL__LLM__EXTRACT__PROVIDER" in out
    assert "MEMSPINE_READ__CANDIDATE_POOL" in out


def test_python_warmup_and_model_decl_kinds() -> None:
    out, _ = py(
        """
        a = SharedResourcesConfig(warmup_include=["embedder", 'reranker'], warmup_exclude=("gliner",))
        b = ModelDecl(id="r", kind="reranker")
        """
    )
    assert 'warmup_include=["embedding", \'rerank\'], warmup_exclude=("extraction",)' in out
    assert 'ModelDecl(id="r", kind="rerank")' in out


def test_python_removed_config_classes_are_reported() -> None:
    _, notes = py('x = LLMConfig(roles={})\ny = ReadConfig(rerank="onnx")\n')
    assert any("LLMConfig" in n for n in notes)
    assert any("ReadConfig" in n for n in notes)


def test_python_yaml_in_string_is_reported() -> None:
    _, notes = py('TEXT = """\nllm:\n  roles: {}\n"""\n')
    assert any("YAML in a string" in n for n in notes)


def test_python_single_quote_style_is_kept() -> None:
    out, _ = py("cfg = {'read': {'rerank_keep': 3}}\n")
    assert out == "cfg = {'model': {'rerank': {'keep': 3}}}\n"


# ── dotted keys and env names (functions) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("embedding.settings.model", "model.embedding.settings.model"),
        ("embedding.profile", "model.embedding.preset"),
        ("models.embedder_profile", "model.embedding.preset"),
        ("llm.roles.extract", "model.llm.extract"),
        ("models.thinking.judge", "model.placement.thinking.judge"),
        ("models.escalation.extract.max_attempts", "model.llm.extract.escalation.max_attempts"),
        ("models.bindings.gliner_semantic", "model.extraction.semantic"),
        ("models.bindings.gliner_decision", "model.decider.route"),
        ("models.bindings.chat", "model.llm.chat"),
        ("models.topic.labels", "model.extraction.topic.labels"),
        ("models.gliner_max_models", "model.placement.max_loaded_models"),
        ("models.default_rerankers", "model.placement.catalogue_rerankers"),
        ("decision.provider", "model.decider.route.provider"),
        ("read.decider", "model.decider.read.provider"),
        ("read.decider_model", "model.decider.read.settings.model"),
        ("read.decider_tasks", "model.decider.read.tasks"),
        ("read.rerank", "model.rerank.chain"),
        ("read.rerank_v2_chain", "model.rerank.chain"),
        ("read.rerank_v2_pool", "model.rerank.pool"),
        ("read.rerank_floor", "model.rerank.floor"),
        ("read.skip_rerank_for_ordering", "model.rerank.skip_for_ordering"),
        ("ingest.asset_vision_url", "model.vision.settings.api_base"),
        ("ingest.asset_vision_timeout_s", "model.vision.timeout_s"),
    ],
)
def test_migrate_dotted(old: str, new: str) -> None:
    assert mod.migrate_dotted(old)[0] == new
    assert mod.migrate_dotted(new)[0] is None  # idempotent


@pytest.mark.parametrize(
    "key", ["read.rerank.components", "read.decider_failed", "models.resolve", "read.top_k"]
)
def test_migrate_dotted_leaves_other_names(key: str) -> None:
    assert mod.migrate_dotted(key)[0] is None


def test_migrate_dotted_removed_keys_carry_a_note() -> None:
    new, note = mod.migrate_dotted("llm.structured")
    assert new is None and note


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("MEMSPINE_READ__RERANK_BLEND", "MEMSPINE_MODEL__RERANK__BLEND"),
        ("MEMSPINE_EMBEDDING__PROVIDER", "MEMSPINE_MODEL__EMBEDDING__PROVIDER"),
        ("MEMSPINE_MODELS__TIERS_V2", "MEMSPINE_MODEL__PLACEMENT__TIERS_V2"),
        ("MEMSPINE_INGEST__ASSET_VISION_MODEL", "MEMSPINE_MODEL__VISION__SETTINGS__MODEL"),
    ],
)
def test_migrate_env(old: str, new: str) -> None:
    assert mod.migrate_env(old)[0] == new
    assert mod.migrate_env(new)[0] is None


# ── YAML / JSON / .env files ───────────────────────────────────────────────────────────


YAML_TEXT = """\
# deployment config
llm:
  roles:
    extract: {provider: litellm, settings: {model: ollama/q}}
  structured: {retry_on_error: true}
read:
  planner: decision   # stays
  rerank: onnx
strict_services: false
"""


def test_yaml_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(YAML_TEXT, encoding="utf-8")
    out, notes = mod.rewrite_file(path)
    assert out is not None
    assert yaml.safe_load(out) == {
        "read": {"planner": "decision"},
        "strict_services": False,
        "model": {
            "llm": {
                "extract": {
                    "provider": "litellm",
                    "settings": {"model": "ollama/q"},
                    "structured": {"retry_on_error": True},
                }
            },
            "rerank": {"chain": [{"provider": "onnx", "settings": {}}]},
        },
    }
    # PyYAML drops comments, as the ADR-081 codemod did: the file is listed for review.
    assert any("comments dropped" in n for n in notes)


def test_yaml_without_old_keys_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("# note\nread:\n  planner: decision\n", encoding="utf-8")
    assert mod.rewrite_file(path) == (None, [])


def test_json_keeps_indent(tmp_path: Path) -> None:
    path = tmp_path / "arm.json"
    path.write_text(json.dumps({"read": {"rerank_keep": 3}}, indent=2) + "\n", encoding="utf-8")
    out, _ = mod.rewrite_file(path)
    assert out == json.dumps({"model": {"rerank": {"keep": 3}}}, indent=2) + "\n"


def test_env_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# x\nMEMSPINE_READ__RERANK=onnx\nOTHER=1\n", encoding="utf-8")
    out, _ = mod.rewrite_file(path)
    assert out == "# x\nMEMSPINE_MODEL__RERANK__CHAIN=onnx\nOTHER=1\n"


# ── idempotence, skip marker, CLI ──────────────────────────────────────────────────────

PY_CASE = """\
eng = Engine(
    template="core",
    llm={"roles": {"extract": {"provider": "litellm"}}, "structured": {"retry_on_error": True}},
    models={"profiles": {"g": GLINER_JSON}, "bindings": {"gliner_semantic": "g"}, "tiers_v2": True},
    read={"top_k": 5, "rerank_v2": True, "rerank_blend": 0.5, "decider": "heuristic"},
    ingest={"asset_vision": "ollama"},
    runtime={"shared_resources": {"warmup_include": ["embedder"]}},
    env={"MEMSPINE_READ__RERANK_KEEP": "2"},
)
x = cfg.read.rerank_keep, "read.rerank_keep"
""".replace("GLINER_JSON", json.dumps(GLINER))


def test_second_run_is_a_no_op_python(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(PY_CASE, encoding="utf-8")
    first, _ = mod.rewrite_file(path)
    assert first is not None
    path.write_text(first, encoding="utf-8")
    assert mod.rewrite_file(path) == (None, [])


def test_migrated_model_block_is_not_migrated_again() -> None:
    """The new ``model`` value holds ``embedding`` / ``llm`` slot keys; a second run must not
    read it as an old root and nest it as ``model={"model": ...}``."""
    first, _ = py('eng = Engine(embedding={"provider": "hash", "settings": {}}, read={"x": 1})\n')
    assert 'model={"embedding": {"provider": "hash", "settings": {}}}' in first
    second, notes = py(first)
    assert second == first and notes == []
    nested, _ = py('cfg = {"model": {"embedding": {"provider": "hash"}, "llm": {}}}\n')
    assert nested == 'cfg = {"model": {"embedding": {"provider": "hash"}, "llm": {}}}\n'
    kept = (
        'model: dict = {"embedding": {"provider": "hash"}}\n'
        'cfg["model"] = {"embedding": {"provider": "hash"}, **extra}\n'
        'cfg.setdefault("model", {}).setdefault("embedding", {})["provider"] = "hash"\n'
    )
    assert py(kept) == (kept, [])


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_second_run_is_a_no_op_data(tmp_path: Path, suffix: str) -> None:
    data = yaml.safe_load(YAML_TEXT)
    path = tmp_path / f"c{suffix}"
    path.write_text(json.dumps(data) if suffix == ".json" else YAML_TEXT, encoding="utf-8")
    first, _ = mod.rewrite_file(path)
    assert first is not None
    path.write_text(first, encoding="utf-8")
    assert mod.rewrite_file(path)[0] is None


def test_python_case_migrates_fully() -> None:
    out, _ = py(PY_CASE)
    call = ast.parse(out).body[0].value  # type: ignore[attr-defined]
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    assert set(kwargs) == {"template", "model", "read", "runtime", "env"}
    model = kwargs["model"]
    assert list(model) == ["llm", "rerank", "extraction", "vision", "placement"]
    assert model["llm"]["extract"]["structured"] == {"retry_on_error": True}
    assert model["vision"]["settings"] == {
        "model": mod.VISION_DEFAULT_MODEL,
        "api_base": mod.VISION_DEFAULT_URL,
    }
    assert kwargs["read"] == {"top_k": 5}
    assert kwargs["runtime"] == {"shared_resources": {"warmup_include": ["embedding"]}}
    assert kwargs["env"] == {"MEMSPINE_MODEL__RERANK__KEEP": "2"}
    assert 'x = cfg.model.rerank.keep, "model.rerank.keep"' in out


def test_skip_marker(tmp_path: Path) -> None:
    path = tmp_path / "case.py"
    path.write_text(f"# {mod.SKIP_MARKER}\n" + PY_CASE, encoding="utf-8")
    assert mod.rewrite_file(path) == (None, [])


def test_cli_check_dry_run_and_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "c.py"
    path.write_text('cfg = {"read": {"rerank_keep": 3}}\n', encoding="utf-8")
    assert mod.main(["--check", str(tmp_path)]) == 1
    assert mod.main(["--dry-run", str(tmp_path)]) == 0
    assert '+cfg = {"model": {"rerank": {"keep": 3}}}' in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == 'cfg = {"read": {"rerank_keep": 3}}\n'
    assert mod.main([str(tmp_path)]) == 0
    assert path.read_text(encoding="utf-8") == 'cfg = {"model": {"rerank": {"keep": 3}}}\n'
    assert mod.main(["--check", str(tmp_path)]) == 0


def test_cli_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert mod.main(["--key", "read.rerank_blend", "--key", "MEMSPINE_LLM__ROLES"]) == 0
    out = capsys.readouterr().out
    assert "read.rerank_blend -> model.rerank.blend" in out
    assert "MEMSPINE_LLM__ROLES -> MEMSPINE_MODEL__LLM" in out
