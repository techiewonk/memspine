"""EnvSecrets: process env + .env parsing, loader integration (D-22)."""

from __future__ import annotations

from pathlib import Path

from memspine.config.loader import load_config
from memspine.services.secrets.env import EnvSecrets


def test_environ_wins_over_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('API_KEY="from-file"\nONLY_FILE=file-val\n# comment\n', encoding="utf-8")
    secrets = EnvSecrets(dotenv_path=dotenv, environ={"API_KEY": "from-env"})
    assert secrets.get("API_KEY") == "from-env"
    assert secrets.get("ONLY_FILE") == "file-val"
    assert secrets.get("MISSING") is None


def test_two_phase_bootstrap_with_loader(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("DB_PATH=/data/spine.db\n", encoding="utf-8")
    secrets = EnvSecrets(dotenv_path=dotenv, environ={})
    resolved = load_config(
        overrides={"storage": {"path": "${secret:DB_PATH}"}},
        secret_resolver=secrets.get,
    )
    assert resolved.config.storage.path == "/data/spine.db"
