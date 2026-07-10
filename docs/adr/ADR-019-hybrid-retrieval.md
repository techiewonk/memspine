# ADR-019 — Hybrid retrieval: lexical BM25 port + RRF fusion (L1)

**Status:** accepted · **Date:** 2026-07-08 · **Register:** D-53
**Supersedes:** ADR-017 §5 (the amended-in-place hybrid note)
**Amended:** 2026-07-10 (v0.2 A3) — `read.hybrid` default flipped **OFF → ON**
(D-25's core-default intent). The lexical leg is now built by default; the
sqlite_fts5 provider rides the existing storage SQLite client, so the default
adds no dependency. `read.hybrid: false` restores the bit-identical vector-only
pipeline. All shipped templates use the sqlite backend, so the default is safe;
a postgres backend must pair the flip with `read.lexical_provider: tantivy`
(FTS5 is a SQLite virtual table).

## Context

D-25 called for a lexical/BM25 leg fused with the vector leg via reciprocal-rank
fusion (RRF), but P7 shipped it as a note amended into the already-accepted
ADR-017 (§5) that pointed at "the ADR-018 deferral" — a dangling reference, as
ADR-018 (security hardening) carries no lexical text. This ADR records the
hybrid-retrieval decision in its own place and folds in the L1 review hardening
(a 5-agent pass: python · security · silent-failure · blueprint · test-coverage).
Everything here is **additive and config-gated**: `profile="simple"` and every
existing config keep bit-identical behavior.

## Decisions

### 1. Lexical port + RRF via `read.hybrid` (default ON as of v0.2 A3; originally OFF)

`services/lexical` is a minimal namespace-scoped port (`index` / `search` /
`delete` / `clear` / `exists` / `close`) with `rrf_fuse` implemented **once** in
the port (D-25). The core default is a zero-dep SQLite **FTS5/BM25** store
(`SQLiteFTS5Lexical`); a standalone **Tantivy** adapter (`TantivyLexical`) is now
implemented behind the `[tantivy]` extra and selectable via
`read.lexical_provider: tantivy` (default `sqlite_fts5`). It satisfies the same
port — an in-RAM index for a `:memory:` event log (else an on-disk directory
`<db>.tantivy`), one long-lived synchronous `IndexWriter` wrapped in
`asyncio.to_thread` with serialized mutations, namespace isolation via a `Must`
term-query filter, and query safety by tokenizing user text into content
term-queries (never `parse_query`, so no field reference can cross namespaces).
The lexical index is a **rebuildable projection**
(`LexicalProjector`), registered in `engine._projectors` **only when
`read.hybrid` is on** — so `rebuild()` replays it, turning hybrid on for an
existing DB backfills the index from the log via catch-up, and off means no
store or projector is constructed. **Default-on is the intended v0.2 flip**,
held back in v0.1 for strict backward-compat.

### 2. RRF scores are normalized into [0, 1] before scoring

Raw RRF contributions are `1/(k+rank)` (≈0.016 at `k=60`, rank 1). Feeding them
raw into the M1 composite as `relevance` collapses the relevance term so
recency/importance dominate under hybrid. `Engine.search` now divides the fused
score by the theoretical max — a record ranked #1 in **both** legs scores
`2/(k+1)` — so fused relevance composes with recency/importance exactly like a
cosine similarity (rank-preserving; the rerank leg's `_minmax_normalize` does the
analogous job for cross-encoder logits). Each leg fetches `top_k *
LEXICAL_FETCH_MULTIPLIER` candidates before fusion and truncates to `top_k`
after, so a record just outside a single leg's window but strong when combined
can still surface.

### 3. NUL / control-char and length hardening

- **Content indexing** strips C0 control chars (NUL and friends, keeping
  tab/newline/CR) from the FTS projection of the content. A NUL truncates the
  SQLite bind and would raise mid-projector-chain — a **poison pill** that
  permanently blocks `catch_up`/`rebuild`. The record store keeps content
  verbatim; only its lexical projection is cleaned, so the lexical store accepts
  exactly what the record and vector projectors accept.
- **Query text** is stripped of control chars and bounded to
  `MAX_LEXICAL_QUERY_CHARS` **before** it becomes a cache key (no oversized-key
  retention) and reaches the parser; `sanitize_fts5_query` additionally caps the
  quoted-phrase count at `MAX_LEXICAL_QUERY_TERMS` (an unbounded OR-join parses
  super-linearly). The LIKE fallback caps both terms and rows scanned
  (`LEXICAL_LIKE_SCAN_MAX_ROWS`). REST `SearchRequest`/`AssembleRequest` add a
  matching `max_length`.
- **Defense in depth:** the lexical leg in `Engine.search` is wrapped in
  try/except — a broken lexical store logs `lexical.search_failed` and degrades
  to vector-only (fusing an empty leg preserves the vector ordering), it never
  crashes the whole search.

### 4. Cache freshness under concurrent mutation

The store's LRU corpus cache carries a monotonic **generation** counter bumped
on every mutation (index/delete/clear). A search captures the generation before
its DB call and stores its result **only if the generation is unchanged** when
the result returns — otherwise a mutation that raced between the await and the
cache-write would leave a now-stale result in the freshly-invalidated cache,
served indefinitely.

### 5. `verify_forget` inspects the lexical index

`memory_fts` holds raw content, so an M7 erasure proof that checked only
record/vector/log had a blind spot. `verify_forget` now queries the lexical
`exists(record_id)` when a lexical store is owned, reports `lexical_absent`, and
factors it into `clean` (`None` when no store is owned — nothing to erase, so it
cannot block a clean result).

### 6. Quarantined content in cleartext FTS — accepted tradeoff

The lexical index stores quarantined records' content in cleartext, indexed the
same as any other WRITE. This is **deliberate and safe**, not skipped at write:
promotion out of quarantine is a `DECAY_TRANSITION`, not a WRITE, so skipping
quarantined content at index time would break projection determinism (the
projector would have to re-index on a non-WRITE event). The **retrieval gate**
already excludes quarantined/non-ACTIVATED records from the fused candidates
(`Engine.search`), so held content never reaches a context window through the
lexical leg. The residual exposure is only to a direct reader of the DB file —
the same trust boundary that already sees the record store's cleartext content.

## Consequences

- `LexicalStore` gains `exists`; `SQLiteFTS5Lexical` implements it. New
  constants: `LEXICAL_FETCH_MULTIPLIER`, `MAX_LEXICAL_QUERY_TERMS`,
  `MAX_LEXICAL_QUERY_CHARS`, `LEXICAL_LIKE_SCAN_MAX_ROWS`.
- `verify_forget` returns an extra `lexical_absent` field.
- `Engine.search` raises `ValueError` on `top_k < 1` (was passed to SQLite
  `LIMIT`, where `-1` means unbounded — diverging from Python slicing).
- No new DDL or `EventKind`; migration 0008 (empty `memory_fts`) is unchanged.

## Alternatives rejected

- **Feeding raw RRF into the composite** — collapses relevance under hybrid (the
  bug this ADR fixes).
- **Skipping quarantined content at index time** — breaks projection determinism
  (promotion is a non-WRITE event); the retrieval gate is the correct control.
- **Rejecting a record whose content contains a NUL** — the record store accepts
  it; the lexical projection must not diverge or it becomes a poison pill.
- **Clamping `top_k` to 1 silently** — hides a caller bug; raising is honest and
  REST already guards `ge=1`.
