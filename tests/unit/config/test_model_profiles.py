"""Strict schema and pure resolution tests for named model profiles."""

from __future__ import annotations

import pytest

from memspine.config.schema import EscalationPolicy, MemspineConfig, ModelProfile
from memspine.exceptions import ConfigError
from memspine.models.profiles import (
    bound_llm_roles,
    resolve_gliner_settings,
    resolve_llm_profile,
)


def _config(raw: dict | None = None) -> MemspineConfig:
    data = raw or {}
    data.setdefault(
        "runtime",
        {
            "provider_services": {
                "health_timeout_s": 5.0,
                "close_timeout_s": 10.0,
                "startup_timeout_s": None,
            }
        },
    )
    return MemspineConfig.model_validate(data)


def _llm_profile(provider: str = "litellm", **settings: object) -> dict:
    return {"kind": "llm", "provider": provider, "settings": settings}


def test_default_profile_bindings_and_gliner_resolution() -> None:
    config = _config()
    assert config.models.profiles == {}
    assert config.models.bindings == {}
    assert config.models.gliner_max_models == 1

    configured = _config(
        {
            "models": {
                "profiles": {"gliner-default": {"kind": "gliner", "provider": "gliner2"}},
                "bindings": {
                    "gliner_decision": "gliner-default",
                    "gliner_semantic": "gliner-default",
                    "gliner_prepass": "gliner-default",
                },
            }
        }
    )
    resolved = resolve_gliner_settings(configured, "gliner_decision")
    assert "classification" in resolved.tasks
    assert "entities" in resolve_gliner_settings(configured, "gliner_semantic").tasks


def test_bound_llm_roles_merge_inline_and_named_profiles() -> None:
    config = _config(
        {
            "llm": {"roles": {"chat": {"provider": "inline", "settings": {"x": 1}}}},
            "models": {
                "profiles": {"extractor": _llm_profile(model="local/model")},
                "bindings": {"extract": "extractor"},
            },
        }
    )
    profile = resolve_llm_profile(config, "extractor")
    assert profile.provider == "litellm" and profile.settings == {"model": "local/model"}
    effective = bound_llm_roles(config)
    assert effective["extract"] == profile
    assert effective["chat"].provider == "inline"


@pytest.mark.parametrize(
    "models",
    [
        {"profiles": {}, "bindings": {"missing_role": "unknown"}},
        {"profiles": {"p": _llm_profile()}, "bindings": {"unknown_role": "p"}},
        {"profiles": {"p": _llm_profile()}, "bindings": {"gliner_semantic": "p"}},
        {
            "profiles": {"p": {"kind": "gliner", "provider": "gliner2"}},
            "bindings": {"extract": "p"},
        },
    ],
)
def test_invalid_binding_roles_and_kinds_are_rejected(models: dict) -> None:
    with pytest.raises((ValueError, ConfigError)):
        _config({"models": models})


def test_missing_profile_reference_and_inline_binding_conflict_are_rejected() -> None:
    with pytest.raises((ValueError, ConfigError), match="unknown profile"):
        _config({"models": {"bindings": {"extract": "missing"}}})
    with pytest.raises((ValueError, ConfigError), match="both llm.roles and models.bindings"):
        _config(
            {
                "llm": {"roles": {"extract": {"provider": "inline"}}},
                "models": {
                    "profiles": {"p": _llm_profile()},
                    "bindings": {"extract": "p"},
                },
            }
        )


def test_profile_names_are_nonempty_and_trimmed() -> None:
    with pytest.raises(ConfigError, match="models.profiles keys"):
        _config({"models": {"profiles": {" p ": _llm_profile()}}})


def test_legacy_and_misspelled_profile_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        _config({"models": {"gliner": {"tasks": ["entities"]}}})
    with pytest.raises(ValueError):
        _config({"decision": {"model": "some-checkpoint"}})
    with pytest.raises(ValueError):
        _config(
            {
                "models": {
                    "profiles": {
                        "ner": {
                            "kind": "gliner",
                            "provider": "gliner2",
                            "settings": {"taskz": ["entities"]},
                        }
                    }
                }
            }
        )


