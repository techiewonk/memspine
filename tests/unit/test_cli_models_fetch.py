"""CLI: `memspine models fetch [--check]` (ADR-093) against a stubbed downloader."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import memspine.clients.weights.onnx_files as files
from memspine.cli import app
from memspine.clients.weights.onnx_catalogue import CATALOGUE, PinnedFile, spec_files

runner = CliRunner()
EMBED = "BAAI/bge-small-en-v1.5"
RERANK = "Xenova/ms-marco-MiniLM-L-6-v2"


def _content(repo: str, name: str) -> bytes:
    return f"{repo}/{name}".encode()


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake hub: downloads land in a fake HF cache layout that ``--check`` then reads."""
    import huggingface_hub.constants

    cache = tmp_path / "hf"
    cache.mkdir()
    state: dict[str, Any] = {"calls": [], "corrupt": set(), "cache": cache}
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(files, "_VERIFIED", {})

    def pin(spec: Any, name: str) -> PinnedFile:
        data = _content(spec.repo, name)
        return PinnedFile(name, len(data), hashlib.sha256(data).hexdigest())

    def download(repo_id: str, filename: str, **kwargs: Any) -> str:
        state["calls"].append((repo_id, filename, kwargs))
        root = Path(kwargs["cache_dir"] or cache)
        target = root / ("models--" + repo_id.replace("/", "--")) / "snapshots"
        target = target / kwargs["revision"] / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _content(repo_id, filename)
        if (repo_id, filename) in state["corrupt"]:
            data = b"!" * len(data)
        target.write_bytes(data)
        return str(target)

    monkeypatch.setattr(files, "pinned_file", pin)
    monkeypatch.setattr(files, "hf_download", download)
    return state


def test_fetch_template_base_prints_one_line_per_file(hub: dict[str, Any]) -> None:
    result = runner.invoke(app, ["models", "fetch", "--template", "base"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    spec = CATALOGUE[EMBED]
    assert len(lines) == len(spec_files(spec))
    assert lines[0] == (
        f"{EMBED} model_optimized.onnx sha256 ok "
        f"{len(_content(spec.repo, 'model_optimized.onnx'))} bytes"
    )
    assert {call[0] for call in hub["calls"]} == {spec.repo}
    assert all(call[2]["revision"] == spec.revision for call in hub["calls"])


def test_fetch_models_and_cache_dir(hub: dict[str, Any], tmp_path: Path) -> None:
    other = tmp_path / "other-cache"
    result = runner.invoke(
        app, ["models", "fetch", "--model", RERANK, "--model", EMBED, "--cache-dir", str(other)]
    )
    assert result.exit_code == 0, result.output
    assert {call[0] for call in hub["calls"]} == {CATALOGUE[RERANK].repo, CATALOGUE[EMBED].repo}
    assert all(call[2]["cache_dir"] == str(other) for call in hub["calls"])
    assert (other / "models--Xenova--ms-marco-MiniLM-L-6-v2").is_dir()


def test_check_verifies_cache_without_network(hub: dict[str, Any]) -> None:
    cold = runner.invoke(app, ["models", "fetch", "--model", RERANK, "--check"])
    assert cold.exit_code == 1
    assert "not in the Hugging Face cache" in cold.output
    assert hub["calls"] == []
    assert runner.invoke(app, ["models", "fetch", "--model", RERANK]).exit_code == 0
    calls = len(hub["calls"])
    warm = runner.invoke(app, ["models", "fetch", "--model", RERANK, "--check"])
    assert warm.exit_code == 0, warm.output
    assert len(hub["calls"]) == calls  # --check never downloads
    assert warm.output.count("sha256 ok") == len(spec_files(CATALOGUE[RERANK]))


def test_check_fails_on_a_corrupt_cached_file(hub: dict[str, Any]) -> None:
    assert runner.invoke(app, ["models", "fetch", "--model", RERANK]).exit_code == 0
    spec = CATALOGUE[RERANK]
    path = hub["cache"] / "models--Xenova--ms-marco-MiniLM-L-6-v2" / "snapshots"
    path = path / spec.revision / "tokenizer.json"
    path.write_bytes(b"#" * path.stat().st_size)
    result = runner.invoke(app, ["models", "fetch", "--model", RERANK, "--check"])
    assert result.exit_code == 1
    assert "FAILED" in result.output and "sha256" in result.output


def test_fetch_exits_nonzero_when_a_download_stays_corrupt(hub: dict[str, Any]) -> None:
    hub["corrupt"].add((CATALOGUE[RERANK].repo, "onnx/model.onnx"))
    result = runner.invoke(app, ["models", "fetch", "--model", RERANK])
    assert result.exit_code == 1
    forced = [c for c in hub["calls"] if c[2]["force_download"]]
    assert [c[1] for c in forced] == ["onnx/model.onnx"]  # one retry, then failure


def test_unknown_model_is_refused(hub: dict[str, Any]) -> None:
    result = runner.invoke(app, ["models", "fetch", "--model", "org/not-catalogued"])
    assert result.exit_code == 1
    assert "not in the pinned ONNX catalogue" in result.output
    assert hub["calls"] == []


def test_core_hash_template_has_nothing_to_fetch(hub: dict[str, Any], tmp_path: Path) -> None:
    cfg = tmp_path / "user.yaml"
    cfg.write_text("model:\n  embedding:\n    provider: hash\n", encoding="utf-8")
    result = runner.invoke(app, ["models", "fetch", "--template", "core", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "no onnx model slots" in result.output
