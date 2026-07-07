"""E3 extraction cache: prompt version is part of the key (N7)."""

from __future__ import annotations

from memspine.prompts.models import ExtractedFact
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedExtractor


class StubExtractor:
    def __init__(self, prompt_version: str = "extract@1") -> None:
        self.prompt_version = prompt_version
        self.calls = 0

    async def extract(self, content: str) -> list[ExtractedFact]:
        self.calls += 1
        return [ExtractedFact(entity="alice", attribute="mood", value=content)]


async def test_repeat_extraction_hits_cache() -> None:
    inner = StubExtractor()
    cached = CachedExtractor(inner, MemoryKV())
    first = await cached.extract("alice is happy")
    second = await cached.extract("alice is happy")
    assert inner.calls == 1
    assert cached.hits == 1 and cached.misses == 1
    assert [fact.model_dump() for fact in first] == [fact.model_dump() for fact in second]


async def test_prompt_upgrade_invalidates() -> None:
    kv = MemoryKV()
    inner = StubExtractor("extract@1")
    cached = CachedExtractor(inner, kv)
    await cached.extract("alice is happy")

    upgraded = StubExtractor("extract@2")
    cached_v2 = CachedExtractor(upgraded, kv)  # same KV, new prompt version
    await cached_v2.extract("alice is happy")
    assert upgraded.calls == 1  # miss — version participates in the key (N7)


async def test_different_content_misses() -> None:
    inner = StubExtractor()
    cached = CachedExtractor(inner, MemoryKV())
    await cached.extract("alice is happy")
    await cached.extract("alice is sad")
    assert inner.calls == 2
