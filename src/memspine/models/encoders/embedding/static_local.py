"""model2vec static-embedding provider (E4, ``[static]`` extra, ADR-020).

Static embeddings are a distilled lookup table — no transformer forward pass, so
they embed on CPU orders of magnitude faster than fastembed at a quality cost.
E4 uses them as an OPT-IN prefilter: a cheap first pass narrows the candidate
set before the real embedder + float32 rescore do the precise work. Never the
production default (that is fastembed, D-08).

The extra is checked at construction (like the flashrank reranker) so a missing
``[static]`` fails fast with the D-10 fix-hint when chosen as a *provider*; as
an optional prefilter *stage* the engine catches that and skip-logs instead of
crashing (retrieval degrades, never fails). The model itself loads lazily on
first ``embed`` in a worker thread.

Offline artifact policy (RM-38). With ``local_files_only=True`` (or ``HF_HUB_OFFLINE``
set truthy in the environment) the adapter never fetches: a local directory is used as
is, a Hub id is resolved only from the local Hugging Face cache
(``snapshot_download(local_files_only=True)``), the snapshot is checked for the files
model2vec reads, and ``StaticModel.from_pretrained`` receives the resolved local path
only, so it has no Hub id to download. A missing artifact raises
:class:`StaticModelUnavailableError` naming the model and the policy, never a silent
network fallback. Without the policy the default (online) load is unchanged.
"""

from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from typing import Any

from memspine.exceptions import MemspineError, MissingServiceError
from memspine.models.encoders.embedding.base import EmbedderManifest
from memspine.models.runtime.shared_weights import SharedWeights
from memspine.providers.shared import ResourceRegistry

__all__ = ["STATIC_WEIGHTS_CATEGORY", "StaticEmbedder", "StaticModelUnavailableError"]

#: Registry category of model2vec weights: the configured static embedder and the E4
#: prefilter build the same key for the same model, so they share one load (RM-73).
STATIC_WEIGHTS_CATEGORY = "model.embedding.static"

#: Default distilled static model — small, CPU-only, no torch.
_DEFAULT_MODEL = "minishlab/potion-base-8M"


#: Files model2vec reads from a snapshot; all must be local before an offline load.
_REQUIRED_FILES = ("config.json", "model.safetensors", "tokenizer.json")


class StaticModelUnavailableError(MemspineError):
    """The offline policy is on and the static model artifact is not available locally."""


def _env_offline() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_local_snapshot(model: str) -> Path:
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


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


class StaticEmbedder:
    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        dim: int | None = None,
        *,
        local_files_only: bool = False,
        registry: ResourceRegistry | None = None,
    ) -> None:
        try:
            import model2vec  # noqa: F401
        except ImportError as exc:
            raise MissingServiceError("services.embedding.static", extra="static") from exc
        self._model_name = model
        self._model: Any = None
        self._dim = dim or 0
        self._expected_dim = dim
        self._local_files_only = local_files_only
        # Weight key: the model and the offline policy; ``dim`` is a per-holder output check.
        self._weights: SharedWeights[Any] = SharedWeights(
            STATIC_WEIGHTS_CATEGORY,
            {"model": model, "local_files_only": local_files_only},
            None,
            self._load,
            warm=lambda loaded: loaded.encode(["warm-up"]),
            registry=registry,
        )

    @property
    def embedder_id(self) -> str:
        return f"static:{self._model_name}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        """The static embedder is itself the cheap stage, so it declares no
        further quantization/truncation — its vectors go straight to prefilter."""
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    @property
    def offline(self) -> bool:
        """Whether the no-download policy applies (setting or ``HF_HUB_OFFLINE``)."""
        return self._local_files_only or _env_offline()

    def _load(self) -> Any:
        from model2vec import StaticModel

        if self.offline:
            # Only a resolved local path reaches the loader: nothing is left to download.
            return StaticModel.from_pretrained(str(_resolve_local_snapshot(self._model_name)))
        return StaticModel.from_pretrained(self._model_name)

    async def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = await self._weights.get()
        return self._model

    async def warm(self) -> None:
        """Load the shared weights and encode a fixed short text once (ADR-079 warm hook)."""
        await self._weights.warm()

    async def aclose(self) -> None:
        """Release this holder's weight lease; another holder keeps them loaded."""
        self._model = None
        await self._weights.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = await self._ensure_model()

        def _run() -> list[list[float]]:
            return [_normalize([float(x) for x in vec]) for vec in model.encode(texts)]

        vectors = await asyncio.to_thread(_run)
        if vectors:
            actual_dim = len(vectors[0])
            if any(len(vector) != actual_dim for vector in vectors):
                raise ValueError("static embedder returned vectors with inconsistent dimensions")
            if self._expected_dim is not None and actual_dim != self._expected_dim:
                raise ValueError(
                    "static embedder output dimension differs from configured dimension"
                )
            self._dim = actual_dim
        return vectors
