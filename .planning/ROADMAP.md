# Roadmap: memspine

## Overview

memspine grew from an event-sourced substrate into a full cognitive-memory engine (all nine memory
types over an append-only log, Phases 1–8 / P0–P7), then proved its headline promise — composable,
swappable stores — by rewiring every backend behind a capability port (Phases 9–14 / ADR-021…025:
LanceDB-sole vector, pluggable cache, pluggable secrets, litellm gateway, lexical parity,
dialect-neutral Postgres storage). Both efforts are landed and green. What remains is a short v0.1
release-hardening tail (Phases 15–17): finalize the user-facing docs, run the swappable backends
under live contract tests, and squash migrations to a clean baseline.

## Milestones

- ✅ **Cognitive Memory Engine v0.1 (P0–P7)** — Phases 1–8 (landed & green)
- ✅ **Composable & Swappable Stores** — Phases 9–14 (landed & green)
- 🚧 **v0.1 Release Hardening** — Phases 15–17 (in progress)

## Phases

<details>
<summary>✅ Cognitive Memory Engine v0.1 — Phases 1–8 (P0–P7, COMPLETE)</summary>

### Phase 1: Substrate
**Goal**: An event-sourced core others build on — the append-only log, config, storage, secrets, and firewall-ready schema.
**Depends on**: Nothing (first phase)
**Requirements**: CORE-01, CORE-02, CORE-03, CORE-04, CORE-05
**Success Criteria** (what must be TRUE):
  1. A write appends to `memory_events` and `Engine.rebuild()` reconstructs every projector from the log alone
  2. Retention modes (full/rolling/ephemeral, optional zstd) honor projector high-water marks; loss surfaces as audit taint
  3. Config resolves through the full layering order with per-key source tracking; `extends` cycles are rejected
  4. A missing capability raises `MissingServiceError` naming the extra to install
  5. The P0 DDL carries firewall + provenance/lifecycle columns and xxhash/fastuuid/orjson fields
**Plans**: Landed (P0)

