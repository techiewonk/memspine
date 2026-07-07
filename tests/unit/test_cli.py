"""CLI: `memspine config validate|resolve` (D-12)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from memspine.cli import app

runner = CliRunner()


def test_validate_base_template() -> None:
    result = runner.invoke(app, ["config", "validate", "--template", "base"])
    assert result.exit_code == 0
    assert "valid" in result.output
    assert "episodic" in result.output


def test_validate_reports_auto_enables(tmp_path: Path) -> None:
    cfg = tmp_path / "user.yaml"
    cfg.write_text("memories:\n  reflective:\n    enabled: true\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "auto-enabled (C1b): episodic" in result.output


def test_validate_unknown_template_fails() -> None:
    result = runner.invoke(app, ["config", "validate", "--template", "nope"])
    assert result.exit_code == 1
    assert "invalid" in result.output


def test_resolve_annotates_sources() -> None:
    result = runner.invoke(app, ["config", "resolve", "--template", "voice"])
    assert result.exit_code == 0
    assert "# source: template:voice" in result.output
    assert "# source: default" in result.output
    assert "event_log.mode" in result.output
