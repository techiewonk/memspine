"""Configuration for remote Temporal and embedded SQLite journaling."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from memspine.config.schema import ResonateWorkerSettings, TemporalWorkerSettings, WorkersConfig


def test_resonate_journal_uses_explicit_persistent_file_path(tmp_path: Path) -> None:
    journal_path = (tmp_path / "durable volume" / "micro.sqlite3").resolve()
    settings = WorkersConfig(
        runner="temporal",
        temporal=TemporalWorkerSettings(deployment_id="test", address="127.0.0.1:17233"),
        resonate=ResonateWorkerSettings(journal_path=str(journal_path)),
    )

    assert settings.resonate.journal_path == str(journal_path)
    assert settings.temporal is not None
    assert settings.temporal.deployment_id == "test"


def test_resonate_journal_path_defaults_to_a_local_file() -> None:
    settings = ResonateWorkerSettings()
    assert settings.journal_path == "./memspine-resonate.sqlite"
    assert settings.journal_path != ":memory:"


def test_temporal_settings_require_a_stable_deployment_identity() -> None:
    with pytest.raises(ValidationError):
        TemporalWorkerSettings()
