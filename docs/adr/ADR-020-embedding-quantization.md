# ADR-020 — Embedding quantization + float rescore, Matryoshka, model2vec prefilter (E4)

**Status:** accepted · **Date:** 2026-07-08 · **Register:** D-54  
**Amended by:** [ADR-021](./ADR-021-lancedb-core-vector.md) — SQLite brute-force vector store and pure-Python int8/binary prefilter **removed**; E4 rescore is **LanceDB-native only** in current code.
**Amends:** the E4 note in the structure plan (Part B) — the `search_rescore`
"seam only / joins in Phase 6" text is now the real implementation.

## Context

E4 (plan Part B) called for "binary/int8 quantization + float rescore (~32×
storage cut, ~95% quality); Matryoshka truncation; static-embedding prefilter
(model2vec)". Until now only the *seams* existed: `EmbedderManifest` carried
`matryoshka_dims`/`quantization` fields and `VectorStore.search_rescore()` fell
back to plain `query()`. This ADR makes quantization real — initially in a SQLite
vector store (since removed by **ADR-021**), now **LanceDB-native** only — keeping
every change **additive and opt-in** — `profile="simple"` and every existing
config stay byte-identical when quantization is off.

## Decisions

### 1. Two-stage `search_rescore` (historical: SQLite store; current: LanceDB only)

> **ADR-021:** the SQLite brute-force store and pure-Python int8/binary prefilter
> described below were removed. **§6 (LanceDB rescore)** is the live implementation.

The core stays slim (D-03): no numpy. `services/vector/quantize.py` held the
primitives; `SQLiteVectorStore.search_rescore` (removed) ran:

1. **Prefilter** — rank every candidate on a cheap, COMMON `~[-1, 1]` scale.
   Stage 1a reads only the small codes (`SELECT record_id, quantized_vec,
   quantization` — NOT the float `vector`) for the `namespace/embedder/dim`
   group. A row coded under the active scheme (codes non-NULL, stored scheme ==
   active, byte length matches) is scored by:
   - `int8`: symmetric linear quantize each dim to `[-127, 127]` (the vectors
     are L2-normalized at upsert, so per-dim values live in `[-1, 1]` — a fixed
     calibration range, no per-vector min/max to store or drift), then
     `int8_dot / (127² · dim)` (monotonic with cosine, normalized to `~[-1, 1]`).
   - `binary`: one sign bit per dim, scored `(dim − 2·Hamming)/dim` (`1` at
     Hamming 0, `−1` at Hamming `dim`).
   Rows with NULL codes, a stale scheme, or a stale dim are float-fallback: stage
   1b reads the float `vector` for JUST that subset (`WHERE record_id IN …`) and
   scores a (truncated) float cosine, already `~[-1, 1]`. All three land on one
   scale, so coded and legacy rows rank fairly in a single merged list — no
   cohort is squeezed out (mixed tables never drop candidates).
2. **Oversample** — keep `max(top_k * RESCORE_OVERSAMPLE, top_k)` survivors
   (`RESCORE_OVERSAMPLE = 4`); a wider cheap scan buys back the recall a lossy
   prefilter would otherwise drop.
