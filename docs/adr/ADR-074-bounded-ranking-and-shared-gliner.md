# ADR-074: Bounded ranking and engine-owned GLiNER resources

**Status:** Accepted
**Date:** 2026-10-18
**Deciders:** Owner requested parallel implementation with kernel-only benchmarks

## Decision

Allow the canonical RRF helper to return an exact, optional prefix after all
input legs have contributed their scores. Preserve full ordering by default.
Only the two graph expansion seed callers request a three-item prefix. The main
router and associative store keep their complete ranking because later logic
uses the tail. Use partial heap selection only above the measured conservative
crossover (1,024 fused IDs and a prefix smaller than one eighth of the pool).
Non-finite scores retain the original sort behavior. Tuning constants belong
in `config/constants.py`; this adds no provider setting or ranking heuristic.

Own one lazy GLiNER model pool per Engine and register its close operation with
the provider lifecycle before use. Decision classification, semantic entity
extraction and write prepass share model weights through that pool. They do not
share namespace-scoped extraction results. No process-global cache or separate
ingestion path is introduced. All optional framework imports and weight loading
remain lazy. Synchronous SDK operations run off the event loop with bounded
concurrency, and shutdown drains running operations before releasing models.

Use one strict `models.gliner` settings record: `model`, `revision`, `device`,
`local_files_only`, `tasks`, and `inference_concurrency`. Remove `decision.model`
without an alias. Consumers select capabilities from the same configured task
set, rather than making one model per task. Default to CPU, local files only,
entities plus classification, and one concurrent inference. Resolve a pinned
Hub snapshot before SDK dispatch because the inspected SDK drops Hub options
between config and model loading. Incomplete local artifacts must fail rather
than allowing a hidden remote fallback.

> **Superseded (settings shape only):** the single `models.gliner` record is replaced by
> named `models.profiles` plus `models.bindings` in
> [ADR-075](ADR-075-named-model-profiles-and-escalation.md), with no alias. The per-profile
> GLiNER settings keep the same fields; the shared Engine-owned pool decision stands.

## Consequences and verification

The same measured-kernel pass adopts bounded datasketch `update_batch` for
MinHash at eight or more unique tokens. Retain the original per-token loop
below that crossover. Use at most 65,536 token-by-permutation cells per batch
(or one token when the permutation count exceeds that cap). Each uint64
permutation matrix is thus bounded by `8 * max(65_536, num_perm)` bytes; the
library can hold more than one such matrix internally. Tokenization, unique
token semantics, seed, scheme, permutations and persisted signature bytes are
unchanged. Tests compare restored signatures and exact LSH candidate sets.
See [MinHash kernel results](../MINHASH_BATCH_KERNEL.md). This changes no dedup
threshold or retrieval-quality setting.

| Choice | Reason / boundary |
|---|---|
| Partial selection after accumulation | Preserves duplicate scores, extra legs, weights and rank tie breaks |
| Full main-router ranking | Preserves protected candidates, date boosts and balanced reranking |
| Engine-owned model weights | Coalesces repeated loads while preserving independent Engine lifecycles |
| Explicit offline policy | Missing local artifacts fail; downloads require caller opt-in |
| Model pool versus extraction cache | Sharing weights does not authorize sharing user data or assertions |
| Kernel-only measurements | No complete-engine latency, model quality or production SLO claim |

Focused tests cover exact ranking prefixes and safe callsites, fake model load
coalescing, effective settings, cancellation, concurrency and shutdown. SDK
policy tests and any actual offline loader check are recorded separately from
the fake ownership benchmark. Structured GLiNER candidates still require the
source-grounding contract in ADR-072; this resource change does not enable
record/relation assertion writing.

See [bounded ranking](../BOUNDED_TOPK_IMPLEMENTATION.md),
[shared GLiNER ownership](../GLINER_SHARED_RUNTIME.md), and
[the delivery register](../PROVIDER_TODO.md) for final results and limitations.