def test_gliner_provider_is_rejected_even_when_profile_is_unused() -> None:
    with pytest.raises(ValueError, match="GLiNER profiles require provider 'gliner2'"):
        _config(
            {
                "models": {
                    "profiles": {"ner": {"kind": "gliner", "provider": "other"}},
                }
            }
        )


def test_llm_profile_resolution_copies_nested_settings() -> None:
    config = _config(
        {
            "models": {
                "profiles": {"p": _llm_profile(options={"stop": ["DONE"]})},
                "bindings": {"extract": "p"},
            }
        }
    )
    resolved = resolve_llm_profile(config, "p")
    resolved.settings["options"]["stop"].append("CHANGED")
    assert config.models.profiles["p"].settings["options"]["stop"] == ["DONE"]


def test_gliner_profile_settings_are_strictly_validated() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ModelProfile.model_validate(
            {"kind": "gliner", "provider": "gliner2", "settings": {"unexpected": True}}
        )
    with pytest.raises(ValueError, match="surrounding whitespace"):
        ModelProfile.model_validate({"kind": "llm", "provider": " litellm "})


def test_active_gliner_consumer_requires_bound_capability_task() -> None:
    with pytest.raises(ConfigError, match="gliner_decision.*classification"):
        _config(
            {
                "decision": {"provider": "gliner2"},
                "models": {
                    "profiles": {
                        "decision": {
                            "kind": "gliner",
                            "provider": "gliner2",
                            "settings": {"tasks": ["entities"]},
                        }
                    },
                    "bindings": {"gliner_decision": "decision"},
                },
            }
        )


def test_escalation_requires_bound_primary_and_valid_bounded_targets() -> None:
    config = _config(
        {
            "models": {
                "profiles": {
                    "primary": _llm_profile(model="a"),
                    "backup": _llm_profile(model="b"),
                },
                "bindings": {"extract": "primary"},
                "escalation": {
                    "extract": {
                        "targets": ["backup"],
                        "triggers": ["error", "invalid_json"],
                        "max_attempts": 2,
                        "timeout_s": 3.0,
                        "max_tokens_per_attempt": 128,
                    }
                },
            }
        }
    )
    assert isinstance(config.models.escalation["extract"], EscalationPolicy)

    invalid_escalations = [
        {"targets": ["missing"], "timeout_s": 3.0, "max_tokens_per_attempt": 128},
        {"targets": ["primary"], "timeout_s": 3.0, "max_tokens_per_attempt": 128},
        {"targets": ["backup"], "max_attempts": 3, "timeout_s": 3.0, "max_tokens_per_attempt": 128},
    ]
    for policy in invalid_escalations:
        with pytest.raises((ValueError, ConfigError)):
            _config(
                {
                    "models": {
                        "profiles": {
                            "primary": _llm_profile(model="a"),
                            "backup": _llm_profile(model="b"),
                        },
                        "bindings": {"extract": "primary"},
                        "escalation": {"extract": policy},
                    }
                }
            )


def test_escalation_requires_explicit_positive_limits() -> None:
    with pytest.raises(ValueError):
        EscalationPolicy(targets=["backup"])
    with pytest.raises(ValueError):
        EscalationPolicy(targets=["backup"], timeout_s=0, max_tokens_per_attempt=100)
    with pytest.raises(ValueError):
        EscalationPolicy(targets=["backup"], timeout_s=2.0, max_tokens_per_attempt=0)


def test_escalation_uses_yaml_safe_trigger_key_and_rejects_on_alias() -> None:
    policy = EscalationPolicy(
        targets=["backup"],
        triggers=["error", "empty"],
        timeout_s=2.0,
        max_tokens_per_attempt=100,
    )
    assert policy.triggers == ("error", "empty")
    with pytest.raises(ValueError):
        EscalationPolicy(
            targets=["backup"],
            on=["error"],
            timeout_s=2.0,
            max_tokens_per_attempt=100,
        )
