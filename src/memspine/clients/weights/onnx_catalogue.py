"""Pinned ONNX exports and arithmetic contracts (ADR-084).

Revisions refer to the exact exports used by the committed fastembed 0.9.0
reference fixtures. In particular the multilingual model does NOT normalize.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
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
        for field in (
            "repo",
            "revision",
            "model_file",
            "tokenizer_file",
            "pooling",
            "query_prefix",
            "passage_prefix",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"ONNX {field} must be a string without NUL characters")
        if not self.repo:
            raise ValueError("ONNX repo must be nonempty")
        if len(self.revision) != 40 or any(c not in "0123456789abcdef" for c in self.revision):
            raise ValueError("ONNX revision must be a pinned 40-character commit hash")
        if any(type(value) is not int or value < 1 for value in (self.max_length, self.dim)):
            raise ValueError("ONNX max_length and dim must be positive integers")
        if type(self.normalize) is not bool or type(self.certified) is not bool:
            raise ValueError("ONNX normalize and certified must be booleans")
        if self.pooling not in {"cls", "mean", "score"}:
            raise ValueError("ONNX pooling must be cls, mean or score")
        if not isinstance(self.additional_files, tuple):
            raise ValueError(
                "ONNX additional_files must be a tuple of repository-relative file paths"
            )
        for filename in (self.model_file, self.tokenizer_file, *self.additional_files):
            if (
                not isinstance(filename, str)
                or not filename
                or "\\" in filename
                or any(part in {"", ".", ".."} for part in filename.split("/"))
                or ":" in filename
                or "\x00" in filename
            ):
                raise ValueError("ONNX files must be nonempty repository-relative file paths")
        if self.pooling == "score" and self.dim != 1:
            raise ValueError("ONNX score models must have dim=1")


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
        return replace(model_spec, certified=False)
    if model_spec is not None:
        if not isinstance(model_spec, dict):
            raise ValueError("ONNX model_spec must be an explicit object")
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
        unknown = values.keys() - (required | {"certified", "additional_files"})
        if unknown:
            raise ValueError(f"ONNX model_spec unknown fields: {', '.join(sorted(unknown))}")
        values["certified"] = False  # user-supplied metadata cannot assert parity
        additional_files = values.get("additional_files", ())
        if not isinstance(additional_files, (list, tuple)):
            raise ValueError(
                "ONNX additional_files must be a list of repository-relative file paths"
            )
        values["additional_files"] = tuple(additional_files)
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


def model_embedder_id(model: str, model_spec: dict[str, Any] | OnnxModelSpec | None = None) -> str:
    """A catalogue certificate grants ``embedder:``; all custom spaces include their spec."""
    spec = resolve_model_spec(model, model_spec)
    if spec.pooling == "score":
        raise ValueError("ONNX score model cannot be used for embedding")
    if model_spec is None and spec.certified:
        return f"embedder:{model}"
    return _custom_model_id(model, spec)


def model_reranker_id(model: str, model_spec: dict[str, Any] | OnnxModelSpec | None = None) -> str:
    spec = resolve_model_spec(model, model_spec)
    if spec.pooling != "score":
        raise ValueError("ONNX embedding model cannot be used for reranking")
    return (
        f"onnx:{model}" if model_spec is None and spec.certified else _custom_model_id(model, spec)
    )


def _custom_model_id(model: str, spec: OnnxModelSpec) -> str:
    suffix = hashlib.sha256(json.dumps(spec_identity(spec), sort_keys=True).encode()).hexdigest()[
        :32
    ]
    return f"onnx:{model}:{suffix}"
