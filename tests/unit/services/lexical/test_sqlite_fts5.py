"""SQLite FTS5 lexical store: BM25 ordering, namespace isolation, delete/clear
cascade, cache freshness, and FTS5 query-injection safety.

The BM25/occurrence ordering assertions hold on BOTH the FTS5 path and the
LIKE fallback (both rank by term frequency), so the suite is agnostic to
whether this build compiled FTS5.
"""

from __future__ import annotations

from memspine.clients.sqlite import SQLiteClient
from memspine.config.constants import MAX_LEXICAL_QUERY_CHARS, MAX_LEXICAL_QUERY_TERMS
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.lexical.sqlite_fts5 import (
    SQLiteFTS5Lexical,
    sanitize_fts5_query,
    strip_control_chars,
)
from memspine.services.storage.sqlite.schema import metadata


async def make_store() -> SQLiteFTS5Lexical:
    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return SQLiteFTS5Lexical(client)


def rec(record_id: str, namespace: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
    )


async def test_search_ranks_by_term_frequency_within_namespace() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "the quick brown fox"))
    await store.index(rec("r2", "ns/a", "quick quick quick"))
    await store.index(rec("r3", "ns/b", "quick in another namespace"))

    hits = await store.search("ns/a", "quick", top_k=5)
    ids = [hit.record_id for hit in hits]
    assert set(ids) == {"r1", "r2"}  # r3 is a different namespace — never leaks
    assert ids[0] == "r2"  # more occurrences → higher score
    assert hits[0].score >= hits[1].score


async def test_delete_removes_from_index() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha beta"))
    assert [h.record_id for h in await store.search("ns/a", "alpha")] == ["r1"]

    await store.delete("r1")
    assert await store.search("ns/a", "alpha") == []


async def test_reindex_is_idempotent_upsert() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha"))
    await store.index(rec("r1", "ns/a", "alpha"))  # replay
    hits = await store.search("ns/a", "alpha")
    assert [h.record_id for h in hits] == ["r1"]  # one row, not two


async def test_clear_then_reindex_parity() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "gamma delta"))
    await store.clear()
    assert await store.search("ns/a", "gamma") == []
    await store.index(rec("r1", "ns/a", "gamma delta"))
    assert [h.record_id for h in await store.search("ns/a", "gamma")] == ["r1"]


async def test_cache_reflects_new_writes() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "quick fox"))
    first = await store.search("ns/a", "quick")  # populates cache
    assert [h.record_id for h in first] == ["r1"]
    await store.index(rec("r2", "ns/a", "quick hare"))  # invalidates cache
    second = await store.search("ns/a", "quick")
    assert set(h.record_id for h in second) == {"r1", "r2"}


async def test_query_injection_is_neutralized() -> None:
    """A crafted query must be treated as data — never break MATCH syntax or
    reach another namespace. None of these may raise."""
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
    ):
        hits = await store.search("ns/a", hostile)
        assert all(h.record_id != "secret" for h in hits)  # never crosses namespace


async def test_empty_query_returns_no_hits() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "content"))
    assert await store.search("ns/a", "   ") == []
    assert await store.search("ns/a", "!!! ???") == []


def test_sanitizer_quotes_terms_and_drops_punctuation_only_tokens() -> None:
    assert sanitize_fts5_query("quick brown") == '"quick" OR "brown"'
    assert sanitize_fts5_query('ab"cd') == '"ab""cd"'  # embedded quote doubled
    assert sanitize_fts5_query("!!! ???") == ""  # nothing indexable


# ── F2: NUL / control chars never crash the index or search ───────────────────


async def test_nul_in_content_indexes_and_is_searchable() -> None:
    """A NUL in content must not raise (it would poison the projector chain);
    the record indexes cleanly with the control char stripped."""
    store = await make_store()
    await store.index(rec("r1", "ns/a", "pangolin\x00 dossier facts"))  # NUL bind hazard
    assert await store.exists("r1")
    hits = await store.search("ns/a", "dossier")
    assert [h.record_id for h in hits] == ["r1"]


async def test_nul_in_query_does_not_raise() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "pangolin dossier"))
    hits = await store.search("ns/a", "dossier\x00")  # NUL in query → stripped, no crash
    assert [h.record_id for h in hits] == ["r1"]


def test_strip_control_chars_keeps_whitespace() -> None:
    assert strip_control_chars("a\x00b\x07c") == "abc"
    assert strip_control_chars("a\tb\nc\rd") == "a\tb\nc\rd"  # tab/newline/CR kept


# ── F3: query-length DoS is bounded before parse and before caching ───────────


async def test_oversized_query_is_bounded_and_cache_key_is_not_huge() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "alpha"))
    huge = " ".join(["term"] * 50_000)  # ~250k chars, 50k tokens
    hits = await store.search("ns/a", huge)  # must return quickly, never blow up
    assert isinstance(hits, list)
    # The cache retains the BOUNDED query, never the 50k-term raw string.
    assert all(len(key[1]) <= MAX_LEXICAL_QUERY_CHARS for key in store._cache)


def test_sanitizer_caps_term_count() -> None:
    query = " ".join(f"t{i}" for i in range(1000))
    out = sanitize_fts5_query(query)
    assert out.count(" OR ") == MAX_LEXICAL_QUERY_TERMS - 1  # capped phrase count


def test_sanitizer_strips_control_chars() -> None:
    assert sanitize_fts5_query("a\x00b c") == '"ab" OR "c"'


# ── F4: a write racing the DB call must not seed a stale cache entry ───────────


async def test_stale_result_not_cached_when_write_races_the_db_call() -> None:
    store = await make_store()
    await store.index(rec("r1", "ns/a", "quick fox"))
    await store.search("ns/a", "warmup")  # forces _ensure so we know which path is live
    method = "_search_fts5" if store._fts5 else "_search_like"
    original = getattr(store, method)

    async def racing(namespace: str, query: str, top_k: int) -> list:
        result = await original(namespace, query, top_k)
        # A concurrent write lands while this search "awaited the DB": it bumps the
        # generation and clears the cache. The in-flight result is now stale.
        await store.index(rec("r2", "ns/a", "quick hare"))
        return result

    setattr(store, method, racing)
    hits = await store.search("ns/a", "quick")
    assert [h.record_id for h in hits] == ["r1"]  # the result we computed pre-write
    assert ("ns/a", "quick", 8) not in store._cache  # NOT cached — generation moved

    setattr(store, method, original)
    fresh = await store.search("ns/a", "quick")  # recomputed, never served stale cache
    assert {h.record_id for h in fresh} == {"r1", "r2"}


# ── F6: exists() backs verify_forget's lexical proof ──────────────────────────


async def test_exists_reflects_index_and_delete() -> None:
    store = await make_store()
    assert not await store.exists("r1")
    await store.index(rec("r1", "ns/a", "alpha"))
    assert await store.exists("r1")
    await store.delete("r1")
    assert not await store.exists("r1")
