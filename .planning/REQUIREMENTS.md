# Requirements: memspine

**Defined:** 2026-07-10
**Core Value:** Every store (vector / graph / cache / SQL storage / LLM / embedding / secrets) is
pluggable behind a capability port — a developer swaps backends by config alone, zero code change,
with `profile="simple"` byte-identical.

> **Provenance:** DOC/ADR-derived (the ingest set contained no PRD or SPEC). Requirements are
> extracted from the authoritative blueprint (`docs/memspine-structure-plan.md` §5 phase plan +
> Part B enhancement program) and the ADR-021…025 ports-completion effort. Acceptance baseline for
> every capability is the C6 combination matrix (§6): clean startup, one write→read round-trip per
> enabled memory type, clean shutdown, `describe()` schema, `config validate` golden tests, runner
> tests (dbos crash-resume, inline flush-on-exit, dead-letter). Treat these as capability
> requirements, not formal PRD acceptance contracts.

## v1 Requirements

### Core Substrate (CORE)

- [x] **CORE-01**: Append-only `memory_events` log is the sole source of truth; vector/graph/FTS/cache are rebuildable projectors that `Engine.rebuild()` reconstructs
- [x] **CORE-02**: Event-log retention is configurable (full/rolling/ephemeral) with optional zstd payload compression; pruning never passes projector high-water marks and degrades via audit taint on loss
- [x] **CORE-03**: Config resolves deterministically (schema defaults → template `extends:` → user YAML → env → kwargs) with per-key source tracking and `extends` cycle rejection
- [x] **CORE-04**: A missing capability raises `MissingServiceError` naming the extra to install (unless `strict_services: false`)
- [x] **CORE-05**: P0 DDL carries firewall (trust/quarantined/instruction_flag), provenance/lifecycle (status/version/history/evolve_to/source), and xxhash/fastuuid/orjson fingerprint/id/payload fields

### Memory Types (MEM)

- [x] **MEM-01**: Working-memory write→read round-trip through the real write pipeline
- [x] **MEM-02**: Semantic memory with two-stage dedup (MinHash-LSH → cosine confirm) and optional gliner2 entity resolution
- [x] **MEM-03**: Episodic memory with a consolidate→decay→compress lifecycle
- [x] **MEM-04**: Resource memory with multi-format ingest (markitdown + chonkie; docx/pdf/pptx smoke)
- [x] **MEM-05**: Procedural (skill/plan) memory advancing a staged→active lifecycle ladder via RESOLVING
- [x] **MEM-06**: Reflective memory that carries caller trust
- [x] **MEM-07**: Associative memory — LINK events project to graph edges with an enforced link budget (ConflictError) and weight-0 tombstone pruning
- [x] **MEM-08**: Prospective memory — watches fire over existing event-log columns (no new EventKind/DDL)
- [x] **MEM-09**: Shared memory — cross-namespace grants with trust capping

### Retrieval & Assembly (RET)

- [x] **RET-01**: Cache-friendly stability-sorted context assembly (persona → skills/rules → semantic facts → [cache boundary] → episodic/working/query) with `cached_tokens` metrics
- [x] **RET-02**: Embedding and extraction/judge caches keyed by embedder-id + prompt-version
- [x] **RET-03**: Opt-in hybrid retrieval fusing a BM25 lexical leg with the vector leg via RRF (default off; v0.2 flip intended)
- [x] **RET-04**: Opt-in rerank + MMR retrieval-quality stages (default off)
- [x] **RET-05**: Two-stage quantized-prefilter/float-rescore vector search with Matryoshka truncation; default OFF (simple profile byte-identical)

### Governance & Firewall (GOV)

- [x] **GOV-01**: Memory Firewall trust scoring at write (retrieved/external content capped low) with a quarantine tier excluded from consolidation/promotion until corroborated
- [x] **GOV-02**: Write-path anomaly detection (embedding-outlier + MINJA heuristics) and instruction-shaped-content flagging
- [x] **GOV-03**: M7 hard erasure — `forget` + `verify_forget` + blast-radius audit taint

### Learning Dynamics (LRN)

- [x] **LRN-01**: Background pipelines are runner-agnostic idempotent step functions decorated by inline/DBOS/taskiq runners (no runner imports inside pipeline code)
- [x] **LRN-02**: A sleep cycle runs consolidate→decay→compress with a reserved fourth (E7) sleep-compute hook (no-op default)
- [x] **LRN-03**: The durable runner survives crash-resume and routes failures to a dead-letter path

### Prompts (PRM)

- [x] **PRM-01**: Every internal LLM call resolves a named, versioned prompt from a YAML pack via the registry, with config-layered overrides and pydantic output-model pairing

### Composable Stores / Ports (PORT) — headline metric