3. **Rescore** — stage 2 reads the float `vector` for the window ONLY
   (`WHERE record_id IN …`), scores exact float32 cosine → sort → `top_k`. In the
   common all-coded case stage 1 transferred only the small codes, so
   `search_rescore` reads strictly LESS than a plain `query()` (E4's purpose).

Quantization is lossy *on purpose*: the rescore restores full-precision
ranking, so the prefilter only has to be *directional*. On a controlled set the
quantized top-1 equals the exact-float top-1 (the "~95% quality" claim, tested).

### 2. Storage layout + migration 0009

The float32 `vector` column is **kept** — the rescore needs full precision.
Migration 0009 adds two **nullable** columns to `memory_embeddings`:
`quantized_vec` (the int8/binary codes of the optionally-truncated vector) and
`quantization` (the scheme string). The migration is **schema-only, NOT a
backfill**: it adds the columns and leaves every existing row's codes NULL. A
store constructed without quantization also writes NULL and never reads the
columns.

**Enabling quantization on a populated DB requires a `rebuild()`.** Adding the
columns does not code the old rows; they stay NULL until a projector
`rebuild()` replays the WRITE events and the `VectorProjector.upsert` path
(`upsert` → `_encode`) re-encodes each vector with the current settings. The
mixed-table fallback covers the interim: in stage 1 a row whose codes are NULL —
or whose stored scheme/dim no longer matches the active one (a config flip
without a rebuild) — is scored by a (truncated) float32 cosine fetched only for
that subset, on the SAME normalized `~[-1, 1]` scale as the coded rows. So a
mixed table never silently drops a candidate, and once the rebuild completes
stage 1 reads only the small codes.

### 3. Matryoshka truncation

When the manifest declares `matryoshka_dims`, the store truncates to the
**smallest** declared prefix dim (max prefilter savings) and re-normalizes for
the prefilter stage only; the rescore still uses the full-dim float `vector`.
Storing the full vector (not a truncated copy) keeps the `dim`-scoped query
filter intact and lets the rescore stay exact. No-op when `matryoshka_dims` is
`None`.

### 4. Manifest-driven, with an explicit config override (default OFF)

Activation is resolved in `Engine._rescore_settings`:

- `vector.quantization: "auto"` (default) reads `manifest.quantization` — the
  default embedders (hash, fastembed) declare `None`, so the exact float32 path
  is unchanged.
- `"none"` forces off even when the manifest declares a scheme.
- `"int8"` / `"binary"` force that scheme even for an embedder that does not
  declare one (a deployer opting a known-tolerant model in).

Matryoshka is manifest-only (the model must be trained for it). `Engine.search`
dispatches to `search_rescore` **only when active**, at the vector leg *before*
fusion (hybrid RRF), the E1 quarantine/status gate, MMR, and rerank compose over
the candidates — so rescore refines the vector leg without perturbing any later
stage. When inactive it calls exactly the pre-E4 `query()` — the byte-identical
guard.

### 5. model2vec static-embedding prefilter behind `[static]`

`services/embedding/static_local.py::StaticEmbedder` lazily imports model2vec (a
distilled static lookup table — no torch, pure-numpy inference, CPU-fast). Two
surfaces, two failure modes (consistent with the codebase):

- as the **embedder provider** (`embedding.provider: "static"`) a missing extra
  hard-fails with `MissingServiceError(extra="static")` (D-10) — the deployer
  chose it;
- as the opt-in **prefilter stage** (`read.static_embedding_prefilter`) two
  failure modes: a genuinely absent extra (`MissingServiceError`/`ImportError`
  at construction) **skip-logs once and STICKY-disables** the stage for the
  process (it can never work); a transient error (weight download, a model2vec
  vector-count mismatch) **skip-logs and skips THIS search only** — not sticky,
  so a later search retries. Either way retrieval degrades to the unfiltered set,
  it never crashes (mirrors the E8 rerank stage). Default off.

**Naming disclosure (as with D-53 for E8):** despite "prefilter", this stage
runs POST-vector-retrieval and POST-fusion — it is a candidate-*narrowing* gate
over the already-retrieved (and, under hybrid, already-RRF-fused) set, applied
before the expensive rerank/score, NOT a true pre-vector prefilter that would
cut the vector search itself. It cannot resurface content the E1 status/quarantine
gate already dropped; it only reorders/narrows live candidates.

### 6. LanceDB rescore via its native quantized index (IMPLEMENTED)

The pure-Python int8/binary codes are a SQLite-store feature; LanceDB owns
quantization natively, so its `search_rescore` realizes E4 the **idiomatic Lance
way** rather than replicating those codes. When the manifest/override declares a
scheme (resolved by the same `Engine._rescore_settings`, now passed through to
`LanceDBVectorStore(quantization=…, matryoshka_dim=…, oversample=…)`), the store:

1. **Lazily builds a native ANN index with a compressed sub-index** on first
   active `search_rescore` — `int8` → **IVF_HNSW_SQ** (scalar quantization: each
   dim to int8, HNSW graph over the IVF cells); `binary`/Matryoshka-only →
   **IVF_PQ** (product quantization). Both use `distance_type="cosine"` to match
   the `query()` metric. Built once, reused; never rebuilt per query (guarded by
   an `asyncio.Lock` + an `_index_ready` flag, and it adopts a pre-existing index
   persisted from a prior run via `list_indices()`).
2. **Queries with `.nprobes(LANCE_NPROBES).refine_factor(RESCORE_OVERSAMPLE)`** —
   LanceDB's native two-stage flow: search the compressed index (probing
   `nprobes` IVF cells), then re-rank the `refine_factor`×`limit` oversampled
   window by **exact** vector distance. This *is* the "quantized prefilter →
   float rescore" of E4, done in-engine. Scores stay `1 − cosine_distance`,
   identical to `query()`.

