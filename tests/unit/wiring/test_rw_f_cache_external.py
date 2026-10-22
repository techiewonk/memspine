"""RW-F: R3-15 (read cache, parallel legs, extraction key), R3-13 (external v2); flag-off."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.prompts.models import ExtractedFact
from memspine.read.pipeline import request as rq
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedExtractor

_ENGINES: list[Engine] = []


@pytest.fixture(autouse=True)
async def _stop_engines() -> Any:
    yield
    while _ENGINES:  # no started Engine outlives its test (RM-75: live Engines are counted)
        await _ENGINES.pop().stop()


def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": False, "record_access": False, **read},
        memories={"episodic": {"enabled": True}},
    )
    _ENGINES.append(eng)
    return eng


async def _seed(eng: Engine) -> list[str]:
    await eng.start()
    await eng.ingest_messages(
        [
            {"role": "user", "content": "Ana booked the flight to Lyon"},
            {"role": "user", "content": "Ana packed the green bag"},
        ],
        namespace="a",
    )
    hits = await eng.search("Lyon flight", namespace="a")
    return [r.record_id for r, _ in hits]


def _spy(eng: Engine) -> list[int]:
    calls = [0]
    inner = eng._read

    async def counted(*a: Any, **k: Any) -> Any:
        calls[0] += 1
        return await inner(*a, **k)

    eng._read = counted  # type: ignore[method-assign]
    return calls


async def test_cache_off_by_default() -> None:
    eng = _engine()
    await _seed(eng)
    calls = _spy(eng)
    await eng.read("Lyon flight", namespace="a")
    await eng.read("Lyon flight", namespace="a")
    assert calls[0] == 2


async def test_cache_hit_then_write_invalidates() -> None:
    eng = _engine(cache={"enabled": True})
    await _seed(eng)
    calls = _spy(eng)
    first = await eng.read("Lyon flight", namespace="a")
    second = await eng.read("Lyon flight", namespace="a")
    assert calls[0] == 1
    assert [r.record_id for r in first.context.records] == [
        r.record_id for r in second.context.records
    ]
    second.context.records.clear()  # a caller mutation never reaches the cache
    third = await eng.read("Lyon flight", namespace="a")
    assert calls[0] == 1 and third.context.records
    await eng.ingest_messages([{"role": "user", "content": "Ana left Lyon"}], namespace="a")
    await eng.read("Lyon flight", namespace="a")
    assert calls[0] == 2  # store generation moved: stale, recomputed


async def test_cache_forget_never_serves_erased_record() -> None:
    eng = _engine(cache={"enabled": True})
    ids = await _seed(eng)
    first = await eng.read("Lyon flight", namespace="a")
    shown = {r.record_id for r in first.context.records}
    victim = next(iter(shown & set(ids)))
    await eng.forget(victim, namespace="a", hard=True)
    again = await eng.read("Lyon flight", namespace="a")
    assert victim not in {r.record_id for r in again.context.records}


async def test_cache_revalidation_drops_partial_hit() -> None:
    eng = _engine(cache={"enabled": True})
    await _seed(eng)
    seen: list[Any] = []
    real_get = rq._cache_get

    async def capture(*a: Any) -> Any:
        seen.append(a)
        return await real_get(*a)

    rq._cache_get = capture  # type: ignore[assignment]
    try:
        first = await eng.read("Lyon flight", namespace="a")
    finally:
        rq._cache_get = real_get  # type: ignore[assignment]
    assert first.context.records
    victim = first.context.records[0].record_id
    real = eng._storage.get_record

    async def gone(rid: str) -> Any:
        return None if rid == victim else await real(rid)

    eng._storage.get_record = gone  # type: ignore[method-assign]
    hit, _ = await real_get(*seen[0])  # same key: the record vanished with no generation bump
    assert hit is None  # POL-1: a partial revalidation is a miss


async def test_session_reads_are_not_cached() -> None:
    eng = _engine(cache={"enabled": True})
    await _seed(eng)
    calls = _spy(eng)
    await eng.read("Lyon flight", namespace="a", session_id="s1")
    await eng.read("Lyon flight", namespace="a", session_id="s1")
    assert calls[0] == 2


async def test_parallel_legs_same_results() -> None:
    seq = _engine()
    par = _engine(parallel_legs=True)
    await _seed(seq)
    await _seed(par)
    a = await seq.search("Lyon flight bag", namespace="a")
    b = await par.search("Lyon flight bag", namespace="a")
    assert [r.content for r, _ in a] == [r.content for r, _ in b]


async def test_extraction_key_carries_model_id() -> None:
    class Ext:
        prompt_version = "extract@1"

        def __init__(self, model: str) -> None:
            self.model_id = model
            self.calls = 0

        async def extract(self, content: str) -> list[ExtractedFact]:
            self.calls += 1
            return [ExtractedFact(entity="e", attribute="a", value=self.model_id)]

    kv = MemoryKV()
    one, two = Ext("m1"), Ext("m2")
    await CachedExtractor(one, kv).extract("same text")
    out = await CachedExtractor(two, kv).extract("same text")
    assert two.calls == 1 and out[0].value == "m2"  # same prompt, other model: no stale reuse
    await CachedExtractor(one, kv).extract("same text")
    assert one.calls == 1  # same model and prompt: served from cache


async def test_external_v2_block_is_labelled_and_not_stored() -> None:
    from memspine.services.external import ExternalSnippet

    class Prov:
        name = "fake"

        async def search(self, query: str, max_results: int) -> list[ExternalSnippet]:
            return [ExternalSnippet("Lyon", "Lyon is a city in France.", "https://a.org/Lyon")]

    eng = _engine(external_evidence="web", external_v2=True)
    eng.set_external_provider(Prov())
    await _seed(eng)
    before = len(await eng._storage.list_records("a"))
    res = await eng.read("What is a general fact about the city of Lyon in France?", namespace="a")
    block = [r for r in res.context.records if "ext:public" in r.tags]
    assert len(block) == 1
    assert block[0].content.startswith("[public]") and "never proves" in block[0].content
    assert len(await eng._storage.list_records("a")) == before  # never deposited


async def test_external_v2_needs_the_opt_in() -> None:
    # FU-REQS: external_v2 never enables lookups; without the E03 opt-in it is a config error
    eng = _engine(external_evidence="off", external_v2=True)
    with pytest.raises(ConfigError, match="external_evidence"):
        await eng.start()
