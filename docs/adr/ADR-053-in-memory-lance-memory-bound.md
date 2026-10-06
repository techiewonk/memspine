# ADR-053: Bound the committed memory of in-memory LanceDB tables

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** fix/lancedb-memory-leak. Builds on ADR-021 (LanceDB as the vector store) and
  the in-memory fast path (commits 2574328, e284ee9).

## Context

With `storage.path: ":memory:"` the vector table lives in a `memory://` LanceDB store. Ingesting
one LoCoMo conversation (hash embedder, dim 1024, `template="core"`) grew the process's private
(committed) memory by ~18 MB per write, to ~7.5 GB, while resident memory stayed ~240 MB. The
file-backed store peaked at ~0.8 GB. Measured in isolation with LanceDB 0.34:

- Every object Lance writes (data file, manifest, transaction file) goes through its object
  writer, which allocates a 5 MB upload buffer (`LANCE_INITIAL_UPLOAD_SIZE`, clamped to at least
  5 MB). The in-memory object store keeps that buffer, at full capacity, as the stored object. A
  single-row `add` therefore commits ~15 MB and a no-op `delete` ~10 MB. The pages are never
  touched, so RSS does not show it; Windows charges it against the commit limit.
- A `memory://` store keeps every table version, so nothing is ever freed. Our periodic
  compaction (`optimize()` every 20 upserts) used the default 7-day retention and freed nothing
  either.
- With old versions removed, ~5 MB per compaction was still retained: the connection's metadata
  cache keeps entries that slice the in-memory objects, which pins the whole 5 MB buffer while the
  cache counts only the entry's own size. The default cache is sized in gigabytes.

The root cause (buffer capacity kept by the in-memory object store) is inside LanceDB and cannot
be changed from our layer.

## Decision

1. **Drop old versions when an exclusive table compacts.** `LanceDBVectorStore._maybe_compact`
   calls `optimize(cleanup_older_than=timedelta(0))` when the store is `exclusive` (only the
   in-memory table is). No other writer can hold an older version of such a table. File-backed
   tables never compact (`compact_every=None`), so they are unchanged.
2. **Deletes count towards the compaction cadence**, like upserts, since each one also adds a
   version.
3. **Small metadata cache for `memory://` connections.** `LanceDBClient.connect` passes
   `lancedb.Session(metadata_cache_size_bytes=LANCE_MEMORY_METADATA_CACHE_BYTES)` (1 MiB) for a
   `memory://` path. The index cache keeps its default. File paths connect exactly as before.

Compaction keeps rows and their order, and removing old versions does not touch the latest one,
so query results are unchanged (`tests/unit/services/vector/test_lancedb_exclusive.py`,
`tests/integration/test_ingest_equivalence.py`).

## Results

LoCoMo conversation 0 ingest plus 5 reads, combo-A read config, hash embedder dim 1024, peak
private memory:

| store | before | after |
|---|---|---|
| `:memory:` | ~7.5 GB (measured 2026-10-06); passed 2.5 GB by session 7 of 19 in this run | 1.0 GB |
| file-backed | 0.8 GB | 0.8 GB |

A regression test (`tests/unit/services/vector/test_lancedb_memory_bound.py`) writes 2,000
vectors with deletes into an in-memory store and fails if private memory grows by more than
800 MB. Before the fix it fails after 41 writes. It is skipped where `psutil` reports no private
memory (non-Windows).

## Consequences

- Between two compactions up to `LANCE_COMPACT_EVERY` versions are alive, ~300 MB of committed
  buffers at most.
- An in-memory table keeps no history, so `history_absent` sees only the latest version. Deleted
  ids stay reported until `purge_deleted` runs, as before.
- **Not covered:** with quantization or Matryoshka active, the store does not compact (compaction
  would also fold new rows into the ANN index), so an in-memory table in that mode still commits
  ~15 MB per write. Those modes are opt-in; use a file-backed store for long ingests with them.
- If a later LanceDB version stops keeping the writer's buffer capacity, the cleanup and the small
  cache stay harmless.
