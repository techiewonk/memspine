# ADR-079: Keyed, shared and warmed provider resources

**Status:** Accepted (design); implementation tracked as RM-70..RM-77
**Date:** 2026-10-22
**Deciders:** Owner (RM-70 decisions, 2026-10-22)
**Supersedes:** the per-engine allocation rule in PROVIDER_ARCHITECTURE_PLAN.md R-19 and
section "connected clients must never be cached in global factories", and ADR-074's
"one GLiNER pool per Engine, no process-global cache".

## Context

The owner asked that clients be warmed up once and shared by every internal consumer.
A 2026-10-22 audit found that inside one Engine most clients are shared, but:

- Postgres opens up to four pools to one database (async, outbox, pgvector, sleep leases);
  Weaviate opens two clients to one target; each outbox runner builds its own broker.
- llama.cpp loads one copy per role; the static prefilter duplicates the static embedder;
  GLiNER can reload repeatedly at `gliner_max_models=1`; Resonate is rebuilt per activity.
- Embedder weights, Tantivy, rerankers, GLiNER and the decider load lazily inside the
  first request.
- Two Engines in one process never share warm resources, while some process globals
  (model scheduler, economy/ops ledgers, tap sink, concurrency gate) are unkeyed, so the
  last Engine overwrites the others.
- Separate worker processes each build a full Engine.

## Decision

1. **One per-process `ResourceRegistry`** owns connected clients and model weights.
   Key = (category, target identity digest, effective settings digest, event-loop
   identity for loop-bound async clients or `thread` for thread-safe native handles,
   process id). Identical effective resources are shared across all internal consumers
   and across Engines in the same process; anything that differs gets its own instance.
2. **Leases and refcounts.** Builders acquire a lease; the Engine registers the lease
   release with its existing `ProviderLifecycleManager`. The last release closes the
   resource. Stopping one Engine never closes a resource another Engine still holds.
   Concurrent first acquisitions coalesce into one build.
3. **Warm-up in the background after start.** `start()` returns once required clients are
   connected. A background task then warms model weights, index handles and rerankers.
   A request that needs a resource that is not warm yet waits for it; readiness reports
   warm state per resource. Warm-up failures surface through readiness and the normal
   error path, never silently.
4. **Per-process warm pools.** Separate OS processes (taskiq workers, uvicorn workers, the
   MCP subprocess) build their own registry at worker start after fork/spawn and warm it.
   `os.register_at_fork` resets the registry in a child; no handle crosses a fork.
5. **Keyed process globals.** The model scheduler, economy and ops ledgers, tap sink and
   concurrency gate move into the registry, keyed by their configuration, with
   refcounted close.

## Invariants (unchanged by sharing)

