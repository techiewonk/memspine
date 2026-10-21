"""Typed runner settings and Temporal/Resonate deadline invariants."""

from __future__ import annotations

import pytest

from memspine.config.schema import WorkersConfig


def test_temporal_settings_are_required_only_when_selected_and_redact_api_key() -> None:
    assert WorkersConfig().runner == "inline"

    config = WorkersConfig.model_validate(
        {
            "runner": "temporal",
            "temporal": {"deployment_id": "unit", "api_key": "unit-secret-value"},
        }
    )
    assert config.temporal is not None
    assert config.temporal.task_queue is None
    assert "unit-secret-value" not in repr(config)
    assert "unit-secret-value" not in str(config.model_dump(mode="json"))


@pytest.mark.parametrize(
    "workers",
    [
        {"runner": "temporal"},
        {
            "runner": "temporal",
            "temporal": {"deployment_id": "unit", "activity_timeout_s": 240},
        },
        {
            "runner": "temporal",
            "temporal": {"deployment_id": "unit", "workflow_timeout_s": 1000},
        },
        {
            "runner": "temporal",
            "temporal": {"deployment_id": "unit"},
            "resonate": {"step_lease_s": 300},
        },
        {
            "runner": "temporal",
            "temporal": {"deployment_id": "unit"},
            "resonate": {"journal_path": ":memory:"},
        },
    ],
)
def test_temporal_worker_deadlines_and_journal_must_be_safe(workers: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WorkersConfig.model_validate(workers)
