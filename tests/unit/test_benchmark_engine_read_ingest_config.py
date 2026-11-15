"""The read/ingest benchmark compares an explicit rerank-off arm to MiniLM-on."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.benchmark_engine_read_ingest import _config, _effective_config

from memspine.config.loader import load_config
from memspine.runtime.model_weights import onnx_requirements


def test_read_benchmark_rerank_arms_resolve_independently(tmp_path: Path) -> None:
    off = load_config(template="base", user_config=_config(tmp_path, rerank=False)).config
    on = load_config(template="base", user_config=_config(tmp_path, rerank=True)).config

    assert off.model.rerank.chain == []
    assert [(stage.provider, stage.settings) for stage in on.model.rerank.chain] == [("onnx", {})]
    assert [req.model for req in onnx_requirements(off)] == ["BAAI/bge-small-en-v1.5"]
    assert [req.model for req in onnx_requirements(on)] == [
        "BAAI/bge-small-en-v1.5",
        "Xenova/ms-marco-MiniLM-L-6-v2",
    ]
    assert off.storage == on.storage
    assert off.model.embedding == on.model.embedding
    assert off.read == on.read


def test_concurrent_current_default_inherits_base_minilm(tmp_path: Path) -> None:
    current = load_config(template="base", user_config=_config(tmp_path, rerank=None)).config
    off = load_config(template="base", user_config=_config(tmp_path, rerank=False)).config

    assert [(stage.provider, stage.settings) for stage in current.model.rerank.chain] == [
        ("onnx", {})
    ]
    assert [req.model for req in onnx_requirements(current)] == [
        "BAAI/bge-small-en-v1.5",
        "Xenova/ms-marco-MiniLM-L-6-v2",
    ]
    assert current.model.embedding == off.model.embedding
    effective = _effective_config(type("Started", (), {"config": current})())
    assert [model["model"] for model in effective["onnx_models"]] == [
        "BAAI/bge-small-en-v1.5",
        "Xenova/ms-marco-MiniLM-L-6-v2",
    ]
    assert "settings" not in effective["embedding"]
    assert "window_max_turns" in effective["resolved"]["write"]
    assert effective["resolved_redacted_sha256"]


def test_effective_config_read_copy_is_redacted_but_keeps_numeric_token_budget(
    tmp_path: Path,
) -> None:
    current = load_config(template="base", user_config=_config(tmp_path, rerank=None)).config
    current.read.scoring["api_key"] = "read-secret"
    current.read.assembly["connection_string"] = "host=db password=read-secret"
    current.read.compression["max_tokens"] = 2048
    effective = _effective_config(type("Started", (), {"config": current})())
    assert effective["read"] == effective["resolved"]["read"]
    assert effective["read"]["scoring"]["api_key"] == "<redacted>"
    assert effective["read"]["assembly"]["connection_string"] == "<redacted-connection>"
    assert effective["read"]["compression"]["max_tokens"] == 2048
    assert "read-secret" not in json.dumps(effective)
