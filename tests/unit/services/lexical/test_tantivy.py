"""Standalone Tantivy lexical adapter (D-25, **core** default): BM25 ordering,
namespace isolation, delete/clear+rebuild, NUL/injection safety, exists().

Tantivy is a core dependency (v0.2), so this always runs; ``importorskip`` is a
belt-and-braces guard for a broken build.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tantivy")

from memspine.config.constants import MAX_LEXICAL_QUERY_TERMS
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.lexical.tantivy import TantivyLexical, tokenize_content


def rec(record_id: str, namespace: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
    )


async def make_store() -> TantivyLexical:
    # index_path=None => in-RAM index (:memory: parity, no on-disk segments).
    return TantivyLexical()


async def test_search_ranks_by_term_frequency_within_namespace() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "the quick brown fox"))
    await store.index(rec("r2", "ns/a", "quick quick quick"))
    await store.index(rec("r3", "ns/b", "quick in another namespace"))

    hits = await store.search("ns/a", "quick", top_k=5)
    ids = [hit.record_id for hit in hits]
    assert set(ids) == {"r1", "r2"}  # r3 is a different namespace — never leaks
    assert ids[0] == "r2"  # more occurrences → higher BM25 score
    assert hits[0].score >= hits[1].score
    await store.close()


async def test_multi_term_query_is_or_joined() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha content"))
    await store.index(rec("r2", "ns/a", "beta content"))
    ids = {hit.record_id for hit in await store.search("ns/a", "alpha beta")}
    assert ids == {"r1", "r2"}  # OR recall: either term surfaces its record
    await store.close()


async def test_delete_removes_from_index() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha beta"))
    assert [h.record_id for h in await store.search("ns/a", "alpha")] == ["r1"]

    await store.delete("r1")
    assert await store.search("ns/a", "alpha") == []
    await store.close()


async def test_reindex_is_idempotent_upsert() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha"))
    await store.index(rec("r1", "ns/a", "alpha"))  # replay → one row, not two
    hits = await store.search("ns/a", "alpha")
    assert [h.record_id for h in hits] == ["r1"]
    await store.close()


async def test_index_many_single_commit() -> None:
    store = await make_store()
    await store.index_many(
        [rec("r1", "ns/a", "gamma"), rec("r2", "ns/a", "gamma delta"), rec("r3", "ns/b", "gamma")]
    )
    assert {h.record_id for h in await store.search("ns/a", "gamma")} == {"r1", "r2"}
    assert await store.index_many([]) is None  # empty batch is a no-op
    await store.close()


async def test_clear_then_reindex_parity() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "gamma delta"))
    await store.clear()  # rebuildable projection: wiped so replay starts clean
    assert await store.search("ns/a", "gamma") == []
    await store.index(rec("r1", "ns/a", "gamma delta"))
    assert [h.record_id for h in await store.search("ns/a", "gamma")] == ["r1"]
    await store.close()


async def test_query_injection_is_neutralized() -> None:
    """A crafted query is DATA — it must never break the query or cross into
    another namespace via a field reference. None of these may raise."""
    store = await make_store()
    await store.index(rec("r1", "ns/a", "harmless content about foxes"))
    await store.index(rec("secret", "ns/b", "cross namespace secret"))

    for hostile in (
        'foxes" OR namespace : "ns/b',
        "foxes) AND (content",
        "NEAR(foxes, 1)",
        "* foxes *",
        '"""',
        "foxes AND secret",
        "namespace:ns/b",
    ):
        hits = await store.search("ns/a", hostile)
        assert all(h.record_id != "secret" for h in hits)  # never crosses namespace
    await store.close()


async def test_empty_query_returns_no_hits() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "content"))
    assert await store.search("ns/a", "   ") == []
    assert await store.search("ns/a", "!!! ???") == []
    await store.close()


async def test_nul_in_content_indexes_and_is_searchable() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "pangolin\x00 dossier facts"))  # NUL bind hazard
    assert await store.exists("r1")
    assert [h.record_id for h in await store.search("ns/a", "dossier")] == ["r1"]
    await store.close()


async def test_nul_in_query_does_not_raise() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "pangolin dossier"))
    hits = await store.search("ns/a", "dossier\x00")  # NUL stripped, no crash
    assert [h.record_id for h in hits] == ["r1"]
    await store.close()


async def test_oversized_query_is_bounded() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha"))
    huge = " ".join(["term"] * 50_000)  # ~250k chars, 50k tokens
    hits = await store.search("ns/a", huge)  # must return quickly, never blow up
    assert isinstance(hits, list)
    await store.close()


async def test_exists_reflects_index_and_delete() -> None:
    store = await make_store()
    assert not await store.exists("r1")
    await store.index(rec("r1", "ns/a", "alpha"))
    assert await store.exists("r1")
    await store.delete("r1")
    assert not await store.exists("r1")
    await store.close()


def test_tokenize_content_matches_default_tokenizer_shape() -> None:
    assert tokenize_content("Quick Brown") == ["quick", "brown"]  # lower-cased runs
    assert tokenize_content("ns/b other") == ["ns", "b", "other"]  # split on '/'
    assert tokenize_content("!!! ???") == []  # nothing indexable
    assert len(tokenize_content(" ".join(f"t{i}" for i in range(1000)))) == MAX_LEXICAL_QUERY_TERMS


# ── target BM25 ranking ───────────────────────────────────────────────────────


async def test_term_frequency_dominates_ranking() -> None:
    """More occurrences of the query term → higher BM25 rank; ns filter honored."""
    corpus = [
        rec("r1", "ns/a", "the quick brown fox jumps"),
        rec("r2", "ns/a", "a lazy dog sleeps all day"),
        rec("r3", "ns/a", "quick quick quick reflexes"),
        rec("r4", "ns/b", "quick content in another namespace"),
    ]
    store = await make_store()
    for record in corpus:
        await store.index(record)
    hits = await store.search("ns/a", "quick", top_k=5)
    ids = {h.record_id for h in hits}
    assert ids == {"r1", "r3"}  # r4 (ns/b) excluded by the namespace filter
    assert hits[0].record_id == "r3"  # 3x "quick" ranks first
    await store.close()
