"""Pinned ONNX catalogue files: cache lookup, SHA-256 verification and explicit fetch (ADR-093).

Downloads happen only here (``memspine models fetch``) or in a load whose slot set
``download: true``; a normal start needs the files in the Hugging Face cache already.
Every catalogued file is checked against its pinned size and SHA-256 after download and
on load from cache; a file that differs raises :class:`WeightIntegrityError` and is never
handed to ONNX Runtime or the tokenizer.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from memspine.clients.weights.onnx_catalogue import (
    OnnxModelSpec,
    PinnedFile,
    pinned_file,
    spec_files,
)
from memspine.exceptions import MissingServiceError, WeightIntegrityError

__all__ = [
    "FETCH_COMMAND",
    "Downloader",
    "FileReport",
    "cached_file",
    "fetch_hint",
    "fetch_model",
    "hf_download",
    "missing_files",
    "require_cached",
    "sha256_file",
    "verify_file",
]

FETCH_COMMAND = "memspine models fetch"
_CHUNK = 1 << 20


def fetch_hint(model: str | None = None) -> str:
    """The D-10 remedy named by every missing-weights error."""
    if model is None:
        return f"run `{FETCH_COMMAND} --template <name>` first"
    return f"run `{FETCH_COMMAND} --model {model}` (or `{FETCH_COMMAND} --template <name>`) first"


class Downloader(Protocol):
    def __call__(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str,
        cache_dir: str | None,
        local_files_only: bool,
        force_download: bool,
    ) -> str: ...


def hf_download(
    repo_id: str,
    filename: str,
    *,
    revision: str,
    cache_dir: str | None,
    local_files_only: bool,
    force_download: bool,
) -> str:
    """The real downloader: ``hf_hub_download`` into the standard Hugging Face cache."""
    from huggingface_hub import hf_hub_download

    return str(
        hf_hub_download(
            repo_id,
            filename,
            revision=revision,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
            force_download=force_download,
        )
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# (resolved path, size, mtime_ns) -> digest of a file already verified in this process, so
# several weight registries loading one file hash it once. A rewritten file changes the key.
_VERIFIED: dict[tuple[str, int, int], str] = {}
_VERIFIED_LOCK = threading.Lock()


def verify_file(path: str | Path, pin: PinnedFile, *, model: str) -> None:
    """Raise :class:`WeightIntegrityError` unless ``path`` has ``pin``'s size and SHA-256."""
    resolved = Path(path).resolve()
    stat = resolved.stat()
    service = f"onnx weights {model}"
    if stat.st_size != pin.size:
        raise WeightIntegrityError(
            service,
            f"{pin.filename} at {resolved} is {stat.st_size} bytes, pinned {pin.size}",
            hint=fetch_hint(model),
        )
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    with _VERIFIED_LOCK:
        known = _VERIFIED.get(key)
    actual = known if known is not None else sha256_file(resolved)
    if actual != pin.sha256:
        raise WeightIntegrityError(
            service,
            f"{pin.filename} at {resolved} has sha256 {actual}, pinned {pin.sha256}",
            hint=fetch_hint(model),
        )
    with _VERIFIED_LOCK:
        _VERIFIED[key] = actual


def cached_file(spec: OnnxModelSpec, filename: str, cache_dir: str | None = None) -> Path | None:
    """The cached copy of ``filename`` at the pinned revision, without any network access."""
    from huggingface_hub import try_to_load_from_cache

    found = try_to_load_from_cache(spec.repo, filename, cache_dir=cache_dir, revision=spec.revision)
    return Path(found) if isinstance(found, str) and os.path.isfile(found) else None


def missing_files(spec: OnnxModelSpec, cache_dir: str | None = None) -> list[str]:
    """The files of ``spec`` absent from the cache (presence only; no hashing)."""
    return [name for name in spec_files(spec) if cached_file(spec, name, cache_dir) is None]


def require_cached(model: str, spec: OnnxModelSpec, *, verify: bool = False) -> None:
    """D-10: raise :class:`MissingServiceError` naming the fetch command when a file is
    missing; with ``verify`` also check each pinned digest (WeightIntegrityError)."""
    missing = missing_files(spec)
    if missing:
        raise MissingServiceError(
            f"onnx weights {model} ({', '.join(missing)} not in the Hugging Face cache)",
            hint=fetch_hint(model),
        )
    if verify:
        for name in spec_files(spec):
            pin = pinned_file(spec, name)
            path = cached_file(spec, name)
            if pin is not None and path is not None:
                verify_file(path, pin, model=model)


@dataclass(frozen=True)
class FileReport:
    model: str
    filename: str
    size: int
    sha256: str
    status: Literal["ok", "unpinned"]

    def line(self) -> str:
        state = "sha256 ok" if self.status == "ok" else "sha256 unpinned"
        return f"{self.model} {self.filename} {state} {self.size} bytes"


def fetch_model(
    model: str,
    spec: OnnxModelSpec,
    *,
    cache_dir: str | None = None,
    check_only: bool = False,
    downloader: Downloader | None = None,
    files: Sequence[str] | None = None,
) -> list[FileReport]:
    """Download (or, with ``check_only``, look up in the cache) and verify every file.

    A cached file whose digest differs is downloaded again once (``force_download``) before
    the fetch fails; ``check_only`` never touches the network and fails on any missing or
    differing file. Raises :class:`MissingServiceError` / :class:`WeightIntegrityError`."""
    download = downloader or hf_download
    reports: list[FileReport] = []
    for name in files if files is not None else spec_files(spec):
        pin = pinned_file(spec, name)
        if check_only:
            path = cached_file(spec, name, cache_dir)
            if path is None:
                raise MissingServiceError(
                    f"onnx weights {model} ({name} not in the Hugging Face cache)",
                    hint=fetch_hint(model),
                )
        else:
            path = Path(
                download(
                    spec.repo,
                    name,
                    revision=spec.revision,
                    cache_dir=cache_dir,
                    local_files_only=False,
                    force_download=False,
                )
            )
        if pin is None:
            reports.append(
                FileReport(model, name, path.stat().st_size, sha256_file(path), "unpinned")
            )
            continue
        try:
            verify_file(path, pin, model=model)
        except WeightIntegrityError:
            if check_only:
                raise
            path = Path(
                download(
                    spec.repo,
                    name,
                    revision=spec.revision,
                    cache_dir=cache_dir,
                    local_files_only=False,
                    force_download=True,
                )
            )
            verify_file(path, pin, model=model)
        reports.append(FileReport(model, name, pin.size, pin.sha256, "ok"))
    return reports
