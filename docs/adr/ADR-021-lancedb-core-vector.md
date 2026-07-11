# ADR-021 — LanceDB as sole core vector store

**Status:** accepted · **Date:** 2026-07-09 (recorded 2026-07-10) · **Register:** amends D-03, D-09, D-54 · **Supersedes:** P1 SQLite brute-force vector fallback (structure plan v1.4)

## Context

P1 shipped a zero-dep **SQLite brute-force vector store** so `pip install memspine` (no extras) could retrieve semantically without the `[lance]` extra. That matched D-03 “slim core” at the time but duplicated two vector implementations (SQLite `memory_embeddings` table + LanceDB), complicated E4 quantization (pure-Python codes in SQLite vs LanceDB native IVF/PQ — ADR-020 §6), and diverged from the field default (LanceDB in EverMemOS, cognee, honcho, SimpleMem).

Operational evidence: maintaining parity between SQLite and LanceDB vector paths blocked ADR-021-scale simplification; the SQLite store was removed from the hot path once LanceDB proved stable in integration tests.

## Decision

1. **`lancedb>=0.13` is a core dependency** — no `[lance]` extra. One vector backend: `vector.backend: lance` (sole valid value in v0.1; `weaviate` reserved).
2. **Remove the SQLite brute-force vector store** — `memory_embeddings` vector projection columns may remain in schema for migration history (see `schema.py` NOTE(ADR-021)); the `SQLiteVectorStore` adapter is gone.
3. **E4 rescore is LanceDB-native** — `LanceDBVectorStore.search_rescore` uses IVF_HNSW_SQ / IVF_PQ + `refine_factor` (ADR-020 §6). No pure-Python int8/binary prefilter in core.
4. **Lexical default stays SQLite FTS5** — hybrid RRF fuses LanceDB vector hits with SQLite FTS5/BM25 (`read.lexical_provider: sqlite_fts5` default). Standalone Tantivy remains `[tantivy]` for non-default lexical configs (D-25 unchanged intent).
5. **`profile="simple"`** still uses hash/fastembed + LanceDB file-backed beside the SQLite db path (`{storage.path}.lance` or temp dir for `:memory:`).

## Consequences

- Positive: one vector code path; ecosystem comparison docs align with manifest; LanceDB native quantization is the scale path.
- Negative: core wheel pulls `lancedb` + transitive deps (pyarrow); “zero heavy deps” is amended to “no torch/transformers” (D-03 intent preserved).
- Follow-up: ~~remove stale `[lance]` references~~ done (2026-07-10 doc pass).

## Alternatives rejected

- **Keep SQLite fallback in core** — dual maintenance, split E4 story, misleading “slim” claim when two ANN paths exist.
- **`[lance]` extra with SQLite default** — prior v1.4 state; rejected after LanceDB integration maturity.
- **Weaviate as default** — prod swap-in stub only (M14).
