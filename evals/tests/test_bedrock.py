"""Bedrock helpers, offline: credential isolation and the hard call cap."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from memspine_evals.bedrock import (
    COHERE_EMBED_V4,
    COHERE_EMBED_V4_DIM,
    QWEN3_32B,
    TITAN_V2,
    TITAN_V2_DIM,
    BudgetExceeded,
    CallBudget,
    bedrock_engine_config,
    load_aws_credentials,
)


def test_only_aws_keys_are_exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION_NAME",
        "AWS_DEFAULT_REGION",
        "DB__PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "AWS_ACCESS_KEY_ID=AKIAEXAMPLE\nAWS_SECRET_ACCESS_KEY='s3cr3t'\n"
        "AWS_MODEL_REGION=us-west-2\nDB__PASSWORD=do-not-export\n",
        encoding="utf-8",
    )
    region = load_aws_credentials(env)
    assert region == "us-west-2"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "s3cr3t"
    assert "DB__PASSWORD" not in os.environ  # unrelated secrets never leave the file


def test_process_env_wins_unless_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FROM_ENV")
    env = tmp_path / ".env"
    env.write_text("AWS_ACCESS_KEY_ID=FROM_FILE\n", encoding="utf-8")
    load_aws_credentials(env)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "FROM_ENV"
    load_aws_credentials(env, override=True)
    assert os.environ["AWS_ACCESS_KEY_ID"] == "FROM_FILE"


def test_missing_credentials_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    with pytest.raises(RuntimeError):
        load_aws_credentials(tmp_path / "absent.env")


def test_call_budget_blocks_before_the_call() -> None:
    budget = CallBudget(max_calls=2, prices_per_mtok={QWEN3_32B: (1.0, 2.0)})
    budget.reserve()
    budget.record(QWEN3_32B, 1_000_000, 500_000)
    budget.reserve()
    with pytest.raises(BudgetExceeded):
        budget.reserve()
    assert budget.calls == 2
    assert budget.usd() == pytest.approx(2.0)


def test_engine_config_defaults_to_cohere_v4_and_qwen3() -> None:
    cfg = bedrock_engine_config("us-east-1")
    assert cfg["embedding"] == {
        "provider": "litellm",
        "model": COHERE_EMBED_V4,
        "dim": COHERE_EMBED_V4_DIM,
        "aws_region": "us-east-1",
    }
    assert COHERE_EMBED_V4_DIM == 1536
    assert all(role["model"] == QWEN3_32B for role in cfg["llm"]["roles"].values())


def test_engine_config_can_select_titan() -> None:
    cfg = bedrock_engine_config("us-east-1", embed_model=TITAN_V2, embed_dim=TITAN_V2_DIM)
    assert cfg["embedding"]["model"] == TITAN_V2 and cfg["embedding"]["dim"] == 1024
