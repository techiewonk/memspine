"""Pluggable secrets (Phase 3, D-22): env / .env, AWS bundle + per-name, chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memspine.services.secrets.aws import AwsSecrets
from memspine.services.secrets.chained import ChainedSecrets
from memspine.services.secrets.env import EnvSecrets


class FakeSecretsManager:
    """Minimal stand-in for a boto3 secretsmanager client."""

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:  # boto3 kwarg name
        self.calls.append(SecretId)
        if SecretId not in self._secrets:
            raise RuntimeError(f"ResourceNotFoundException: {SecretId}")
        return {"SecretString": self._secrets[SecretId]}


# ── EnvSecrets: env wins over .env, set-but-empty still wins ──────────────────


def test_env_wins_over_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("API_KEY=from-file\nONLY_FILE=file-value\n", encoding="utf-8")
    secrets = EnvSecrets(dotenv_path=dotenv, environ={"API_KEY": "from-env"})
    assert secrets.get("API_KEY") == "from-env"  # env layer wins
    assert secrets.get("ONLY_FILE") == "file-value"  # falls through to file
    assert secrets.get("ABSENT") is None


def test_env_empty_string_wins_over_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("K=file\n", encoding="utf-8")
    secrets = EnvSecrets(dotenv_path=dotenv, environ={"K": ""})
    assert secrets.get("K") == ""  # blanked-out secret must not resurrect the file value


# ── AwsSecrets: JSON bundle, per-name fallback, negative cache ────────────────


def test_aws_reads_json_bundle() -> None:
    client = FakeSecretsManager({"memspine": json.dumps({"DB_URL": "postgres://x", "N": 5})})
    aws = AwsSecrets(client=client, bundle_secret_id="memspine")
    assert aws.get("DB_URL") == "postgres://x"
    assert aws.get("N") == "5"  # coerced to str
    # bundle fetched exactly once and cached across lookups.
    assert aws.get("DB_URL") == "postgres://x"
    assert client.calls.count("memspine") == 1


def test_aws_per_name_fallback_when_absent_from_bundle() -> None:
    client = FakeSecretsManager({"memspine": json.dumps({"A": "1"}), "STANDALONE": "solo"})
    aws = AwsSecrets(client=client, bundle_secret_id="memspine")
    assert aws.get("STANDALONE") == "solo"  # fell back to GetSecretValue(SecretId=name)


def test_aws_missing_is_none_and_negatively_cached() -> None:
    client = FakeSecretsManager({"memspine": "{}"})
    aws = AwsSecrets(client=client, bundle_secret_id="memspine")
    assert aws.get("NOPE") is None
    aws.get("NOPE")
    # one bundle fetch + one per-name miss; the second get() hits the negative cache.
    assert client.calls.count("NOPE") == 1


def test_aws_tolerates_non_json_or_missing_bundle() -> None:
    client = FakeSecretsManager({"memspine": "not-json{"})
    aws = AwsSecrets(client=client, bundle_secret_id="memspine")
    assert aws.get("ANY") is None  # bad bundle degrades to empty, never crashes


# ── ChainedSecrets: first non-None wins, order = precedence ───────────────────


def test_chain_env_first_then_aws() -> None:
    env = EnvSecrets(environ={"LOCAL": "dev"})
    aws = AwsSecrets(
        client=FakeSecretsManager({"memspine": json.dumps({"LOCAL": "cloud", "REMOTE": "r"})}),
        bundle_secret_id="memspine",
    )
    chain = ChainedSecrets(env, aws)
    assert chain.get("LOCAL") == "dev"  # env overrides AWS
    assert chain.get("REMOTE") == "r"  # only AWS has it
    assert chain.get("NEITHER") is None


# ── engine bootstrap selector: MEMSPINE_SECRETS_BACKEND (pre-config) ──────────


def _engine() -> Any:
    from memspine import Engine

    return Engine(dotenv_path=None, storage={"path": ":memory:"})


def test_selector_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMSPINE_SECRETS_BACKEND", raising=False)
    assert isinstance(_engine()._build_secrets(), EnvSecrets)


def test_selector_aws_chains_env_then_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("boto3")
    monkeypatch.setenv("MEMSPINE_SECRETS_BACKEND", "aws")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")  # boto3 client needs a region
    resolver = _engine()._build_secrets()
    assert isinstance(resolver, ChainedSecrets)


def test_selector_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.exceptions import ConfigError

    monkeypatch.setenv("MEMSPINE_SECRETS_BACKEND", "vault")
    with pytest.raises(ConfigError, match="MEMSPINE_SECRETS_BACKEND"):
        _engine()._build_secrets()
