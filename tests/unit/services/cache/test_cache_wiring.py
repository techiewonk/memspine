"""Engine cache wiring (Phase 2): one shared cache injected everywhere, built
from CacheConfig, reported by describe(), lifecycle-closed on stop()."""

from __future__ import annotations

from pathlib import Path

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.services.cache.base import MemoryKV


def _engine(**cfg: object) -> Engine:
    return Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        **cfg,  # type: ignore[arg-type]
    )


async def test_default_backend_is_shared_memory_kv() -> None:
    eng = _engine()
    await eng.start()
    try:
        assert eng.describe()["cache"] == "memory"
        assert isinstance(eng._cache, MemoryKV)
        # the ONE cache object is the one the embedding cache decorates.
        assert eng._embedder is not None
        assert eng._embedder._kv is eng._cache  # type: ignore[attr-defined]
    finally:
        await eng.stop()


async def test_shared_cache_serves_repeat_embeds() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write("the sky is blue today")
        await eng.search("the sky is blue today")  # embeds the same text again
        assert eng._embedder is not None
        assert eng._embedder.hits > 0  # served from the shared cache
    finally:
        await eng.stop()


async def test_disk_backend_wires_and_closes(tmp_path: Path) -> None:
    pytest.importorskip("cashews")
    from memspine.services.cache.cashews_cache import CashewsCache

    eng = _engine(cache={"backend": "disk", "path": str(tmp_path / "cache")})
    await eng.start()
    try:
        assert eng.describe()["cache"] == "disk"
        assert isinstance(eng._cache, CashewsCache)
        assert eng._cashews is not None and await eng._cashews.health()
    finally:
        await eng.stop()
    assert eng._cashews is not None and not await eng._cashews.health()  # closed on stop()


async def test_unknown_backend_raises_config_error() -> None:
    eng = _engine(cache={"backend": "bogus"})
    with pytest.raises(ConfigError, match=r"cache\.backend"):
        await eng.start()
    await eng.stop()
