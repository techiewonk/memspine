"""MD-02: embedding profiles pinned per store (P-17 C, M-23 B). Hash embedder + a fake profile."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.models.encoders.embedding.profiles import (
    CPU_MULTILINGUAL,
    GPU_QWEN3,
    EmbeddingProfile,
    EmbeddingProfileMismatch,
    check_or_pin,
    register_profile,
    resolve_profile,
)
from memspine.observability import tap

FAKE_A = EmbeddingProfile(name="fake_a", provider="hash", model_id="fake-a", dim=64)
FAKE_B = EmbeddingProfile(name="fake_b", provider="hash", model_id="fake-b", dim=64)


@pytest.fixture(autouse=True)
def _profiles() -> None:
    register_profile(FAKE_A)
    register_profile(FAKE_B)


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    return seen


def _engine(path: Path, **embedding: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path)},
        embedding={"provider": "hash", **embedding},
        read={"hybrid": False, "record_access": False},
        memories={"semantic": {"enabled": True}},
    )


def _pin_file(path: Path) -> Path:
    return Path(f"{path}.embedding-profile.json")


def test_profile_identity() -> None:
    assert FAKE_A.version_hash != FAKE_B.version_hash
    assert FAKE_A.version_hash == replace(FAKE_A, device="cpu").version_hash  # device not hashed
    assert FAKE_A.version_hash != replace(FAKE_A, query_instruction="q: ").version_hash
    assert GPU_QWEN3.model_id == "Qwen/Qwen3-Embedding-0.6B"
    assert (GPU_QWEN3.dim, GPU_QWEN3.dtype, GPU_QWEN3.device) == (1024, "bfloat16", "cuda")
    assert "Instruct:" in (GPU_QWEN3.query_instruction or "")
    with pytest.raises(ConfigError, match=r"unknown embedding.profile"):
        resolve_profile("nope")


def test_gpu_profile_applies_to_config() -> None:
    from memspine.config.schema.llm import EmbeddingConfig

    cfg = GPU_QWEN3.apply(EmbeddingConfig())
    assert (cfg.provider, cfg.model, cfg.dim) == ("st", "Qwen/Qwen3-Embedding-0.6B", 1024)
    assert (cfg.device, cfg.dtype) == ("cuda", "bfloat16")
    assert cfg.query_instruction == GPU_QWEN3.query_instruction


async def test_flag_off_unchanged(tmp_path: Path, events: list[Any]) -> None:
    db = tmp_path / "m.db"
    eng = _engine(db)
    await eng.start()
    await eng.write("the kayak is red", namespace="u", memory_type="semantic")
    await eng.stop()
    assert not _pin_file(db).exists()
    assert not [e for e in events if e[0].startswith("embed.")]


async def test_pinned_at_creation_and_survives_restart(tmp_path: Path, events: list[Any]) -> None:
    db = tmp_path / "m.db"
    eng = _engine(db, profile="fake_a", pin_profile=True)
    await eng.start()
    await eng.write("the kayak is red", namespace="u", memory_type="semantic")
    await eng.stop()
    pin = json.loads(_pin_file(db).read_text(encoding="utf-8"))
    assert pin["version_hash"] == FAKE_A.version_hash
    assert (pin["model_id"], pin["dim"], pin["dtype"]) == ("fake-a", 64, "float32")
    assert any(stage == "embed.profile" and f["profile"] == "fake_a" for stage, f in events)

    again = _engine(db, profile="fake_a", pin_profile=True)  # restart, same profile
    await again.start()
    hits = await again.search("kayak", namespace="u")
    await again.stop()
    assert hits
    assert json.loads(_pin_file(db).read_text(encoding="utf-8")) == pin


async def test_mismatch_refused_with_command(tmp_path: Path, events: list[Any]) -> None:
    db = tmp_path / "m.db"
    first = _engine(db, profile="fake_a", pin_profile=True)
    await first.start()
    await first.stop()
    other = _engine(db, profile="fake_b", pin_profile=True)
    with pytest.raises(EmbeddingProfileMismatch, match=r"VersionedSpaceStore.reembed"):
        await other.start()
    assert any(stage == "embed.profile_mismatch" for stage, _ in events)
    assert json.loads(_pin_file(db).read_text(encoding="utf-8"))["name"] == "fake_a"


async def test_migration_requires_versioned_spaces(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    first = _engine(db, profile="fake_a", pin_profile=True)
    await first.start()
    await first.stop()
    bad = _engine(db, profile="fake_b", pin_profile=True, migrate_profile=True)
    with pytest.raises(ConfigError, match="versioned_spaces"):
        await bad.start()


async def test_migration_replaces_pin(tmp_path: Path, events: list[Any]) -> None:
    db = tmp_path / "m.db"
    first = _engine(db, profile="fake_a", pin_profile=True, versioned_spaces=True)
    await first.start()
    await first.stop()
    moved = _engine(
        db, profile="fake_b", pin_profile=True, versioned_spaces=True, migrate_profile=True
    )
    await moved.start()
    await moved.stop()
    pin = json.loads(_pin_file(db).read_text(encoding="utf-8"))
    assert pin["name"] == "fake_b"
    assert pin["previous"] == FAKE_A.version_hash
    assert any(stage == "embed.profile_mismatch" and f.get("migrated") for stage, f in events)


def test_unprofiled_config_pins_its_own_fields(tmp_path: Path) -> None:
    path = str(tmp_path / "pin.json")
    assert check_or_pin(path, FAKE_A) == "pinned"
    assert check_or_pin(path, FAKE_A) == "ok"
    assert check_or_pin(None, FAKE_B) == "ok"
    assert check_or_pin(path, FAKE_B, migrate=True) == "migrated"


async def test_memory_store_pins_nothing() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash", "profile": "fake_a", "pin_profile": True},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    await eng.stop()


def _fastembed_cached(model: str) -> bool:
    base = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
        os.environ.get("TEMP", "/tmp"), "fastembed_cache"
    )
    hf = os.path.expanduser("~/.cache/huggingface/hub")
    stem = model.split("/")[-1].lower()
    for root in (base, hf):
        if os.path.isdir(root) and any(stem in d.lower() for d in os.listdir(root)):
            return True
    return False


@pytest.mark.skipif(
    not _fastembed_cached(CPU_MULTILINGUAL.model_id),
    reason="paraphrase-multilingual-MiniLM-L12-v2 is not in the local fastembed/HF cache",
)
async def test_cpu_multilingual_profile_resolves() -> None:
    from memspine.config.schema.llm import EmbeddingConfig
    from memspine.models.registry import build_embedder

    prof = resolve_profile("cpu_multilingual")
    emb = build_embedder(prof.apply(EmbeddingConfig()))
    vecs = await emb.embed(["Der Kayak ist rot", "the kayak is red"])
    assert len(vecs[0]) == prof.dim == emb.dim
