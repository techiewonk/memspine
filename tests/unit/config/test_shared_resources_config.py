"""RM-78: ``runtime.shared_resources`` settings (ADR-079 addendum)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.config.schema.runtime import SharedResourcesConfig
from memspine.exceptions import ConfigError

_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}


def _config(**shared: Any) -> MemspineConfig:
    return MemspineConfig(runtime={**_RUNTIME, "shared_resources": shared})


def test_defaults_match_the_adr_and_constants() -> None:
    cfg = _config().runtime.shared_resources
    assert cfg.scope == "process" == constants.SHARED_RESOURCES_SCOPE
    assert cfg.warmup == "background"
    assert cfg.warmup_timeout_s == 120.0
    assert cfg.warmup_concurrency == 2
    assert cfg.warmup_include == ("embedder", "reranker", "gliner", "lexical", "llm", "decider")
    assert cfg.warmup_exclude == ()
    assert cfg.keep_warm is True
    assert cfg.idle_ttl_s is None and cfg.memory_budget_mb is None
    assert cfg.max_models_per_category is None
    assert (cfg.sql.pool_size, cfg.sql.max_overflow, cfg.sql.pool_recycle_s) == (5, 10, 1800)
    assert cfg.sql.pool_pre_ping is True
    assert (cfg.http.max_connections, cfg.http.keepalive_s) == (100, 30.0)
    assert cfg.model_server is None


def test_yaml_style_values_are_accepted() -> None:
    cfg = _config(
        scope="engine",
        warmup="blocking",
        warmup_timeout_s=5,
        warmup_include=["embedder", "llm"],
        warmup_exclude=["gliner"],
        idle_ttl_s=600,
        memory_budget_mb=2048,
        max_models_per_category=2,
        sql={"pool_size": 2, "max_overflow": 0},
        http={"max_connections": 10},
        model_server="off",
    ).runtime.shared_resources
    assert cfg.scope == "engine" and cfg.warmup_include == ("embedder", "llm")
    assert cfg.warmup_categories() == ("embedder", "llm")
    assert cfg.sql.pool_size == 2 and cfg.http.max_connections == 10


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": "thread"},
        {"warmup": "eager"},
        {"warmup_timeout_s": 0},
        {"warmup_timeout_s": True},
        {"warmup_concurrency": 0},
        {"warmup_concurrency": 2.0},
        {"warmup_include": ["embedder", "gpu"]},
        {"warmup_include": ["embedder", "embedder"]},
        {"keep_warm": "yes"},
        {"idle_ttl_s": 0},
        {"memory_budget_mb": 0},
        {"max_models_per_category": 0},
        {"sql": {"pool_size": 0}},
        {"sql": {"max_overflow": -1}},
        {"sql": {"pool_recycle_s": 0}},
        {"sql": {"pool_pre_ping": 1}},
        {"http": {"max_connections": 0}},
        {"http": {"keepalive_s": 0}},
        {"unknown": 1},
        {"sql": {"pool": 1}},
    ],
)
def test_strict_validation_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SharedResourcesConfig(**bad)


def test_explicit_include_and_exclude_must_be_disjoint() -> None:
    with pytest.raises(ValidationError, match="disjoint"):
        SharedResourcesConfig(warmup_include=["embedder", "llm"], warmup_exclude=["llm"])
    cfg = SharedResourcesConfig(warmup_exclude=["llm", "decider"])
    assert cfg.warmup_categories() == ("embedder", "reranker", "gliner", "lexical")


def test_idle_ttl_requires_keep_warm() -> None:
    with pytest.raises(ValidationError, match="keep_warm"):
        SharedResourcesConfig(keep_warm=False, idle_ttl_s=10.0)
    assert SharedResourcesConfig(keep_warm=False).idle_ttl_s is None


@pytest.mark.parametrize("value", ["on", "local", "llama-server"])
def test_model_server_is_reserved_for_rm79(value: str) -> None:
    with pytest.raises(ConfigError, match="RM-79"):
        SharedResourcesConfig(model_server=value)
    with pytest.raises(ConfigError, match="RM-79"):
        _config(model_server=value)


def test_settings_are_frozen() -> None:
    cfg = SharedResourcesConfig()
    with pytest.raises(ValidationError):
        cfg.scope = "engine"  # type: ignore[misc]
