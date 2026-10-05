# ADR-051: Deterministic tie-breaks on the read path

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** master absorb list #87 (retrieval not repeatable run to run), found by the G8a
  sweep (`evals/prereg/G8a_wrapper_threshold.md`, Amendment 1). Builds on D-25 (RRF fusion) and
  the content tie-break already in `core.policies.assembly.ranked`.

## Context

Two identical offline runs of LoCoMo conv-26 (hash embedder, firewall-on config,
`PYTHONHASHSEED=0`) delivered different gold evidence: 3 of about 110 pairs. A per-question
comparison of two fresh engines showed more than that. In replay mode, 25 of 150 questions got a
different set of turns, and all 150 had the turns of a session in a different order.

Two causes, both a tie broken on `record_id`, which is a random uuid4:

1. **RRF fusion.** `rrf_fuse` broke equal fused scores on `record_id`. Equal scores are common:
   rank 3 + rank 5 scores the same as rank 5 + rank 3. At the `top_k` cut a different record won
   from run to run, and the replay window then carried a different 5 turns.
2. **Chronological order.** Context, replay windows, lead blocks and pipelines sorted records by
   `(valid_from, record_id)`. Every turn of a benchmark session shares the session's time, so
   the turns came out in uuid order.

The legs themselves were identical across runs in the reproduction. The firewall's neighbour
signal uses scores only (#63), so its decisions did not depend on order.

## Decision

1. **No random id decides an order.** `core.records.chrono_key` = (event time, record time,
   content fingerprint, record id) replaces `(valid_from, record_id)` everywhere records are
   ordered or cut: engine read paths, `lead`, `temporal_query`, prospective triggers, list
   cards, the GR-4 edge context and session members in the pipelines, and the episodic timeline.
   The record id only separates byte-identical records with the same times.
2. **Record time is strictly increasing in a process** (`core.records.record_time`, the
   `recorded_at` default). A tie is bumped by 1 µs, so record time follows write order even for
   records built in the same clock tick.
3. **RRF ties break on the leg ranks** (vector leg first, absent = last), not on ids. No two
   records share a rank within one leg, so the order is a function of the legs alone.
4. **Leg cuts settle ties** (`core.ties.settle_ties`). Each vector and BM25 leg fetches one extra
   row. If the cut falls inside a run of equal scores, the fetch doubles until the run is whole,
   up to `LEG_TIE_FETCH_MAX_FACTOR` (8) x the cut. Each tied run is then ordered by write order
   (record time, then content fingerprint), so a store's own tie order (Tantivy segment layout,
   which background merges change; a multi-threaded scan) cannot change membership. Write order
   is what an exact flat scan of an append-only LanceDB table already returned, so legs whose
   order was stable keep it (the graph-leg-off read golden is unchanged). Without ties this
   costs one extra row per leg query and no lookups.
5. **The compose path** breaks equal fused scores by `chrono_key` of the record.

## Bounds

- A run of equal scores longer than 8 x the leg's cut keeps the store's order past the bound.
- Exact (flat) vector search is already the default: LanceDB builds an ANN index only when
  quantization or Matryoshka is configured. With an ANN index, ties are settled, but approximate
  recall can still differ if the index is rebuilt differently. Indexes are built once (k-means
  training on the rows present), so two runs that write the same rows in the same order get the
  same index. That is not guaranteed across LanceDB versions.
- Scoring terms that read the wall clock (recency, and decay in a sleep) make a read depend on
  when it runs. Determinism is promised for identical inputs at the same clock, or with those
  weights at 0 (as in the eval configs).

## Consequences

- No config key and no default changes. The golden guard is unchanged.
- The rendered order of same-time turns is now write order. Previously it was random.
- `tests/unit/test_read_determinism.py` builds two fresh engines (firewall-on config, 60 turns,
  4 sessions) and requires identical replay and retrieve contexts. Both tests failed before this
  change. Two fresh engines on LoCoMo conv-26 now agree on all 150 questions, in both the
  `messages` and `ingest` channels and with any hash seed.
