"""model2vec static-embedding weights connector (RM-113, ``[static]`` extra).

Offline artifact policy (RM-38). With ``local_files_only=True`` (or ``HF_HUB_OFFLINE`` set
truthy in the environment) the connector never fetches: a local directory is used as is, a Hub
id is resolved only from the local Hugging Face cache (``snapshot_download(local_files_only=
True)``), the snapshot is checked for the files model2vec reads, and
``StaticModel.from_pretrained`` receives the resolved local path only, so it has no Hub id to
download. A missing artifact raises :class:`StaticModelUnavailableError` naming the model and
the policy, never a silent network fallback. Without the policy the default (online) load is
unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from memspine.clients.weights.shared import SharedWeights, WeightRegistry
from memspine.exceptions import MemspineError, MissingServiceError

__all__ = [
    "STATIC_WEIGHTS_CATEGORY",
    "StaticModelUnavailableError",
    "env_offline",
    "resolve_local_snapshot",
    "static_weights",
]

#: Registry category of model2vec weights: the configured static embedder and the E4
#: prefilter build the same key for the same model, so they share one load (RM-73).
STATIC_WEIGHTS_CATEGORY = "model.embedding.static"

#: Files model2vec reads from a snapshot; all must be local before an offline load.
_REQUIRED_FILES = ("config.json", "model.safetensors", "tokenizer.json")


class StaticModelUnavailableError(MemspineError):
    """The offline policy is on and the static model artifact is not available locally."""


def env_offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_local_snapshot(model: str) -> Path:
    """A local snapshot directory for ``model`` without any network access (RM-38)."""
    path = Path(model).expanduser()
    if path.is_dir():
        snapshot = path
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:  # pragma: no cover - huggingface_hub ships with model2vec
            raise StaticModelUnavailableError(
                f"static model {model!r}: an offline load needs huggingface_hub "
                "to read the local cache"
            ) from exc
        try:
            snapshot = Path(snapshot_download(repo_id=model, local_files_only=True))
        except Exception as exc:  # LocalEntryNotFoundError and friends: not cached
            raise StaticModelUnavailableError(
                f"static model {model!r} is not in the local Hugging Face cache and the "
                "offline policy forbids downloads (local_files_only / HF_HUB_OFFLINE); "
                "place the artifact locally first"
            ) from exc
    missing = [name for name in _REQUIRED_FILES if not (snapshot / name).is_file()]
    if missing:
        raise StaticModelUnavailableError(
            f"static model snapshot {snapshot} is incomplete (missing {', '.join(missing)}); "
            "the offline policy forbids fetching the rest"
        )
    return snapshot


def static_weights(
    model: str, *, local_files_only: bool = False, registry: WeightRegistry | None = None
) -> SharedWeights[Any]:
    """A lease on ``model2vec.StaticModel``; the extra is checked here (fail fast, D-10)."""
    try:
        import model2vec  # noqa: F401
    except ImportError as exc:
        raise MissingServiceError("services.embedding.static", extra="static") from exc

    def load() -> Any:
        from model2vec import StaticModel

        if local_files_only or env_offline():
            # Only a resolved local path reaches the loader: nothing is left to download.
            return StaticModel.from_pretrained(str(resolve_local_snapshot(model)))
        return StaticModel.from_pretrained(model)

    # Weight key: the model and the offline policy; ``dim`` is a per-holder output check.
    return SharedWeights(
        STATIC_WEIGHTS_CATEGORY,
        {"model": model, "local_files_only": local_files_only},
        None,
        load,
        warm=lambda loaded: loaded.encode(["warm-up"]),
        registry=registry,
    )
