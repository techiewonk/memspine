"""Config layering golden tests (D-11/D-12/D-14/D-22)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine.config.loader import load_config, template_dir
from memspine.exceptions import ConfigError
from memspine.store.events import EventLogMode


def test_defaults_only() -> None:
    with pytest.raises(ConfigError, match="runtime"):
        load_config()


def test_all_shipped_templates_load_cleanly() -> None:
    for path in sorted(template_dir().glob("*.yaml")):
        resolved = load_config(template=path.stem)
        assert resolved.config.profile  # every template names its profile


def test_template_then_env_then_kwargs_precedence() -> None:
    resolved = load_config(
        template="voice",  # sets event_log.mode=rolling, retention_days=14
        env={"MEMSPINE_EVENT_LOG__RETENTION_DAYS": "7"},
        overrides={"profile": "custom"},
    )
    assert resolved.config.event_log.mode is EventLogMode.ROLLING
    assert resolved.config.event_log.retention_days == 7
    assert resolved.config.profile == "custom"
    assert resolved.sources["event_log.mode"] == "template:voice"
    assert resolved.sources["event_log.retention_days"] == "env"
    assert resolved.sources["profile"] == "kwargs"


def test_extends_chain_merges_base(tmp_path: Path) -> None:
    (tmp_path / "leaf.yaml").write_text("extends: base\nprofile: leaf\n", encoding="utf-8")
    resolved = load_config(template="leaf", extra_template_dirs=[tmp_path])
    assert resolved.config.profile == "leaf"
    assert resolved.config.memories["working"].enabled  # inherited from shipped base


def test_extends_cycle_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("extends: b\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("extends: a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="cycle"):
        load_config(template="a", extra_template_dirs=[tmp_path])


def test_missing_template_names_search_dirs() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(template="nope")


def test_reserved_namespace_memories_key_rejected() -> None:
    with pytest.raises(ConfigError, match=r"reserved for v0\.2"):
        load_config(template="base", overrides={"namespaces": {"org/a": {"memories": {}}}})


def test_unknown_memory_type_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config(overrides={"memories": {"telepathic": {"enabled": True}}})


def test_unknown_top_level_key_rejected() -> None:
    with pytest.raises(ConfigError):
        load_config(overrides={"not_a_key": 1})


def test_validation_error_reports_path_without_rejected_value() -> None:
    secretish_value = "sensitive-value-that-must-not-leak"
    with pytest.raises(ConfigError) as error:
        load_config(
            template="base",
            overrides={"runtime": {"environment": secretish_value}},
        )
    assert "runtime.environment" in str(error.value)
    assert secretish_value not in str(error.value)


def test_secret_resolution_and_missing_secret() -> None:
    resolved = load_config(
        template="base",
        overrides={"storage": {"records": {"settings": {"path": "${secret:DB_PATH}"}}}},
        secret_resolver=lambda name: "/tmp/x.db" if name == "DB_PATH" else None,
    )
    assert resolved.config.storage.records.settings["path"] == "/tmp/x.db"

    with pytest.raises(ConfigError, match="unresolved secret"):
        load_config(
            template="base",
            overrides={"storage": {"records": {"settings": {"path": "${secret:MISSING}"}}}},
        )


def test_env_scalars_parse_types() -> None:
    resolved = load_config(
        template="base",
        env={
            "MEMSPINE_EVENT_LOG__COMPRESS": "true",
            "MEMSPINE_EVENT_LOG__MODE": "ephemeral",
            "IGNORED": "x",
        },
    )
    assert resolved.config.event_log.compress is True
    assert resolved.config.event_log.mode is EventLogMode.EPHEMERAL


def test_workers_runner_rides_env_layering() -> None:
    resolved = load_config(
        template="base",
        env={
            "MEMSPINE_WORKERS__RUNNER": "temporal",
            "MEMSPINE_WORKERS__TEMPORAL__DEPLOYMENT_ID": "test-deployment",
        },
    )
    assert resolved.config.workers.runner == "temporal"
    assert resolved.config.workers.temporal is not None
    assert resolved.config.workers.temporal.deployment_id == "test-deployment"


def test_unrelated_memspine_env_vars_are_ignored() -> None:
    """Regression: MEMSPINE_HOME/MEMSPINE_DEBUG from other tooling used to be
    injected into the strict schema and crash every load."""
    resolved = load_config(template="base", env={"MEMSPINE_HOME": "/opt/x", "MEMSPINE_DEBUG": "1"})
    assert resolved.config.profile == "simple"


def test_yaml_hostile_env_value_falls_back_to_string() -> None:
    """Regression: '@data/db.sqlite' used to raise a raw yaml.ScannerError."""
    resolved = load_config(
        template="base", env={"MEMSPINE_STORAGE__RECORDS__SETTINGS__PATH": "@data/db.sqlite"}
    )
    assert resolved.config.storage.records.settings["path"] == "@data/db.sqlite"


def test_missing_user_config_file_is_config_error(tmp_path: Path) -> None:
    """Regression: FileNotFoundError used to escape the ConfigError contract."""
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(user_config=tmp_path / "nope.yaml")
