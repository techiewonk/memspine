"""GLiNER2 weights connector (RM-113, ``[ner]`` extra): snapshot resolution and the SDK load.

The only place a GLiNER checkpoint is downloaded or resolved: a concrete snapshot is resolved
first (``snapshot_download`` honouring ``revision`` and ``local_files_only``), checked for the
files the SDK needs, and only that local path reaches ``AutoExtractor.from_pretrained``, so the
SDK has no Hub id to fall back to. The extraction pool
(:mod:`memspine.services.extraction.gliner2`) leases these weights through the registry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "GLINER_WEIGHTS_CATEGORY",
    "GlinerWeightSettingsLike",
    "load_local_gliner",
    "validate_snapshot",
]

#: Registry category of GLiNER weights; extraction and decider consumers of the same
#: checkpoint share one load.
GLINER_WEIGHTS_CATEGORY = "model.gliner"


class GlinerWeightSettingsLike(Protocol):
    """What a loader receives: only the weight-affecting settings."""

    @property
    def model(self) -> str: ...

    @property
    def revision(self) -> str | None: ...

    @property
    def device(self) -> str: ...

    @property
    def local_files_only(self) -> bool: ...


def load_local_gliner(settings: GlinerWeightSettingsLike) -> Any:
    """Resolve a concrete snapshot then use the SDK loader only with a local path."""
    model_path = Path(settings.model).expanduser()
    if model_path.is_dir():
        snapshot = model_path
        if settings.revision is not None and snapshot.name != settings.revision:
            raise RuntimeError(
                "An explicit revision for a local GLiNER snapshot must match its directory name"
            )
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError("GLiNER runtime requires the optional [ner] dependencies") from exc
        snapshot = Path(
            snapshot_download(
                repo_id=settings.model,
                revision=settings.revision,
                local_files_only=settings.local_files_only,
            )
        )
    validate_snapshot(snapshot)
    try:
        from gliner2 import AutoExtractor
    except ImportError as exc:
        raise RuntimeError("GLiNER runtime requires the optional [ner] dependencies") from exc

    # AutoExtractor drops Hub kwargs on the selected model loader. Passing the
    # resolved local snapshot ensures the requested revision/local-only policy
    # applies to both config and weights. map_location is a model load option.
    return AutoExtractor.from_pretrained(str(snapshot), map_location=settings.device)


def validate_snapshot(snapshot: Path) -> None:
    """Fail before SDK construction if a local snapshot could need Hub fallback."""
    if not snapshot.is_dir():
        raise RuntimeError(f"GLiNER checkpoint is not a local snapshot directory: {snapshot}")
    required = [
        snapshot / "config.json",
        snapshot / "encoder_config" / "config.json",
        snapshot / "tokenizer_config.json",
    ]
    missing = [str(path.relative_to(snapshot)) for path in required if not path.is_file()]
    has_weights = any(
        (snapshot / filename).is_file() for filename in ("model.safetensors", "pytorch_model.bin")
    )
    tokenizer_files = (
        "tokenizer.json",
        "vocab.txt",
        "spiece.model",
        "sentencepiece.bpe.model",
    )
    has_tokenizer = any((snapshot / filename).is_file() for filename in tokenizer_files)
    has_bpe_files = (snapshot / "vocab.json").is_file() and (snapshot / "merges.txt").is_file()
    if not has_weights:
        missing.append("model.safetensors or pytorch_model.bin")
    if not has_tokenizer and not has_bpe_files:
        missing.append("tokenizer.json or supported tokenizer vocabulary files")
    if missing:
        raise RuntimeError(
            f"Incomplete local GLiNER checkpoint {snapshot}; missing: {', '.join(missing)}"
        )
    try:
        config = json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        json.loads((snapshot / "encoder_config" / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read GLiNER checkpoint config files in {snapshot}") from exc
    architecture = config.get("architecture")
    if architecture not in {"boundary", "span"}:
        raise RuntimeError(f"Unsupported GLiNER checkpoint architecture: {architecture!r}")
