"""The outbox CLI opens the supplied Engine configuration and closes it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from memspine.cli import app


def _config(path: Path, *, outbox: bool) -> Path:
    config = path / "engine.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "storage": {
                    "records": {
                        "provider": "sqlite",
                        "settings": {"path": str(path / "engine.sqlite")},
                    }
                },
                "model": {"embedding": {"provider": "hash"}},
                "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
                "write": {"window_extract": "facts", "online_extractor": True},
                "workers": {"outbox": outbox},
            }
        ),
        encoding="utf-8",
    )
    return config


def test_cli_drains_supplied_empty_backend(tmp_path: Path) -> None:
    config = _config(tmp_path, outbox=True)
    result = CliRunner().invoke(
        app, ["outbox", "drain", "--template", "core", "--config", str(config)]
    )
    assert result.exit_code == 0, result.output
    assert "items: 0" in result.output
    assert "remaining: 0" in result.output
    assert (tmp_path / "engine.sqlite").exists()


def test_cli_rejects_disabled_outbox(tmp_path: Path) -> None:
    config = _config(tmp_path, outbox=False)
    result = CliRunner().invoke(
        app, ["outbox", "drain", "--template", "core", "--config", str(config)]
    )
    assert result.exit_code == 1
    assert "requires workers.outbox" in result.output


async def test_cli_namespace_drains_only_selected_queued_scope(tmp_path: Path) -> None:
    from memspine import Engine
    from memspine.runtime.runner import DrainReport

    config = _config(tmp_path, outbox=True)
    contents = yaml.safe_load(config.read_text(encoding="utf-8"))
    contents["write"].update(window_async=False, window_max_turns=1, window_idle_s=0.0)
    config.write_text(yaml.safe_dump(contents), encoding="utf-8")

    async def make_engine() -> Engine:
        engine = Engine(template="core", user_config=config, dotenv_path=None)
        await engine.start()
        return engine

    first = await make_engine()
    try:
        assert first._windows is not None and first._windows._outbox is not None

        async def leave_queued(scope: Any = None) -> DrainReport:
            return DrainReport()

        first._windows._outbox.drain = leave_queued
        for namespace in ("selected", "other"):
            await first.ingest_messages(
                [
                    {
                        "message_id": f"cli-{namespace}",
                        "role": "user",
                        "sender": {"id": "Mira"},
                        "content": "I work for Acme.",
                        "created_at": "2026-10-24T10:00:00+00:00",
                    }
                ],
                namespace=namespace,
                session_id="s1",
                conversation_id="s1",
            )
            await first.flush_windows(namespace)
    finally:
        await first.stop()

    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        [
            "outbox",
            "drain",
            "--template",
            "core",
            "--config",
            str(config),
            "--namespace",
            "selected",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "items: 0" not in result.output

    second = await make_engine()
    try:
        assert second._windows is not None and second._windows._outbox is not None
        outbox = second._windows._outbox
        selected = second._windows._scope("selected")
        other = second._windows._scope("other")
        assert outbox.store.backlog(selected)[0] == 0, (
            outbox.store.backlog(selected, "window.discovery"),
            outbox.store.backlog(selected, "window"),
            outbox.store.backlog(other, "window.discovery"),
            outbox.store.backlog(other, "window"),
        )
        assert outbox.store.backlog(other)[0] > 0
    finally:
        await second.stop()

    all_scopes = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        ["outbox", "drain", "--template", "core", "--config", str(config)],
    )
    assert all_scopes.exit_code == 0, all_scopes.output
    assert "remaining: 0" in all_scopes.output
