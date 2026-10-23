"""The documented ADR-082 model-slots example is current configuration, not pseudocode."""

from pathlib import Path
from typing import Any

import yaml

from memspine.config.schema import MemspineConfig
from memspine.config.schema.model import (
    gliner_settings,
    llm_slot,
    llm_slots,
    weights_settings,
)

_EXAMPLE = Path(__file__).resolve().parents[3] / "docs" / "examples" / "model_slots.yaml"


def _raw() -> dict[str, Any]:
    data = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _config() -> MemspineConfig:
    return MemspineConfig.model_validate(_raw())


def test_example_validates_and_shows_every_slot_category() -> None:
    raw = _raw()
    assert set(raw["model"]) == {
        "embedding",
        "llm",
        "rerank",
        "extraction",
        "decider",
        "vision",
        "placement",
    }
    config = _config()
    model = config.model
    assert model.embedding.provider == "onnx"
    assert [stage.provider for stage in model.rerank.chain] == ["jina", "onnx"]
    assert model.rerank.pool == 64
    assert model.vision is not None and model.vision.provider == "ollama"
    assert model.placement.max_loaded_models == 2
    assert config.read.planner == "decision"


def test_example_llm_roles_carry_their_own_options() -> None:
    config = _config()
    roles = llm_slots(config)
    assert set(roles) == {"window_extract", "chat", "judge"}
    assert all(slot.timeout_s > 0 for slot in roles.values())
    extract = llm_slot(config, "window_extract")
    assert extract is not None and extract.structured.retry_on_error
    assert extract.escalation is not None
    assert extract.escalation.triggers == ("error", "invalid_json", "empty")
    (target,) = extract.escalation.targets
    assert target.settings["model"] != extract.settings["model"]
    judge = llm_slot(config, "judge")
    assert judge is not None and judge.thinking == 512
    assert roles["chat"].settings["model"] != judge.settings["model"]
    assert llm_slot(config, "reflect") is None  # not listed = off


def test_example_extraction_and_decider_consumers() -> None:
    config = _config()
    extraction = config.model.extraction
    for consumer in ("semantic", "prepass", "topic", "sensitivity"):
        assert getattr(extraction, consumer) is not None, consumer
    assert extraction.topic is not None
    assert extraction.topic.labels == {"cardiology": "health/condition"}
    assert extraction.topic.min_confidence == 0.6
    decider = config.model.decider
    assert decider.route is not None and decider.route.provider == "gliner2"
    assert decider.read is not None and decider.read.provider == "opendecider"
    assert decider.read.tasks == ["list_mode", "bridge_hop"]
    assert gliner_settings(config, "route").model != gliner_settings(config, "semantic").model


def test_example_gliner_consumers_of_one_checkpoint_share_weights() -> None:
    extraction = _config().model.extraction
    assert extraction.semantic is not None and extraction.topic is not None
    assert extraction.semantic.settings["tasks"] != extraction.topic.settings["tasks"]
    assert weights_settings(extraction.semantic) == weights_settings(extraction.topic)
