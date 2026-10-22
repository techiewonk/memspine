"""E4 model2vec StaticEmbedder (ADR-020): a missing [static] extra fails fast
with the D-10 fix-hint; the real path (extra installed) embeds deterministically.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import pytest

from memspine.exceptions import MissingServiceError
from memspine.models.encoders.embedding.static_local import (
    StaticEmbedder,
    StaticModelUnavailableError,
)

_HAS_MODEL2VEC = importlib.util.find_spec("model2vec") is not None


def test_missing_extra_raises_missing_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # M-BRANCH: simulate the absent [static] extra whatever this environment has installed.
    monkeypatch.setitem(sys.modules, "model2vec", None)
    with pytest.raises(MissingServiceError) as excinfo:
        StaticEmbedder()
    assert excinfo.value.extra == "static"  # names the pip install to run


# -- RM-38 offline artifact policy (stubbed model2vec; no network, no real weights) --------


class _FakeStaticModel:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[3.0, 4.0] for _ in texts]


@pytest.fixture
def stub_model2vec(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stub ``model2vec`` recording every ``from_pretrained`` argument."""
    loads: list[str] = []

    class StaticModel:
        @staticmethod
        def from_pretrained(name: str) -> _FakeStaticModel:
            loads.append(name)
            return _FakeStaticModel()

    module = types.ModuleType("model2vec")
    module.StaticModel = StaticModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "model2vec", module)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    return loads


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Blocks and records every connection attempt; the test teardown asserts none happened
    (recorded, not only raised, so a caller swallowing the error cannot hide a fetch)."""
    attempts: list[str] = []

    def _blocked(name: str) -> Callable[..., NoReturn]:
        def _raise(*_args: object, **_kwargs: object) -> NoReturn:
            attempts.append(name)
            raise OSError("network access attempted under the offline policy")

        return _raise

    monkeypatch.setattr(socket.socket, "connect", _blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", _blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", _blocked("getaddrinfo"))
    yield attempts
    assert attempts == [], f"network attempted: {attempts}"


def _snapshot(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.safetensors", "tokenizer.json"):
        (root / name).write_text("{}", encoding="utf-8")
    return root


async def test_offline_local_directory_hit_loads_the_path(
    stub_model2vec: list[str], no_network: list[str], tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path / "potion")
    embedder = StaticEmbedder(model=str(snapshot), dim=2, local_files_only=True)
    assert embedder.offline
    [vec] = await embedder.embed(["hello"])
    assert vec == pytest.approx([0.6, 0.8])
    assert stub_model2vec == [str(snapshot)]


async def test_offline_hub_id_resolves_from_the_local_cache_only(
    stub_model2vec: list[str],
    no_network: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import huggingface_hub

    snapshot = _snapshot(tmp_path / "cache" / "snap")
    calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)
    embedder = StaticEmbedder(model="org/static", dim=2, local_files_only=True)
    await embedder.embed(["hello"])
    assert calls == [{"repo_id": "org/static", "local_files_only": True}]
    assert stub_model2vec == [str(snapshot)]  # the loader never sees the Hub id


async def test_offline_missing_artifact_fails_visibly_without_fetching(
    stub_model2vec: list[str],
    no_network: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real huggingface_hub cache lookup, pointed at an empty cache, under a blocked network.
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-cache"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))
    embedder = StaticEmbedder(model="org/definitely-not-cached", dim=2, local_files_only=True)
    with pytest.raises(StaticModelUnavailableError, match="not in the local Hugging Face cache"):
        await embedder.embed(["hello"])
    assert stub_model2vec == []  # the loader was never reached


async def test_offline_incomplete_snapshot_fails_visibly(
    stub_model2vec: list[str], no_network: list[str], tmp_path: Path
) -> None:
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "config.json").write_text("{}", encoding="utf-8")
    embedder = StaticEmbedder(model=str(partial), dim=2, local_files_only=True)
    with pytest.raises(StaticModelUnavailableError, match="model.safetensors, tokenizer.json"):
        await embedder.embed(["hello"])
    assert stub_model2vec == []


async def test_hf_hub_offline_env_enables_the_policy(
    stub_model2vec: list[str],
    no_network: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-cache"))
    embedder = StaticEmbedder(model="org/not-cached", dim=2)
    assert embedder.offline
    with pytest.raises(StaticModelUnavailableError):
        await embedder.embed(["hello"])
    assert stub_model2vec == []


async def test_default_policy_keeps_the_online_load(stub_model2vec: list[str]) -> None:
    embedder = StaticEmbedder(model="org/static", dim=2)
    assert not embedder.offline
    await embedder.embed(["hello"])
    assert stub_model2vec == ["org/static"]


def test_settings_carry_the_offline_policy() -> None:
    from memspine.config.schema.providers import StaticEmbeddingSettings

    assert StaticEmbeddingSettings().local_files_only is False
    assert StaticEmbeddingSettings(local_files_only=True).local_files_only is True


@pytest.mark.skipif(not _HAS_MODEL2VEC, reason="[static] not installed")
async def test_static_embed_is_normalized_and_stable() -> None:
    embedder = StaticEmbedder()
    [a1] = await embedder.embed(["the sky is blue"])
    [a2] = await embedder.embed(["the sky is blue"])
    assert a1 == a2  # deterministic lookup table
    assert abs(sum(x * x for x in a1) - 1.0) < 1e-5  # L2-normalized
    assert embedder.embedder_id.startswith("static:")
    assert embedder.dim > 0
