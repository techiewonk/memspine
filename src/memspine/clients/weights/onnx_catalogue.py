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


#: Tokenizer metadata every ONNX load reads beside ``tokenizer_file`` (see ``OnnxModel``).
TOKENIZER_METADATA_FILES: tuple[str, ...] = ("tokenizer_config.json", "special_tokens_map.json")


@dataclass(frozen=True)
class PinnedFile:
    """One pinned catalogue file: its repository path, byte size and SHA-256 content digest."""

    filename: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.size) is not int or self.size < 0:
            raise ValueError("pinned file size must be a nonnegative integer")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("pinned file sha256 must be 64 lower-case hex characters")


def _pins(*rows: tuple[str, int, str]) -> tuple[PinnedFile, ...]:
    return tuple(PinnedFile(name, size, digest) for name, size, digest in rows)


_BERT_UNCASED_TOKENIZER = (
    ("tokenizer.json", 711396, "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"),
    (
        "tokenizer_config.json",
        1242,
        "0b29c7bfc889e53b36d9dd3e686dd4300f6525110eaa98c76a5dafceb2029f53",
    ),
)
_BERT_SPECIAL_TOKENS = (
    "special_tokens_map.json",
    695,
    "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
)

#: ADR-093 (amendment 2026-11-01): every file a catalogue load reads, pinned by size and
#: SHA-256. Digests were taken from ``hf_hub_download`` at the pinned revision; LFS files
#: match the Hub's own LFS sha256 (evidence:
#: docs/provider_verification/onnx_catalogue_digests_2026-11-01.json). An int8 variant would
#: be its own row. The loader and ``memspine models fetch`` refuse a file that differs.
CATALOGUE_FILES: dict[str, tuple[PinnedFile, ...]] = {
    "BAAI/bge-small-en-v1.5": _pins(
        (
            "model_optimized.onnx",
            66465124,
            "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431",
        ),
        *_BERT_UNCASED_TOKENIZER,
        _BERT_SPECIAL_TOKENS,
    ),
    "BAAI/bge-base-en-v1.5": _pins(
        (
            "model_optimized.onnx",
            217824172,
            "4e556722bc4f65716c544c8a931f1e90fb3f866e5741fd93a96f051d673339c7",
        ),
        *_BERT_UNCASED_TOKENIZER,
        _BERT_SPECIAL_TOKENS,
    ),
    "sentence-transformers/all-MiniLM-L6-v2": _pins(
        (
            "model.onnx",
            90387630,
            "bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5",
        ),
        (
            "tokenizer.json",
            711649,
            "851ca67100d372ca3ae031a6abd168f53489eebfd7d89523f35c5c9b4d372c3c",
        ),
        (
            "tokenizer_config.json",
            1412,
            "abda01c8c14c5151ae498aceb30db406d6b91242c394fd88d3b6fd5a63a101e6",
        ),
        _BERT_SPECIAL_TOKENS,
    ),
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": _pins(
        (
            "model_optimized.onnx",
            235052644,
            "634d0f66c29dc934c8fa72b8a4fe91dd4d420a22f1d82a241058d4316e659a99",
        ),
        (
            "tokenizer.json",
            17083009,
            "fa685fc160bbdbab64058d4fc91b60e62d207e8dc60b9af5c002c5ab946ded00",
        ),
        (
            "tokenizer_config.json",
            1416,
            "0666eebf692422757e1dddf3c9fb1ded73ba3dc726c5828671fc89e45bf3609f",
        ),
        (
            "special_tokens_map.json",
            964,
            "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
        ),
    ),
    "Xenova/ms-marco-MiniLM-L-6-v2": _pins(
        (
            "onnx/model.onnx",
            90992115,
            "c623d0bcb99f4622beb413eaef00cfbe5db20df9f1dd982da4b4f26022881870",
        ),
        *_BERT_UNCASED_TOKENIZER,
        (
            "special_tokens_map.json",
            125,
            "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
        ),
    ),
}

_PINNED_BY_SOURCE: dict[tuple[str, str], dict[str, PinnedFile]] = {
    (CATALOGUE[model].repo, CATALOGUE[model].revision): {pin.filename: pin for pin in pins}
    for model, pins in CATALOGUE_FILES.items()
}


_ID_BY_SOURCE: dict[tuple[str, str], str] = {
    (spec.repo, spec.revision): model for model, spec in CATALOGUE.items()
}


def catalogue_id(spec: OnnxModelSpec) -> str | None:
    """The catalogue model id whose pinned export ``spec`` names, if any."""
    return _ID_BY_SOURCE.get((spec.repo, spec.revision))


def spec_files(spec: OnnxModelSpec) -> tuple[str, ...]:
    """Every repository file an ONNX load of ``spec`` reads, in load order."""
    return (
        spec.model_file,
        *spec.additional_files,
        spec.tokenizer_file,
        *TOKENIZER_METADATA_FILES,
    )


def pinned_file(spec: OnnxModelSpec, filename: str) -> PinnedFile | None:
    """The pinned digest for ``filename`` of ``spec``'s repo at its revision, if catalogued.

    Keyed by source (repo, revision), so a custom ``model_spec`` naming a catalogued export is
    verified too; any other custom export has no pin (its integrity is the caller's)."""
    return _PINNED_BY_SOURCE.get((spec.repo, spec.revision), {}).get(filename)


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


def spec_identity(spec: OnnxModelSpec) -> dict[str, Any]:
    return asdict(spec)


def model_embedder_id(
    model: str,
    model_spec: dict[str, Any] | OnnxModelSpec | None = None,
    *,
    query_instruction: str | None = None,
) -> str:
    """A catalogue certificate grants ``embedder:``; all custom spaces include their spec."""
    spec = resolve_model_spec(model, model_spec)
    if spec.pooling == "score":
        raise ValueError("ONNX score model cannot be used for embedding")
    if query_instruction:
        # The instruction is holder-owned rather than shared weight metadata, but
        # it changes retrieval semantics and must enter projection/space identity.
        # Otherwise a space built with the instruction and one built without it
        # would share an identity, so a table could be swapped for one with
        # different query semantics while keeping the projection checkpoint.
        spec = replace(spec, query_prefix=query_instruction, certified=False)
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
