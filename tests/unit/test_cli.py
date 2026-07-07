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


def test_prompts_list_and_resolve(tmp_path: Path) -> None:
    result = runner.invoke(app, ["prompts", "list"])
    assert result.exit_code == 0
    assert "extract" in result.output and "firewall_flag" in result.output
    assert "source=defaults" in result.output

    cfg = tmp_path / "user.yaml"
    cfg.write_text(
        "prompts:\n  overrides:\n    extract:\n      body: 'custom {{ content }}'\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["prompts", "resolve", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "extract@2  # source: override" in result.output


def test_prompts_show_prints_frontmatter_and_body() -> None:
    result = runner.invoke(app, ["prompts", "show", "judge"])
    assert result.exit_code == 0
    assert "role: judge" in result.output and "---" in result.output

    result = runner.invoke(app, ["prompts", "show", "nope"])
    assert result.exit_code == 1


def test_forget_hard_verify_exit_codes(tmp_path: Path) -> None:
    """M7 CLI gate: ``forget --hard --verify`` exits 0 only on PROVEN erasure."""
    import asyncio

    from memspine import Engine

    db = tmp_path / "mem.db"

    async def seed() -> str:
        eng = Engine(
            template="base",
            dotenv_path=None,
            storage={"path": str(db)},
            embedding={"provider": "hash"},
        )
        await eng.start()
        record = await eng.write("a secret to erase", namespace="default")
        await eng.stop()
        return record.record_id

    record_id = asyncio.run(seed())
    result = runner.invoke(app, ["forget", record_id, "--db", str(db), "--hard", "--verify"])
    assert result.exit_code == 0, result.output
    assert "clean: true" in result.output

    result = runner.invoke(app, ["audit", "taint", record_id, "--db", str(db)])
    assert result.exit_code == 0
    assert record_id in result.output


def test_resolve_annotates_sources() -> None:
    result = runner.invoke(app, ["config", "resolve", "--template", "voice"])
    assert result.exit_code == 0
    assert "# source: template:voice" in result.output
    assert "# source: default" in result.output
    assert "event_log.mode" in result.output