**Row-count floor / flat fallback.** IVF/PQ trains a codebook by k-means over the
corpus and needs ≥ `LANCE_ANN_MIN_ROWS` (**256**, the PQ-centroid minimum at
`num_bits=8`; below it `create_index` raises "Not enough rows to train PQ"). So
the store guards on `count_rows()`: under the threshold it **skip-logs once**
(`vector.lance_index_deferred`) and runs a **flat exact query** — retried as the
corpus grows. A genuine `create_index` failure at/above the threshold sticky-
disables the ANN path for the process (`vector.lance_index_failed`, the §5
precedent) and likewise degrades to flat exact search. The default (no
quantization/Matryoshka) path never touches any of this: `search_rescore` returns
`query()` directly — no index requirement, byte-identical to the pre-E4 behavior.

**Matryoshka at the Lance layer** is *not* applied by truncating query/stored
vectors here: the per-embedder table stores full-dim vectors and the native
IVF/PQ compressed sub-index already provides the cheap-prefilter → exact-refine
two-stage. An embedder that genuinely emits a truncated prefix presents a smaller
`dim` upstream and therefore a smaller table; declaring `matryoshka_dims` simply
opts the Lance store into building an IVF_PQ index (whose PQ compression delivers
the storage/speed win) with the full-dim vectors as the exact-refine source.

The engine no longer disables rescore for the Lance backend (the old
`vector.rescore_unsupported` skip-log is removed); `_rescore_active` stays as
resolved and `Engine.search` dispatches to the native path, with its existing
`vector.rescore_failed` try/except as a final backstop to `query()`.

**Remaining limitation:** below `LANCE_ANN_MIN_ROWS` there is no ANN index to
rescore against, so a small Lance corpus runs an exact flat query (correct, just
without the compressed-index speed/storage win) until it grows past the floor.

## Consequences

- New module `services/vector/quantize.py`; new extra `[static]`
  (`model2vec>=0.3`); new constants `RESCORE_OVERSAMPLE`,
  `STATIC_PREFILTER_KEEP_MULTIPLIER`.
- New config: `vector.quantization` (default `"auto"`),
  `read.static_embedding_prefilter` (default `False`), `embedding.provider:
  "static"`.
- `SQLiteVectorStore.__init__` gains keyword-only `quantization`,
  `matryoshka_dim`, `oversample` (all defaulting to the inert path).
- Migration 0009 (`memory_embeddings.quantized_vec` + `quantization`), mirrored
  in `schema.py` metadata so `:memory:` `create_all` and the Alembic path agree.
- The default retrieval path is unchanged: existing vector/search tests pass
  untouched, and `search_rescore == query` when inactive.

## Alternatives rejected

- **Quantizing without a float rescore** — trades the ~95% quality claim for raw
  int8/binary ranking; the rescore is what makes lossy prefiltering safe.
- **Storing only the quantized vector (dropping float32)** — cuts storage more
  but makes an exact rescore impossible and an event-log rebuild lossy; the
  float32 stays authoritative, the codes are a derived prefilter index.
- **A separate quantized-vector table** — a nullable column on the existing
  projection is simpler, keeps the per-record row atomic, and the default path
  pays only two NULLs.
- **numpy in core for the quantization math** — violates D-03 (slim core);
  pure-Python int8/binary over already-normalized vectors is fast enough for the
  brute-force store, and LanceDB is the scale path anyway.
- **Making the static prefilter crash when `[static]` is missing** — a *stage*
  must degrade, not fail (E8 precedent); only the explicit *provider* choice
  hard-fails.
- **Choosing the largest Matryoshka dim** — gives no prefilter savings; the
  smallest prefix maximizes the speed/storage win and the rescore recovers
  precision.
