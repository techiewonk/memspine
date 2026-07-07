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


async def test_versionless_extractors_never_share_a_cache_bucket() -> None:
    """Regression: two extractors without prompt_version used to collide on
    the literal 'None' key and serve each other stale facts."""

    class Versionless:
        def __init__(self, value: str) -> None:
            self.value = value
            self.calls = 0

        async def extract(self, content: str) -> list[ExtractedFact]:
            self.calls += 1
            return [ExtractedFact(entity="e", attribute="a", value=self.value)]

    kv = MemoryKV()
    first, second = Versionless("from-first"), Versionless("from-second")
    await CachedExtractor(first, kv).extract("same content")
    result = await CachedExtractor(second, kv).extract("same content")
    assert second.calls == 1  # not served from first's cache
    assert result[0].value == "from-second"
