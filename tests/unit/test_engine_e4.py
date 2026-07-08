"""E4 engine wiring (ADR-020): the vector leg runs the two-stage quantized
rescore only when active (manifest-driven or the vector.quantization override);
default OFF keeps the exact query() path. The model2vec static-embedding
prefilter self-disables (skip-log) when [static] is absent — never crashes.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.services.embedding.base import EmbedderManifest

_HAS_MODEL2VEC = importlib.util.find_spec("model2vec") is not None


class _FakeEmbedder:
    """Minimal embedder carrying only a manifest — for _rescore_settings."""

    def __init__(self, manifest: EmbedderManifest) -> None:
        self._manifest = manifest

    @property
    def manifest(self) -> EmbedderManifest:
        return self._manifest


def make_engine(**read: object) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        **({"read": read} if read else {}),  # type: ignore[arg-type]
    )


# ── _rescore_settings: manifest-driven with an explicit config override ───────


def test_rescore_settings_manifest_driven_and_override() -> None:
    eng = Engine(dotenv_path=None, storage={"path": ":memory:"})
    config = MemspineConfig()  # vector.quantization defaults to "auto"

    # auto reads the manifest: default (None) -> inactive.
    eng._embedder = _FakeEmbedder(EmbedderManifest("x", 4))  # type: ignore[assignment]
    assert eng._rescore_settings(config) == (None, None)

    # auto reads a declared int8 + matryoshka (smallest prefix dim chosen).
    eng._embedder = _FakeEmbedder(  # type: ignore[assignment]
        EmbedderManifest("x", 8, matryoshka_dims=(4, 8), quantization="int8")
    )
    assert eng._rescore_settings(config) == ("int8", 4)

    # explicit override forces a scheme even when the manifest declares none.
    forced = MemspineConfig(vector={"quantization": "binary"})
    eng._embedder = _FakeEmbedder(EmbedderManifest("x", 4))  # type: ignore[assignment]
    assert eng._rescore_settings(forced) == ("binary", None)

    # explicit "none" forces OFF even when the manifest declares int8.
    off = MemspineConfig(vector={"quantization": "none"})
    eng._embedder = _FakeEmbedder(  # type: ignore[assignment]
        EmbedderManifest("x", 4, quantization="int8")
    )
    assert eng._rescore_settings(off) == (None, None)


def test_rescore_settings_rejects_unknown_override() -> None:
    eng = Engine(dotenv_path=None, storage={"path": ":memory:"})
    eng._embedder = _FakeEmbedder(EmbedderManifest("x", 4))  # type: ignore[assignment]
    with pytest.raises(ConfigError):
        eng._rescore_settings(MemspineConfig(vector={"quantization": "bogus"}))


# ── default OFF: exact query() path, rescore inactive ─────────────────────────


async def test_default_engine_rescore_inactive() -> None:
    eng = make_engine()
    await eng.start()
    try:
        assert eng._rescore_active is False  # simple-profile guard
    finally:
        await eng.stop()


# ── int8 override ON: search still returns the exact float top-1 ──────────────


@pytest.fixture
async def int8_engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        vector={"quantization": "int8"},
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()
    yield eng
    await eng.stop()


async def test_int8_engine_matches_plain_search_top1(int8_engine: Engine) -> None:
    assert int8_engine._rescore_active is True
    for content in ("the sky is blue today", "green grass grows", "red apples fall"):
        await int8_engine.write(content)
    quantized = await int8_engine.search("the sky is blue today")

    plain = make_engine()
    await plain.start()
    try:
        for content in ("the sky is blue today", "green grass grows", "red apples fall"):
            await plain.write(content)
        exact = await plain.search("the sky is blue today")
    finally:
        await plain.stop()

    # Small record set (< oversample window) => rescore is exact: identical top-1.
    assert quantized[0][0].content == exact[0][0].content == "the sky is blue today"


# ── model2vec static prefilter: self-disables when [static] is absent ─────────


@pytest.mark.skipif(_HAS_MODEL2VEC, reason="[static] installed — the miss path cannot be exercised")
async def test_static_prefilter_skiplogs_when_extra_absent() -> None:
    eng = make_engine(static_embedding_prefilter=True)
    await eng.start()
    try:
        assert eng._static_prefilter_on is True
        await eng.write("alpha topic note")
        await eng.write("beta topic note")
        results = await eng.search("alpha")  # must not crash despite missing extra
        assert results  # retrieval degraded gracefully to the unfiltered set
        assert eng._static_unavailable is True  # stage self-disabled once
    finally:
        await eng.stop()


# ── M1: a TRANSIENT static-embed error is NOT sticky (a later search retries) ─


class _Doc:
    """Minimal stand-in for a candidate record (the prefilter reads .content)."""

    def __init__(self, content: str) -> None:
        self.content = content


class _RaiseOnceEmbedder:
    """Raises on the first embed (transient), succeeds afterwards."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient embed failure")
        return [[1.0, 0.0] for _ in texts]


class _BadCountEmbedder:
    """Returns ONE vector regardless of input length — a model2vec count
    mismatch that the zip(strict=True) unpack would otherwise turn into a crash."""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0]]


async def test_static_prefilter_transient_error_is_not_sticky() -> None:
    eng = make_engine()
    await eng.start()
    try:
        embedder = _RaiseOnceEmbedder()
        eng._static_embedder = embedder  # type: ignore[assignment]
        # top_k=1 → keep=4; 5 candidates trips the embed (len > keep).
        candidates = [(_Doc(f"note {i}"), 1.0) for i in range(5)]

        first = await eng._static_embedding_prefilter("q", candidates, top_k=1)  # type: ignore[arg-type]
        assert first == candidates  # degraded to the unfiltered set
        assert eng._static_unavailable is False  # NOT sticky-disabled on a transient error

        second = await eng._static_embedding_prefilter("q", candidates, top_k=1)  # type: ignore[arg-type]
        assert embedder.calls == 2  # the stage retried instead of staying disabled
        assert len(second) == 4  # succeeded this time → narrowed to keep
    finally:
        await eng.stop()


async def test_static_prefilter_vector_count_mismatch_degrades_no_crash() -> None:
    eng = make_engine()
    await eng.start()
    try:
        eng._static_embedder = _BadCountEmbedder()  # type: ignore[assignment]
        candidates = [(_Doc(f"note {i}"), 1.0) for i in range(5)]
        out = await eng._static_embedding_prefilter("q", candidates, top_k=1)  # type: ignore[arg-type]
        assert out == candidates  # zip-strict mismatch caught → unfiltered set, no raise
        assert eng._static_unavailable is False  # a count mismatch is transient, not sticky
    finally:
        await eng.stop()