Plans:
- [x] 01: Substrate — core/*, config, SQLite storage (SQLAlchemy Core + Alembic, aiosqlite), secrets, observability, CLI config, retention

### Phase 2: Working Memory + Retrieval
**Goal**: The first memory type round-trips through the real pipeline with cache-friendly retrieval.
**Depends on**: Phase 1
**Requirements**: MEM-01, RET-01, RET-02
**Success Criteria** (what must be TRUE):
  1. A working-memory write→read round-trips end to end
  2. Assembly places context in stability-sorted order with a cache boundary and reports `cached_tokens`
  3. Embedding + extraction caches hit on repeat content, keyed by embedder-id + prompt-version
**Plans**: Landed (P1)

Plans:
- [x] 02: Working memory, services/llm (local providers), embedding/vector/cache defaults, scoring+assembly (E2), inline worker, embedding cache (E3)

### Phase 3: Semantic Memory
**Goal**: Durable facts with dedup, entity resolution, and a prompts-as-data subsystem.
**Depends on**: Phase 2
**Requirements**: MEM-02, PRM-01
**Success Criteria** (what must be TRUE):
  1. A semantic write→read round-trips; two-stage dedup (MinHash-LSH → cosine confirm) collapses near-duplicates
  2. Optional gliner2 entity resolution links mentions to entities
  3. Every internal LLM call resolves a named, versioned prompt from the YAML pack via the registry with config-layered overrides
**Plans**: Landed (P2)

Plans:
- [x] 03: Semantic memory, dedup (datasketch), entities (gliner2), prompts subsystem, instructor structured output, E9 YAML/CoD

### Phase 4: Episodic + Lifecycle
**Goal**: Time-ordered episodes plus the background learning dynamics that consolidate, decay, and compress them.
**Depends on**: Phase 3
**Requirements**: MEM-03, LRN-01, LRN-02, LRN-03
**Success Criteria** (what must be TRUE):
  1. An episodic write→read round-trips and moves through a consolidate→decay→compress lifecycle
  2. Pipelines run identically under inline and durable runners with no runner imports in pipeline code
  3. A durable (DBOS) run survives crash-resume; failures land in a dead-letter path
  4. The sleep cycle exposes a reserved fourth (E7) hook that is a no-op by default
**Plans**: Landed (P3)

Plans:
- [x] 04: Episodic memory, consolidation/decay/compression (zstandard), pipelines + dbos_runner + schedule, resource ingest, E3 extraction cache, E7 hook

### Phase 5: Governance + Memory Firewall
**Goal**: The differentiator — poisoning defense, quarantine, and verifiable erasure — plus resource memory.
**Depends on**: Phase 4
**Requirements**: MEM-04, GOV-01, GOV-02, GOV-03
**Success Criteria** (what must be TRUE):
  1. Retrieved/external content is capped low-trust at write; quarantined records are excluded from consolidation/promotion until corroborated
  2. Write-path anomaly detection (embedding-outlier + MINJA heuristics) and instruction-shaped-content flagging are active
  3. `forget` + `verify_forget` erase content and propagate blast-radius audit taint
  4. Resource memory ingests docx/pdf/pptx and round-trips
**Plans**: Landed (P4 / E1)

Plans:
- [x] 05: M7 delete hooks + `cli forget --verify`, resource memory, full Memory Firewall (trust/quarantine/anomaly/taint)

### Phase 6: Procedural + Reflective
**Goal**: Skills/plans that graduate through a lifecycle ladder, and trust-carrying reflections.
**Depends on**: Phase 5
**Requirements**: MEM-05, MEM-06
**Success Criteria** (what must be TRUE):
  1. A skill/plan advances the staged→active ladder, held out of retrieval via RESOLVING until active
  2. Validated multi-step plans are retrieved by task-embedding similarity
  3. Reflections carry the caller's trust level
**Plans**: Landed (P5 / E6)

Plans:
- [x] 06: Procedural + reflective memory, E6 plan-skill subtype (staged plans via RESOLVING)

### Phase 7: Associative Graph
**Goal**: Associations ride the event log; the graph is a rebuildable projector with quantized two-stage recall.
**Depends on**: Phase 6
**Requirements**: MEM-07, RET-05
**Success Criteria** (what must be TRUE):
  1. LINK events project to graph edges; the link budget is enforced at creation (ConflictError); prune = weight-0 tombstone (replay-deterministic)
  2. PPR recall returns associated memories; graph adapters (sqlite_adjacency / kuzu / ladybug) share the same round-trip
  3. Two-stage quantized-prefilter/float-rescore search with Matryoshka truncation is available and OFF by default (simple profile byte-identical)
**Plans**: Landed (P6 / E4)

Plans:
- [x] 07: Associative memory (LINK events, GraphProjector, PPR), graph adapters, communities (graspologic) + reorganizer, E4 two-stage retrieval

### Phase 8: Prospective + Shared + REST
**Goal**: The last two memory types plus the network surface — a REST protocol and a brokered runner.
**Depends on**: Phase 7
**Requirements**: MEM-08, MEM-09, RET-03, RET-04, API-01, API-02
**Success Criteria** (what must be TRUE):
  1. Prospective watches and cross-namespace shared-memory grants work over existing DDL (no new EventKind)
  2. The REST protocol exposes the Engine with the `/write` path treated as an untrusted external channel
  3. A taskiq brokered runner provides per-scope queues with priority labels
  4. Opt-in hybrid RRF and rerank/MMR stages are available and default-off
**Plans**: Landed (P7)

Plans:
- [x] 08: Prospective + shared memories, protocols/rest, taskiq runner, E5 compression + E8 rerank/hybrid RRF opt-in

</details>

<details>
<summary>✅ Composable & Swappable Stores — Phases 9–14 (ADR-021…025, COMPLETE)</summary>

### Phase 9: LanceDB Sole Core Vector
**Goal**: One vector path — LanceDB in core, no brute-force fallback, native two-stage rescore.
**Depends on**: Phase 8
**Requirements**: PORT-01
**Success Criteria** (what must be TRUE):
  1. LanceDB is the sole vector backend; the SQLite brute-force store and `[lance]` extra are gone
  2. E4 quantized-prefilter/float-rescore runs LanceDB-native
  3. `profile="simple"` remains byte-identical (default embedders declare no quantization capability)
**Plans**: Landed (ADR-020/021)

Plans:
- [x] 09: LanceDB-sole vector store, LanceDB-native search_rescore, remove brute-force fallback

### Phase 10: Shared Pluggable KV Cache
**Goal**: One cache seam, swappable by config across every cache consumer.
**Depends on**: Phase 9
**Requirements**: PORT-02
**Success Criteria** (what must be TRUE):
  1. A single KVCache backend (memory/LMDB/Redis/Valkey) is injected and shared by the embedding and extractor caches
  2. Swapping the backend is config-only — no code change
  3. The default (in-memory) path keeps the simple profile installable without extras
**Plans**: Landed (ADR-022)

Plans:
- [x] 10: Shared pluggable KVCache (memory/LMDB/Redis/Valkey), CachedEmbedding + CachedExtractor unified

### Phase 11: Pluggable Secrets Resolver
**Goal**: Secrets resolved through a swappable resolver chosen before config loads.
**Depends on**: Phase 10
**Requirements**: PORT-03
**Success Criteria** (what must be TRUE):
  1. The secrets resolver (env / chained / AWS Secrets Manager) is selected via env var during pre-config bootstrap
  2. Swapping env→AWS is config/env-only; boto3 stays behind the `[aws]` extra
  3. A missing `[aws]` extra hard-fails clearly when AWS resolution is requested
**Plans**: Landed (ADR-023)

Plans:
- [x] 11: Pluggable secrets (EnvSecrets/ChainedSecrets/AwsSecrets), bootstrap selection, `[aws]` extra

### Phase 12: LiteLLM Unified Gateway
**Goal**: One adapter for LLM, embedding, and rerank — local and cloud swap by config.
**Depends on**: Phase 11
**Requirements**: PORT-04
**Success Criteria** (what must be TRUE):
  1. LLM, embedding, and rerank all route through the litellm gateway (lazily imported, core)
  2. Swapping a role from a local provider to a cloud provider is config-only
  3. The hand-rolled openai_compat path is removed; slim core holds (litellm lazy-import)
**Plans**: Landed (ADR-024)

Plans:
- [x] 12: litellm unified LLM/embedding/rerank gateway, per-role config, remove openai_compat

### Phase 13: Lexical Provider Parity
**Goal**: The lexical/BM25 leg is provider-swappable with identical behavior.
**Depends on**: Phase 12
**Requirements**: PORT-05
**Success Criteria** (what must be TRUE):
  1. SQLite FTS5 and Tantivy lexical providers produce the same write→read round-trip
  2. A parity test asserts equivalence across providers
  3. Hybrid RRF fusion consumes either provider identically
**Plans**: Landed (D-25 / ADR-019)

Plans:
- [x] 13: Lexical provider parity test (SQLiteFTS5 ↔ Tantivy), provider-choice docs

### Phase 14: PostgreSQL Storage Backend
**Goal**: Prove the composability metric end to end — the event log runs identically on SQLite and Postgres.
**Depends on**: Phase 13
**Requirements**: PORT-06
**Success Criteria** (what must be TRUE):
  1. A dialect-neutral `SqlStorage` base runs the event log + read model identically on SQLite and Postgres
  2. Switching SQLite→Postgres is config-only; no dialect-specific behavior leaks into the storage surface
  3. FTS5 lexical + sqlite_adjacency graph providers are dialect-scoped without breaking the Postgres path
**Plans**: Landed (ADR-025)

Plans:
- [x] 14: SqlStorage base + PostgresStorage/PostgresClient (psycopg3), Alembic migrations, dialect-scoped providers

</details>

### 🚧 v0.1 Release Hardening (In Progress)

**Milestone Goal:** Ship a clean, documented, live-verified v0.1 — the docs match the code, the
swappable backends pass under real services, and the migration history is a tidy baseline.

#### Phase 15: Documentation & Usage Finalization
**Goal**: The user-facing docs describe exactly what the shipped engine does and how to drive it.
**Depends on**: Phase 14
**Requirements**: REL-01
**Success Criteria** (what must be TRUE):
  1. USAGE.md walks install → Engine → CLI → REST against the current API with runnable examples
  2. FEATURES.md and README accurately catalog the nine memory types, profiles, and config keys as shipped
  3. Every documented config key / verb / route resolves against the code (no drift)
**Plans**: TBD

Plans:
- [ ] 15-01: Verify + refresh USAGE.md / FEATURES.md / README against the shipped Engine/CLI/REST surface

#### Phase 16: Live Backend Contract Verification
**Goal**: The composability promise holds against real services, not just SQLite/in-memory stand-ins.
**Depends on**: Phase 15
**Requirements**: REL-02
**Success Criteria** (what must be TRUE):
  1. The storage contract test suite passes against a live Postgres instance
  2. The swappable cache (Redis/Valkey), secrets (AWS), and LLM (cloud provider) round-trips pass against live backends or documented fakes
  3. A repeatable way to run the live suite (compose/fixtures + docs) exists
**Plans**: TBD

Plans:
- [ ] 16-01: Live-Postgres contract-test run + swappable-store live round-trips + harness docs

#### Phase 17: Pre-release Cleanups
**Goal**: A clean v0.1 baseline — migrations squashed, deferred items closed or explicitly carried.
**Depends on**: Phase 16
**Requirements**: REL-03
**Success Criteria** (what must be TRUE):
  1. The Alembic migration history is squashed to a single clean v0.1 baseline with an upgrade path documented
  2. `alembic upgrade head` builds an identical schema to the incremental history
  3. Remaining deferred items are either closed or explicitly recorded as carried to v2
**Plans**: TBD

Plans:
- [ ] 17-01: Squash Alembic migrations to a v0.1 baseline; reconcile deferred items

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → … → 14 (complete) → 15 → 16 → 17

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Substrate | Engine v0.1 | 1/1 | Complete | landed |
| 2. Working Memory + Retrieval | Engine v0.1 | 1/1 | Complete | landed |
| 3. Semantic Memory | Engine v0.1 | 1/1 | Complete | landed |
| 4. Episodic + Lifecycle | Engine v0.1 | 1/1 | Complete | landed |
| 5. Governance + Memory Firewall | Engine v0.1 | 1/1 | Complete | landed |
| 6. Procedural + Reflective | Engine v0.1 | 1/1 | Complete | landed |
| 7. Associative Graph | Engine v0.1 | 1/1 | Complete | landed |
| 8. Prospective + Shared + REST | Engine v0.1 | 1/1 | Complete | landed |
| 9. LanceDB Sole Core Vector | Composable Stores | 1/1 | Complete | landed |
| 10. Shared Pluggable KV Cache | Composable Stores | 1/1 | Complete | landed |
| 11. Pluggable Secrets Resolver | Composable Stores | 1/1 | Complete | landed |
| 12. LiteLLM Unified Gateway | Composable Stores | 1/1 | Complete | landed |
| 13. Lexical Provider Parity | Composable Stores | 1/1 | Complete | landed |
| 14. PostgreSQL Storage Backend | Composable Stores | 1/1 | Complete | landed |
| 15. Documentation & Usage Finalization | Release Hardening | 0/1 | In progress | - |
| 16. Live Backend Contract Verification | Release Hardening | 0/1 | Not started | - |
| 17. Pre-release Cleanups | Release Hardening | 0/1 | Not started | - |
