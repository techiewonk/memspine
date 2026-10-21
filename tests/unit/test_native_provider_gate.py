"""Strict native evidence must fail closed on incomplete execution."""

from __future__ import annotations

import pytest
from scripts.run_provider_live_contracts import (
    REQUIRED_SERVICE_ENV,
    gate_exit_code,
    missing_required_services,
    selected_images,
)


@pytest.mark.parametrize(
    ("code", "summary", "skips", "expected"),
    [
        (0, "8 passed, 12 warnings", [], 0),
        (0, "7 passed, 1 skipped", [], 2),
        (0, "8 skipped", ["SKIPPED unavailable"], 2),
        (0, "NO SUMMARY", [], 2),
        (1, "1 failed, 7 passed", [], 1),
        (5, "no tests ran", [], 5),
    ],
)
def test_strict_gate_rejects_incomplete_or_failed_evidence(code, summary, skips, expected):
    assert gate_exit_code(code, summary, skips, require_all=True) == expected


def test_exploratory_mode_preserves_pytest_skip_exit_code():
    assert gate_exit_code(0, "8 skipped", ["SKIPPED unavailable"], require_all=False) == 0


def test_required_service_errors_expose_names_only():
    environment = dict.fromkeys(REQUIRED_SERVICE_ENV, "synthetic-value")
    assert missing_required_services(environment) == []
    environment.pop("MEMSPINE_TEST_NEO4J_PASSWORD")
    assert missing_required_services(environment) == ["MEMSPINE_TEST_NEO4J_PASSWORD"]


def test_explicit_image_pin_disables_fallback(monkeypatch):
    monkeypatch.setenv("MEMSPINE_TEST_IMAGE_POSTGRES", "example@sha256:synthetic")
    assert selected_images("postgres", "postgres:16", "postgres:alpine") == (
        "example@sha256:synthetic",
        None,
    )


def test_strict_preflight_writes_failure_report_without_executing_pytest(monkeypatch, tmp_path):
    import json
    import sys

    from scripts import run_provider_live_contracts as runner

    for name in REQUIRED_SERVICE_ENV:
        monkeypatch.delenv(name, raising=False)
    report_path = tmp_path / "preflight.json"
    monkeypatch.setattr(sys, "argv", ["runner", "--require-all", "--report", str(report_path)])

    def unexpected(*args, **kwargs):
        raise AssertionError("preflight must not start subprocesses")

    monkeypatch.setattr(runner.subprocess, "run", unexpected)
    assert runner.main() == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["gate_exit_code"] == 2
    assert report["pytest_exit_code"] is None
    assert report["preflight_missing"] == list(REQUIRED_SERVICE_ENV)
    assert "runner_error_code" not in report


def test_created_service_is_ephemeral_and_binds_loopback_only(monkeypatch):
    from scripts import run_provider_live_contracts as runner

    commands = []
    monkeypatch.setattr(runner, "run", lambda args: commands.append(args) or "synthetic-container")
    service = runner.start_service("postgres", "synthetic-image", 5432, {}, "owned-test")
    assert service.container_name == "memspine-live-owned-test-postgres"
    command = commands[0]
    assert "--rm" in command
    assert command[command.index("--publish") + 1] == "127.0.0.1::5432"
    assert not any(arg in {"--volume", "--mount", "-v"} for arg in command)
