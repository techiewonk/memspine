# ADR-022 — Shared, pluggable KV cache (memory / LMDB / Redis / Valkey)

**Status:** accepted · **Date:** 2026-07-10 · **Register:** implements D-09 (cache), amends D-22 (one more service port wired end-to-end) · **Phase:** services/* completion — Phase 2

## Context

The E3 semantic caches — `CachedEmbedding` (P1) and `CachedExtractor` (P3) — each
constructed their **own** `MemoryKV()` inside the engine. Two independent,
in-process, non-persistent caches meant:

- nothing survived a process restart (a warm embedding cache was thrown away on
  every reopen — the most expensive thing to recompute), and
- nothing could be **shared across processes** (a fleet of engines behind one
  REST surface each recomputed the same embeddings), and
- the LMDB (`[lmdb]`, D-09) and Redis/Valkey backends reserved in the plan had
  no code and no configuration surface.

The `KVCache` port (`services/cache/base.py`) already existed with a single
in-process implementation (`MemoryKV`). Keys are already producer-scoped
(`emb:{embedder_id}:…`, `ext:{prompt_version}:…`, N7), so one physical store can
safely hold both without collision.

## Decision

1. **One cache, injected.** The engine builds a single `KVCache` in
   `_build_cache()` and injects the same object into both `CachedEmbedding` and
   `CachedExtractor`. The two per-consumer `MemoryKV()` new-ups are gone.
2. **`CacheConfig` selects the backend** (`config.cache`):
   - `memory` (default): in-process `MemoryKV`, zero-dep — byte-identical
     behavior to before this ADR, so `profile="simple"` is unchanged.
   - `lmdb` (`[lmdb]`): persistent single-process cache at `cache.path` (an LMDB
     env directory). LMDB has no native TTL, so values are stored as
     `<f64 expires_at><value>` with **wall-clock** lazy expiry (a monotonic clock
     would break TTLs across restarts).
   - `redis` (`[redis]`) / `valkey` (`[valkey]`): shared cross-process cache at
     `cache.url`, native `EX` TTL. The two libraries are wire-compatible; either
     serves either backend, and the `backend` name only decides which extra the
     D-10 error names.
3. **Ports & adapters preserved (D-22).** New connection owners
   `clients/lmdb.py` (`LmdbClient`) and `clients/redis.py` (`RedisClient`) own the
   handle; the cache adapters (`services/cache/lmdb_cache.py`,
   `redis_cache.py`) never open a connection. Both clients defer the optional
   import to `connect()` and raise `MissingServiceError` naming the extra (D-10).
   The engine closes them centrally in `_teardown`.
4. **`cache.namespace` prefixes every key** so multiple memspine instances can
   share one LMDB env / Redis server without colliding.
5. **`describe()["cache"]`** reports the active backend.

## Consequences

- Positive: a warm embedding cache **persists** (`lmdb`) or is **shared**
  (`redis`/`valkey`); one cache to reason about; the reserved backends are real.
- Neutral: default install and behavior unchanged (`memory` backend, no new core
  dep — `lmdb`/`redis`/`valkey` stay optional extras).
- Cost: `RedisClient.connect()` pings eagerly, so a misconfigured shared cache
  fails loudly at `start()` (D-10) rather than mid-request.
- Deferred: `cache.url` will be **secrets-resolved** once the pluggable secrets
  tier lands (Phase 3) — today it is used verbatim.

## Notes on the register (SERVICE_EXTRAS)

`SERVICE_EXTRAS` maps one service → one extra, which does not fit a single
service backed by three possible extras (`lmdb`/`redis`/`valkey`). Rather than
distort that table, the **client** raises the correctly-named `MissingServiceError`
(`cache:lmdb`/`cache:redis`/`cache:valkey`), so the D-10 remedy is still exact.
`SERVICE_EXTRAS["cache"]` stays `"lmdb"` (the persistent default extra).

## Alternatives rejected

- **Keep two independent MemoryKV caches** — the status quo; loses persistence,
  sharing, and any backend selection.
- **One cache but no namespace prefix** — two instances on one Redis would serve
  each other stale entries once producer ids coincide.
- **A cache client that lazily connects on first use** — hides a misconfigured
  endpoint until the first write; eager `ping()` at start is the D-10 posture.
