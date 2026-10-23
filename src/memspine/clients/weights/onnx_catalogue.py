"""Pinned ONNX exports and arithmetic contracts (ADR-084).

Revisions refer to the exact exports used by the committed fastembed 0.9.0
reference fixtures. In particular the multilingual model does NOT normalize.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class OnnxModelSpec:
    repo: str
    revision: str
    model_file: str
    pooling: Literal["cls", "mean", "score"]
    normalize: bool
    max_length: int
    dim: int
    tokenizer_file: str = "tokenizer.json"
    query_prefix: str = ""
    passage_prefix: str = ""
    certified: bool = False
    additional_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.revision) != 40 or any(c not in "0123456789abcdef" for c in self.revision):
            raise ValueError("ONNX revision must be a pinned 40-character commit hash")
        if self.max_length < 1 or self.dim < 1:
            raise ValueError("ONNX max_length and dim must be positive")
        if self.pooling not in {"cls", "mean", "score"}:
            raise ValueError("ONNX pooling must be cls, mean or score")


CATALOGUE: dict[str, OnnxModelSpec] = {
    "BAAI/bge-small-en-v1.5": OnnxModelSpec(
        "Qdrant/bge-small-en-v1.5-onnx-Q",
        "aa8f8b060edb00e03bfdd08813a2949946c8ba55",
        "model_optimized.onnx",
        "cls",
        True,
        512,
        384,
        certified=True,
    ),
    "BAAI/bge-base-en-v1.5": OnnxModelSpec(
        "Qdrant/bge-base-en-v1.5-onnx-Q",
        "199291fdd1aa89faf9c20b722dc72ad5e17aa0d0",
        "model_optimized.onnx",
        "cls",
        True,
        512,
        768,
        certified=True,
    ),
    "sentence-transformers/all-MiniLM-L6-v2": OnnxModelSpec(
        "Qdrant/all-MiniLM-L6-v2-onnx",
        "d13954661f83248295ba75c1ed411eef3b7b936e",
        "model.onnx",
        "mean",
        True,
        256,
        384,
        certified=True,
    ),
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": OnnxModelSpec(
        "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
        "faf4aa4225822f3bc6376869cb1164e8e3feedd0",
        "model_optimized.onnx",
        "mean",
        False,
        128,
        384,
        certified=True,
    ),
    "Xenova/ms-marco-MiniLM-L-6-v2": OnnxModelSpec(
        "Xenova/ms-marco-MiniLM-L-6-v2",
        "a09144355adeed5f58c8ed011d209bf8ee5a1fec",
        "onnx/model.onnx",
        "score",
        False,
        512,
        1,
        certified=True,
    ),
}


def resolve_model_spec(
    model: str, model_spec: dict[str, Any] | OnnxModelSpec | None = None
) -> OnnxModelSpec:
    if isinstance(model_spec, OnnxModelSpec):
        return model_spec
    if model_spec is not None:
        required = {
            "repo",
            "revision",
            "model_file",
            "tokenizer_file",
            "pooling",
            "normalize",
            "max_length",
            "query_prefix",
            "passage_prefix",
            "dim",
        }
        missing = required - model_spec.keys()
        if missing:
            raise ValueError(
                f"ONNX model_spec missing explicit fields: {', '.join(sorted(missing))}"
            )
        values = dict(model_spec)
        values["certified"] = False  # user-supplied metadata cannot assert parity
        values["additional_files"] = tuple(values.get("additional_files", ()))
        return OnnxModelSpec(**values)
    try:
        return CATALOGUE[model]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported ONNX model {model!r}; configure explicit settings.model_spec"
        ) from exc


def legacy_embedder_id(
    model: str, *, model_spec: Any = None, query_instruction: str | None = None
) -> str | None:
    spec = CATALOGUE.get(model)
    if (
        spec is None
        or not spec.certified
        or spec.pooling == "score"
        or model_spec is not None
        or query_instruction
    ):
        return None
    return f"fastembed:{model}"


def spec_identity(spec: OnnxModelSpec) -> dict[str, Any]:
    return asdict(spec)
