"""sentence-transformers weights connector (RM-113, ``[st]`` extra)."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import MissingServiceError

__all__ = ["SentenceTransformerSpec", "st_weights"]


@dataclass(frozen=True, slots=True)
class SentenceTransformerSpec:
    """Everything that changes the loaded model; prompts and batch sizes stay per holder."""

    model: str
    device: str | None = None
    dtype: str | None = None
    trust_remote_code: bool = False


def _load(spec: SentenceTransformerSpec) -> Any:
    from sentence_transformers import SentenceTransformer

    kwargs: dict[str, Any] = {}
    if spec.dtype:
        kwargs["model_kwargs"] = {"torch_dtype": spec.dtype}
    model = SentenceTransformer(
        spec.model, device=spec.device, trust_remote_code=spec.trust_remote_code, **kwargs
    )
    model.max_seq_length = min(model.max_seq_length or 8192, 8192)
    return model


def st_weights(
    spec: SentenceTransformerSpec, *, registry: WeightRegistry | None = None
) -> SharedWeights[Any]:
    """A lease on ``SentenceTransformer(spec.model)``; the extra is checked here."""
    try:
        importlib.import_module("sentence_transformers")
    except ImportError as exc:
        raise MissingServiceError("services.embedding.st", extra="st") from exc
    return SharedWeights(
        "model.embedding.st",
        {"model": spec.model},
        {"device": spec.device, "dtype": spec.dtype, "trust_remote_code": spec.trust_remote_code},
        lambda: _load(spec),
        warm=lambda loaded: loaded.encode(["warm-up"], convert_to_numpy=True),
        registry=registry,
    )