- Results are never shared: embedding/extraction caches, KV contents and per-namespace
  state stay Engine- and namespace-scoped (ADR-074 "share weights, never results",
  erasure #43).
- Single-writer embedded stores (Tantivy, Ladybug) are opened once per path per process;
  a second open on the same path shares the handle instead of failing or duplicating.
- `:memory:` SQLite stays unique per Engine.
- Credentials are part of the key through the credential-free target identity plus an
  auth digest; secrets are never stored in keys or logs.
- No compatibility layer or alias is added.

## Implementation notes

- **RM-73 (model weights).** `memspine.models.runtime.shared_weights.SharedWeights` is a lazy,
  refcounted lease on a `scope="thread"` registry entry; the key holds only weight-affecting
  options. Embedders (fastembed: model; sentence-transformers: model, device, dtype,
  trust_remote_code; model2vec: model), rerankers (fastembed, flashrank: model; Qwen3: model,
  device, quant; Jina: model, device), llama.cpp (absolute GGUF path plus `n_ctx` and native
  load options; never the role or profile name), GLiNER (model, revision, device,
  local_files_only; tasks and concurrency are per-lease limits) and OpenDecider (model,
  device, dtype, backend, threads) all load through it. Engine builders pass the process
  registry and register the lease release with the Engine lifecycle; directly constructed
  adapters use a private registry. The E4 static prefilter builds the same key as a
  configured static embedder of the same model. Load failures are never cached. Each adapter
  exposes `warm()` (load plus one fixed short inference) for RM-74; RM-73 schedules no
  warm-up itself. Result caches (`CachedEmbedding`, extraction caches) stay per Engine.

## Consequences

- Fewer connections and less RAM; no first-request model load once warm.
- Engines in one process can no longer assume private ownership of a client; tests must
  cover stop-one-while-another-active, different-settings isolation, loop affinity and
  fork reset.
- Work is phased: RM-71 registry core, RM-72 in-engine duplicates, RM-73 model sharing,
  RM-74 background warm-up and readiness, RM-75 keyed globals, RM-76 per-process warm
  pools, RM-77 multi-engine acceptance tests.

## Addendum 2026-10-22: alternatives re-verified and settings selected

Re-verification: a plain process singleton is rejected (mixes targets and credentials,
breaks loop-bound async clients and forks); per-Engine allocation is rejected (duplicate
pools and weight copies). The keyed refcounted registry matches library guidance (one
SQLAlchemy engine per database per process, long-lived HTTP sessions, load-once model
weights). An external connection pooler and a shared local model server are complements,
not replacements.

Selected `runtime.shared_resources` settings (RM-78), all strict, documented in USAGE.md,
defaults in `config/constants.py`:

| Setting | Default |
|---|---|
| `scope` | `process` (`engine` = isolated) |
| `warmup` | `background` (`lazy` \| `background` \| `blocking`) |
| `warmup_timeout_s` | 120 |
| `warmup_concurrency` | 2 |
| `warmup_include` / `warmup_exclude` | all / none |
| `keep_warm` | `true` (weights stay loaded for the process life) |
| `idle_ttl_s` | unset (opt-in) |
| `memory_budget_mb` | unset (opt-in; LRU among idle, never in-use) |
| `max_models_per_category` | unset (replaces fixed per-model caps) |
| `sql.pool_size` / `max_overflow` / `pool_recycle_s` / `pool_pre_ping` | 5 / 10 / 1800 / true |
| `http.max_connections` / `keepalive_s` | 100 / 30 |
| `model_server` | reserved for a future opt-in local model server (RM-79) |

## Evidence-based defaults (RM-80)

Owner, 2026-10-22: defaults are confirmed or changed by measurement, not judgement.
Harness: `scripts/benchmark_shared_resources.py` (fresh process per scenario; N Engines,
real cached fastembed model, no download). Baseline captured before sharing lands, then
re-run per variant on an idle host (latency) and any host (RSS, handles, threads).

| Setting | Variants | Primary metric | Decision rule |
|---|---|---|---|
| `scope` | process, engine | RSS and handles per extra Engine; second-Engine start | `process` unless isolation tests fail or saving < 10% |
| `warmup` | lazy, background, blocking | start time, first-request latency, time to all warm | `background` if first request after settle is within 20% of steady and start stays within 10% of lazy |
| `warmup_concurrency` | 1, 2, 4 | time to all warm, peak RSS, CPU | smallest value within 10% of the fastest |
| `sql.pool_size` / `max_overflow` | 2, 5, 10 / 0, 10 | concurrent ingest+search throughput, p95, connection count | smallest size within 5% of best throughput |
| `http.max_connections` | 10, 100 | p95 against a local stub server under concurrency | smallest within 5% of best p95 |
| `keep_warm` / `idle_ttl_s` | owner-fixed (keep warm, no TTL) | reload cost after release | documented, not re-decided |
| `memory_budget_mb` | opt-in | measured RSS per model | documented typical sizes |

### RM-80 result 1: `scope` (2026-10-22)

Same harness, before vs after RM-72/RM-73 (3 trials, cached bge-small, fresh process):
each extra identical Engine went from about 124 MB RSS, +23 threads and +32 OS handles to
about 6 MB, +8 threads and +17 handles; first request on additional Engines from 280-700 ms
to about 110 ms. Four Engines: 623 MB to 271 MB. Saving far exceeds the 10% rule and
isolation tests pass, so **`scope=process` is confirmed as the default**. Evidence:
`docs/provider_verification/rm80-shared-resources-{baseline,after-rm72-rm73}-2026-10-22.json`.
