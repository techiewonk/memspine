"""WT-01: tap config defaults and the engine verbs' request wrappers."""

from __future__ import annotations

from memspine.config.schema import MemspineConfig
from memspine.observability import tap


def test_default_config_tap_off():
    cfg = MemspineConfig()
    assert cfg.observability.tap.enabled is False
    assert cfg.observability.tap.sink == "jsonl"
    assert cfg.observability.tap.dir == "runs/_tap"
    assert cfg.observability.tap.content is False


def test_engine_verbs_are_traced_wrappers():
    from memspine import Engine

    for name in ("search", "write", "sleep", "assemble", "retrieve", "read"):
        assert hasattr(getattr(Engine, name), "__wrapped__"), name
    assert not tap.enabled()
