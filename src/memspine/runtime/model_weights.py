"""ADR-093: the ONNX weights a configuration needs, and the start-time cache check (D-10).

:func:`onnx_requirements` maps a config's model slots (``model.embedding``, every
``model.rerank.chain`` entry and any ``onnx`` extraction/decider slot) to pinned catalogue
specs; ``memspine models fetch`` downloads them. :func:`check_model_weights` runs at Engine
start: a slot whose files are not in the Hugging Face cache raises ``MissingServiceError``
naming the fetch command. Nothing here downloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from memspine.clients.weights.onnx_catalogue import (
    CATALOGUE,
    OnnxModelSpec,
    catalogue_id,
    resolve_model_spec,
)
from memspine.clients.weights.onnx_files import fetch_hint, missing_files
from memspine.config.schema import MemspineConfig
from memspine.config.schema.providers import OnnxEmbeddingSettings, OnnxRerankerSettings
from memspine.exceptions import MissingServiceError
from memspine.observability.logging import get_logger

__all__ = ["WeightRequirement", "check_model_weights", "onnx_requirements"]

_log = get_logger(__name__)

WeightCategory = Literal["embedding", "rerank", "extraction", "decider"]


@dataclass(frozen=True)
class WeightRequirement:
    slot: str
    category: WeightCategory
    model: str
    spec: OnnxModelSpec
    download: bool

    @property
    def catalogued(self) -> bool:
        return catalogue_id(self.spec) is not None


def _requirement(
    slot: str, category: WeightCategory, raw: Mapping[str, Any], settings_model: Any
) -> WeightRequirement | None:
    try:
        settings = settings_model.model_validate(dict(raw)) if settings_model is not None else None
        if settings is not None:
            model, model_spec, download = settings.model, settings.model_spec, settings.download
        else:
            model = raw.get("model")
            if not isinstance(model, str):
                return None
            model_spec, download = raw.get("model_spec"), raw.get("download") is True
        spec = resolve_model_spec(model, model_spec)
    except (ValidationError, ValueError, TypeError):
        return None  # invalid settings are the provider build's error to report
    return WeightRequirement(slot, category, model, spec, bool(download))


def onnx_requirements(config: MemspineConfig) -> list[WeightRequirement]:
    """Every ``onnx`` model slot of ``config`` (embedding preset applied), in slot order."""
    from memspine.runtime.container import resolved_embedding

    found: list[WeightRequirement] = []
    embedding = resolved_embedding(config)
    if embedding.provider == "onnx":
        req = _requirement(
            "model.embedding", "embedding", embedding.settings, OnnxEmbeddingSettings
        )
        if req is not None:
            found.append(req)
    for index, stage in enumerate(config.model.rerank.chain):
        if stage.provider == "onnx":
            req = _requirement(
                f"model.rerank.chain[{index}]", "rerank", stage.settings, OnnxRerankerSettings
            )
            if req is not None:
                found.append(req)
    extraction = config.model.extraction
    decider = config.model.decider
    others: tuple[tuple[str, WeightCategory, Any], ...] = (
        ("model.extraction.semantic", "extraction", extraction.semantic),
        ("model.extraction.prepass", "extraction", extraction.prepass),
        ("model.extraction.topic", "extraction", extraction.topic),
        ("model.extraction.sensitivity", "extraction", extraction.sensitivity),
        ("model.decider.route", "decider", decider.route),
        ("model.decider.read", "decider", decider.read),
    )
    for path, category, slot in others:
        if slot is not None and slot.provider == "onnx":
            req = _requirement(path, category, slot.settings, None)
            if req is not None:
                found.append(req)
    return found


def catalogue_requirement(model: str) -> WeightRequirement:
    """``--model ID``: a catalogue entry by id (ValueError when it is not catalogued)."""
    if model not in CATALOGUE:
        raise ValueError(
            f"{model!r} is not in the pinned ONNX catalogue ({', '.join(sorted(CATALOGUE))})"
        )
    spec = CATALOGUE[model]
    category: WeightCategory = "rerank" if spec.pooling == "score" else "embedding"
    return WeightRequirement(f"--model {model}", category, model, spec, False)


def check_model_weights(config: MemspineConfig) -> None:
    """D-10 / ADR-093 start check: cache presence of every cache-only ONNX slot.

    ``strict_services: true`` raises ``MissingServiceError`` naming ``memspine models fetch``.
    ``false``: a reranker is skipped with a warning (its lazy build reports it again and stays
    off); the embedder keeps its existing behaviour (the Engine starts, a load fails visibly
    on first use). There is never an automatic download here."""
    for req in onnx_requirements(config):
        if req.download:
            continue
        missing = missing_files(req.spec)
        if not missing:
            continue
        hint = fetch_hint(req.model if req.catalogued else None)
        if config.strict_services:
            raise MissingServiceError(
                f"{req.slot} weights {req.model} ({', '.join(missing)} not in the Hugging Face"
                " cache)",
                hint=hint,
            )
        _log.warning(
            "service.missing_ignored",
            detail=(
                "strict_services=false: reranking skipped"
                if req.category == "rerank"
                else "strict_services=false: model loads fail until the weights are fetched"
            ),
            slot=req.slot,
            model=req.model,
            missing=missing,
            hint=hint,
        )
