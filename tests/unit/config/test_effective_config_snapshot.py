from __future__ import annotations

import pytest
from pydantic import ValidationError

from memspine.config.schema.base import ConfigInputError
from memspine.config.schema.root import MemspineConfig, freeze_effective_config
from memspine.exceptions import ConfigError


def test_mutated_typed_config_rechecks_ranges_without_disclosing_settings() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": 1.0,
                }
            }
        }
    )
    config.write.window_max_turns = 0
    config.storage.settings["private_token"] = "sensitive-example-value"
    with pytest.raises(ConfigInputError, match="write.window_max_turns") as caught:
        freeze_effective_config(config)
    assert "sensitive-example-value" not in str(caught.value)


def test_mutated_typed_config_rechecks_cross_field_rules() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": 1.0,
                }
            }
        }
    )
    config.runtime.environment = "production"
    with pytest.raises(ConfigError, match="requires runtime.deployment_id"):
        freeze_effective_config(config)


def test_valid_typed_mutation_preserves_explicit_fields() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": 1.0,
                }
            }
        }
    )
    config.write.window_max_turns = 12
    fields = set(config.write.model_fields_set)
    snapshot = freeze_effective_config(config)
    assert snapshot.write.window_max_turns == 12
    assert snapshot.write.model_fields_set == fields
    assert config.write.model_fields_set == fields


def test_effective_config_is_detached_and_recursively_immutable() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "environment": "development",
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": 1.0,
                },
            },
            "storage": {"settings": {"path": "memory.db"}},
            "firewall": {"signals": {"minja_bridge_exempt_roles": ["assistant"]}},
            "memories": {
                "semantic": {"enabled": True, "policies": {"nested": {"weights": [1, 2]}}}
            },
        }
    )
    snapshot = freeze_effective_config(config)
    second_snapshot = freeze_effective_config(config)

    assert snapshot is not config
    assert second_snapshot is not snapshot
    assert second_snapshot.storage.settings is not snapshot.storage.settings
    assert isinstance(snapshot, MemspineConfig)
    assert snapshot.storage is not config.storage
    assert snapshot.storage.model_fields_set == config.storage.model_fields_set
    assert snapshot.storage == config.storage
    assert snapshot.storage.settings == {"path": "memory.db"}
    assert snapshot.firewall.signals.minja_bridge_exempt_roles == ["assistant"]
    assert snapshot.memories["semantic"].policies["nested"]["weights"] == [1, 2]

    with pytest.raises(ValidationError, match="frozen"):
        snapshot.storage.provider = "postgres"  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        snapshot.storage.model_fields_set.add("path")
    with pytest.raises(TypeError, match="immutable"):
        snapshot.storage.settings["path"] = "changed.db"
    with pytest.raises(TypeError, match="immutable"):
        snapshot.memories["semantic"].policies["nested"]["extra"] = True
    with pytest.raises(TypeError, match="immutable"):
        snapshot.memories["semantic"].policies["nested"]["weights"].append(3)
    with pytest.raises(TypeError, match="immutable"):
        snapshot.firewall.signals.minja_bridge_exempt_roles.append("user")

    # A caller holding the mutable config used to create the snapshot cannot mutate it indirectly.
    config.storage.settings["path"] = "changed.db"
    config.memories["semantic"].policies["nested"]["weights"].append(3)
    assert snapshot.storage.settings["path"] == "memory.db"
    assert snapshot.memories["semantic"].policies["nested"]["weights"] == [1, 2]


def test_snapshot_preserves_explicit_field_presence_and_schema_config() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": 1.0,
                }
            },
            "storage": {"provider": "sqlite"},
        }
    )
    before = dict(MemspineConfig.model_config)
    snapshot = freeze_effective_config(config)

    assert snapshot.storage.model_fields_set == {"provider"}
    assert config.storage.model_fields_set == {"provider"}
    assert MemspineConfig.model_config == before
    assert MemspineConfig.model_config.get("frozen") is None


def test_snapshot_serialization_and_revalidation_preserve_values_and_freezing() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            },
            "storage": {"settings": {"path": "snapshot.db"}},
        }
    )
    snapshot = freeze_effective_config(config)
    assert snapshot.model_dump(mode="json") == config.model_dump(mode="json")
    assert snapshot.storage.model_dump_json() == config.storage.model_dump_json()
    validated = type(snapshot.storage).model_validate({"provider": "sqlite"})
    with pytest.raises(ValidationError, match="frozen"):
        validated.provider = "postgres"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        type(snapshot.storage).model_validate({"provider": 17})
