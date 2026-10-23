"""MD-02 (P-17 C, M-23 B): embedding profiles and the per-store pin.

An :class:`EmbeddingProfile` names everything that makes two vectors comparable: provider, model
id, dimension, dtype, query instruction and normalisation. Its ``version_hash`` hashes exactly
those (device and batch size do not change the vectors, so they are not part of it).

Behind ``model.embedding.preset`` (default ``None``: the embedder config is used as written) the
profile fills the embedder config; behind ``model.embedding.pin_preset`` (default off) the resolved
profile is written next to the store at creation and checked on every open (the pin file format is
data and keeps its field names). A mismatch is refused with an error that names the migration
(``model.embedding.migrate_preset`` + the F-03 re-embed), never silently mixed into one vector
space.

Profiles: ``gpu`` is the evals default (Qwen3-Embedding-0.6B, bf16 on CUDA, 1024 dims, with the
retrieval instruction); ``cpu_multilingual`` is a small multilingual ONNX model that runs on the
CPU through fastembed. Register more with :func:`register_profile`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from typing import Any

from memspine.exceptions import ConfigError

__all__ = [
    "CPU_MULTILINGUAL",
    "GPU_QWEN3",
    "EmbeddingProfile",
    "EmbeddingProfileMismatch",
    "check_or_pin",
    "profile_names",
    "register_profile",
    "resolve_profile",
]

#: The query instruction the evals configs use for Qwen3-Embedding (evals/gen_qwen_stack_arms.py).
QWEN_QUERY_INSTRUCTION = (
    "Instruct: Given a question about a user's past conversations, "
    "retrieve memories that answer it\nQuery: "
)


class EmbeddingProfileMismatch(ConfigError):
    """A store was created with one embedding profile and is being opened with another."""


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str
    provider: str
    model_id: str
    dim: int
    dtype: str = "float32"
    query_instruction: str | None = None
    normalisation: str = "l2"
    #: Runtime placement, not part of the vector space (not hashed).
    device: str | None = None

    @property
    def version_hash(self) -> str:
        from memspine.store.events import fingerprint_payload

        return fingerprint_payload(
            {
                "provider": self.provider,
                "model": self.model_id,
                "dim": self.dim,
                "dtype": self.dtype,
                "query_instruction": self.query_instruction or "",
                "normalisation": self.normalisation,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model_id": self.model_id,
            "dim": self.dim,
            "dtype": self.dtype,
            "query_instruction": self.query_instruction,
            "normalisation": self.normalisation,
            "version_hash": self.version_hash,
        }

    def apply(self, cfg: Any) -> Any:
        """The nested provider config with this profile's fields filled in."""
        settings: dict[str, Any] = dict(cfg.settings)
        # The hash embedder has no model setting, but its vector size is native
        # provider configuration and must follow a selected profile.
        if self.provider != "hash":
            settings["model"] = self.model_id
        if self.provider in {"fastembed", "hash", "st", "litellm"}:
            settings["dim"] = self.dim
        if self.provider in {"fastembed", "st"}:
            settings["query_instruction"] = self.query_instruction
        if self.provider == "st":
            settings.update(
                device=self.device, dtype=None if self.dtype == "float32" else self.dtype
            )
        return cfg.model_copy(update={"provider": self.provider, "settings": settings})


GPU_QWEN3 = EmbeddingProfile(
    name="gpu",
    provider="st",
    model_id="Qwen/Qwen3-Embedding-0.6B",
    dim=1024,
    dtype="bfloat16",
    query_instruction=QWEN_QUERY_INSTRUCTION,
    device="cuda",
)

CPU_MULTILINGUAL = EmbeddingProfile(
    name="cpu_multilingual",
    provider="fastembed",
    model_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    dim=384,
)

_PROFILES: dict[str, EmbeddingProfile] = {p.name: p for p in (GPU_QWEN3, CPU_MULTILINGUAL)}


def register_profile(profile: EmbeddingProfile) -> None:
    _PROFILES[profile.name] = profile


def profile_names() -> list[str]:
    return sorted(_PROFILES)


def resolve_profile(name: str, *, device: str | None = None) -> EmbeddingProfile:
    try:
        profile = _PROFILES[name]
    except KeyError:
        raise ConfigError(
            f"unknown model.embedding.preset {name!r} (valid: {', '.join(profile_names())})"
        ) from None
    return replace(profile, device=device) if device else profile


def check_or_pin(path: str | None, profile: EmbeddingProfile, *, migrate: bool = False) -> str:
    """Pin ``profile`` at ``path`` on first open; on later opens require the same version hash.

    Returns ``"pinned"`` (written now), ``"ok"`` (matches), or ``"migrated"`` (``migrate`` was
    requested and the pin was replaced). ``path=None`` (an in-memory store: nothing outlives the
    process) pins nothing and returns ``"ok"``.
    """
    if path is None:
        return "ok"
    existing: dict[str, Any] | None = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    if existing is None:
        _write(path, profile.to_dict())
        return "pinned"
    if existing.get("version_hash") == profile.version_hash:
        return "ok"
    if not migrate:
        raise EmbeddingProfileMismatch(
            f"this store is pinned to embedding profile {existing.get('name')!r} "
            f"({existing.get('model_id')}, dim {existing.get('dim')}, "
            f"hash {existing.get('version_hash')}) but was opened with "
            f"{profile.name!r} ({profile.model_id}, dim {profile.dim}, "
            f"hash {profile.version_hash}). Vectors of different profiles are not "
            "comparable. Re-embed the store: set model.embedding.versioned_spaces=true and "
            "model.embedding.migrate_preset=true, then run VersionedSpaceStore.reembed(...) "
            "(F-03) for each namespace; or reopen it with the original profile."
        )
    _write(path, {**profile.to_dict(), "previous": existing.get("version_hash")})
    return "migrated"


def _write(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
    os.replace(tmp, path)
