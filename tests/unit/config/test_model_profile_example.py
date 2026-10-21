"""The documented multi-model example is current configuration, not pseudocode."""

from pathlib import Path

import yaml

from memspine.config.schema import MemspineConfig
from memspine.models.profiles import bound_llm_roles, resolve_gliner_settings
from memspine.providers.registry import get_registry


def test_documented_model_profiles_validate_without_model_execution() -> None:
    example = Path(__file__).resolve().parents[3] / "docs/examples/model_profiles.yaml"
    config = MemspineConfig.model_validate(yaml.safe_load(example.read_text(encoding="utf-8")))
    roles = bound_llm_roles(config)
    assert roles["chat"].settings["model"] != roles["judge"].settings["model"]
    assert config.models.escalation["window_extract"].triggers == ("error", "invalid_json", "empty")
    for role, binding in roles.items():
        descriptor = get_registry("llm_providers").get(binding.provider)
        assert descriptor is not None, role
        descriptor.settings_model.model_validate(binding.settings)
    assert resolve_gliner_settings(config, "gliner_decision").model != (
        resolve_gliner_settings(config, "gliner_semantic").model
    )
    assert resolve_gliner_settings(config, "gliner_semantic") == (
        resolve_gliner_settings(config, "gliner_prepass")
    )
