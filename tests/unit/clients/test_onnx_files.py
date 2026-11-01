"""ADR-093: pinned catalogue digests, cache lookup and fetch, with a fake cache (no network)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import memspine.clients.weights.onnx_files as files
from memspine.clients.weights.onnx_catalogue import (
    CATALOGUE,
    CATALOGUE_FILES,
    OnnxModelSpec,
    PinnedFile,
    catalogue_id,
    pinned_file,
    spec_files,
)
from memspine.exceptions import MissingServiceError, WeightIntegrityError

RERANK = "Xenova/ms-marco-MiniLM-L-6-v2"
SPEC = OnnxModelSpec(
    "org/fake-onnx", "0" * 40, "onnx/model.onnx", "score", False, 16, 1, certified=False
)
GOOD = {name: f"bytes of {name}".encode() for name in spec_files(SPEC)}


def _pin(name: str, data: bytes) -> PinnedFile:
    return PinnedFile(name, len(data), hashlib.sha256(data).hexdigest())


def _snapshot(cache: Path, spec: OnnxModelSpec) -> Path:
    folder = "models--" + spec.repo.replace("/", "--")
    return cache / folder / "snapshots" / spec.revision


def _populate(cache: Path, spec: OnnxModelSpec, contents: dict[str, bytes]) -> None:
    root = _snapshot(cache, spec)
    for name, data in contents.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import huggingface_hub.constants

    root = tmp_path / "hf"
    root.mkdir()
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(root))
    monkeypatch.setattr(files, "_VERIFIED", {})
    monkeypatch.setattr(files, "pinned_file", lambda spec, name: _pin(name, GOOD[name]))
    return root


def test_every_catalogue_file_is_pinned() -> None:
    assert set(CATALOGUE_FILES) == set(CATALOGUE)
    for model, spec in CATALOGUE.items():
        pins = {pin.filename for pin in CATALOGUE_FILES[model]}
        assert pins == set(spec_files(spec)), model
        assert catalogue_id(spec) == model
        for name in spec_files(spec):
            pin = pinned_file(spec, name)
            assert pin is not None and len(pin.sha256) == 64 and pin.size > 0
    minilm = pinned_file(CATALOGUE[RERANK], "onnx/model.onnx")
    assert minilm is not None
    assert minilm.sha256 == "c623d0bcb99f4622beb413eaef00cfbe5db20df9f1dd982da4b4f26022881870"


def test_pinned_file_rejects_malformed_digest() -> None:
    with pytest.raises(ValueError, match="sha256"):
        PinnedFile("x", 1, "ABC")


def test_verify_good_file(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weights")
    files.verify_file(path, _pin("model.onnx", b"weights"), model="m")


def test_verify_corrupt_file_same_size(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weightz")
    with pytest.raises(WeightIntegrityError, match="sha256") as err:
        files.verify_file(path, _pin("model.onnx", b"weights"), model="m")
    assert "memspine models fetch --model m" in str(err.value)
    assert isinstance(err.value, MissingServiceError)  # D-10 handling applies


def test_verify_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"weig")
    with pytest.raises(WeightIntegrityError, match="4 bytes, pinned 7"):
        files.verify_file(path, _pin("model.onnx", b"weights"), model="m")


def test_cache_lookup_missing_and_present(cache: Path) -> None:
    assert files.missing_files(SPEC) == list(spec_files(SPEC))
    _populate(cache, SPEC, {"onnx/model.onnx": GOOD["onnx/model.onnx"]})
    assert "onnx/model.onnx" not in files.missing_files(SPEC)
    with pytest.raises(MissingServiceError, match="memspine models fetch"):
        files.require_cached("fake", SPEC)
    _populate(cache, SPEC, GOOD)
    files.require_cached("fake", SPEC, verify=True)


def test_require_cached_verify_rejects_corrupt_cache(cache: Path) -> None:
    _populate(cache, SPEC, {**GOOD, "tokenizer.json": b"tampered tokenizer file"})
    with pytest.raises(WeightIntegrityError, match="tokenizer.json"):
        files.require_cached("fake", SPEC, verify=True)


def test_check_only_never_downloads(cache: Path) -> None:
    def downloader(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("check must not download")

    with pytest.raises(MissingServiceError):
        files.fetch_model("fake", SPEC, check_only=True, downloader=downloader)
    _populate(cache, SPEC, GOOD)
    reports = files.fetch_model("fake", SPEC, check_only=True, downloader=downloader)
    assert [r.status for r in reports] == ["ok"] * len(GOOD)
    assert (
        reports[0].line() == f"fake onnx/model.onnx sha256 ok {len(GOOD['onnx/model.onnx'])} bytes"
    )


def test_fetch_redownloads_a_corrupt_file_once(cache: Path, tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []
    store = tmp_path / "dl"

    def downloader(repo_id: str, filename: str, **kwargs: Any) -> str:
        assert kwargs["revision"] == SPEC.revision and kwargs["local_files_only"] is False
        calls.append((filename, kwargs["force_download"]))
        target = store / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        corrupt = filename == "tokenizer.json" and not kwargs["force_download"]
        target.write_bytes(b"x" * len(GOOD[filename]) if corrupt else GOOD[filename])
        return str(target)

    reports = files.fetch_model("fake", SPEC, downloader=downloader)
    assert len(reports) == len(GOOD)
    assert ("tokenizer.json", True) in calls
    assert sum(force for _, force in calls) == 1


def test_fetch_fails_when_the_download_stays_corrupt(cache: Path, tmp_path: Path) -> None:
    def downloader(repo_id: str, filename: str, **kwargs: Any) -> str:
        target = tmp_path / filename.replace("/", "_")
        target.write_bytes(b"bad")
        return str(target)

    with pytest.raises(WeightIntegrityError):
        files.fetch_model("fake", SPEC, downloader=downloader)


def test_loader_refuses_a_corrupt_cached_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real catalogue pins: a cached MiniLM model of the wrong size is never loaded."""
    import huggingface_hub

    from memspine.clients.weights.onnx import OnnxModel

    corrupt = tmp_path / "model.onnx"
    corrupt.write_bytes(b"not an onnx graph")
    monkeypatch.setattr(files, "_VERIFIED", {})
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda *a, **k: str(corrupt))
    with pytest.raises(WeightIntegrityError, match="onnx/model.onnx") as err:
        OnnxModel(CATALOGUE[RERANK])
    assert f"--model {RERANK}" in str(err.value)


def test_loader_is_cache_only_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    from memspine.clients.weights.onnx import OnnxModel

    seen: list[bool] = []

    def missing(repo: str, filename: str, **kwargs: Any) -> str:
        seen.append(kwargs["local_files_only"])
        raise RuntimeError("not cached")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", missing)
    with pytest.raises(FileNotFoundError, match="memspine models fetch"):
        OnnxModel(CATALOGUE[RERANK])
    assert seen == [True]