- [x] **PORT-01**: LanceDB is the sole core vector store; no SQLite brute-force fallback; E4 rescore is LanceDB-native
- [x] **PORT-02**: The KV cache backend is swappable (memory/LMDB/Redis/Valkey) by config, shared across embedding and extractor caches
- [x] **PORT-03**: The secrets resolver is swappable (env/chained/AWS Secrets Manager), selected by env during bootstrap
- [x] **PORT-04**: LLM/embedding/rerank run through one litellm gateway so local↔cloud providers swap by config
- [x] **PORT-05**: The lexical provider swaps (SQLite FTS5 ↔ Tantivy) with identical round-trip behavior
- [x] **PORT-06**: A dialect-neutral `SqlStorage` base runs the event log + read model identically on SQLite↔Postgres by config alone

### Protocols & Infra (API)

- [x] **API-01**: A no-authn REST protocol exposes the Engine, with the `/write` path treated as an untrusted external channel
- [x] **API-02**: A taskiq brokered runner provides per-scope queues with priority labels

### Release Hardening (REL) — open

- [ ] **REL-01**: USAGE.md / FEATURES.md / README document the shipped Engine/CLI/REST surface, verified against the code
- [ ] **REL-02**: Live-backend contract tests pass against a real Postgres (plus swappable cache/secrets/LLM round-trips), not just SQLite/in-memory
- [ ] **REL-03**: Alembic migration history is squashed to a clean v0.1 baseline

## v2 Requirements

Deferred to a future release. Tracked, not in the current roadmap.

### Security / Access (SEC)

- **SEC-01**: REST authentication + authorization (v0.1 ships no-authn by design — ADR-018)
- **SEC-02**: Certified-defense integration for the Memory Firewall (research-grade hook reserved)

### Protocols (PROTO)

- **PROTO-01**: WS / gRPC / MCP protocol adapters (seats reserved in `protocols/`)

### Scope Expansion (EXP)

- **EXP-01**: Per-namespace memory-type enablement (config key reserved)
- **EXP-02**: Third-party memory plugins (interfaces provisional)
- **EXP-03**: Additional production store adapters (Weaviate / Neo4j) beyond stubs

## Out of Scope

Explicitly excluded for v0.1. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| REST authn/authz | `/write` is an untrusted external channel behind the operator boundary; no-authn is the deliberate v0.1 deployment contract (ADR-018) |
| File-native / Markdown profile | D-30 skipped |
| torch / transformers in core | Slim-core invariant — only behind `[st]` extra (ADR-002) |
| Benchmark leaderboard claims | Harness-first; the (accuracy, tokens, latency) triplet rule |
| WS / gRPC / MCP protocols | Seats reserved; not built in v0.1 |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CORE-01 | Phase 1 | Complete |
| CORE-02 | Phase 1 | Complete |
| CORE-03 | Phase 1 | Complete |
| CORE-04 | Phase 1 | Complete |
| CORE-05 | Phase 1 | Complete |
| MEM-01 | Phase 2 | Complete |
| RET-01 | Phase 2 | Complete |
| RET-02 | Phase 2 | Complete |
| MEM-02 | Phase 3 | Complete |
| PRM-01 | Phase 3 | Complete |
| MEM-03 | Phase 4 | Complete |
| LRN-01 | Phase 4 | Complete |
| LRN-02 | Phase 4 | Complete |
| LRN-03 | Phase 4 | Complete |
| MEM-04 | Phase 5 | Complete |
| GOV-01 | Phase 5 | Complete |
| GOV-02 | Phase 5 | Complete |
| GOV-03 | Phase 5 | Complete |
| MEM-05 | Phase 6 | Complete |
| MEM-06 | Phase 6 | Complete |
| MEM-07 | Phase 7 | Complete |
| RET-05 | Phase 7 | Complete |
| MEM-08 | Phase 8 | Complete |
| MEM-09 | Phase 8 | Complete |
| RET-03 | Phase 8 | Complete |
| RET-04 | Phase 8 | Complete |
| API-01 | Phase 8 | Complete |
| API-02 | Phase 8 | Complete |
| PORT-01 | Phase 9 | Complete |
| PORT-02 | Phase 10 | Complete |
| PORT-03 | Phase 11 | Complete |
| PORT-04 | Phase 12 | Complete |
| PORT-05 | Phase 13 | Complete |
| PORT-06 | Phase 14 | Complete |
| REL-01 | Phase 15 | In Progress |
| REL-02 | Phase 16 | Pending |
| REL-03 | Phase 17 | Pending |

**Coverage:**
- v1 requirements: 37 total
- Mapped to phases: 37
- Unmapped: 0 ✓
- Complete: 34 · Open (Phases 15–17): 3

---
*Requirements defined: 2026-07-10*
*Last updated: 2026-07-10 after ingest bootstrap*
