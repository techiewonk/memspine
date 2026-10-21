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

## Consequences

- Fewer connections and less RAM; no first-request model load once warm.
- Engines in one process can no longer assume private ownership of a client; tests must
  cover stop-one-while-another-active, different-settings isolation, loop affinity and
  fork reset.
- Work is phased: RM-71 registry core, RM-72 in-engine duplicates, RM-73 model sharing,
  RM-74 background warm-up and readiness, RM-75 keyed globals, RM-76 per-process warm
  pools, RM-77 multi-engine acceptance tests.
