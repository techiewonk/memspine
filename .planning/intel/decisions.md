# Decisions (ADR intel)

Synthesized from `docs/adr/*.md`. Source of truth for locked architectural
decisions. Every ADR in this ingest set is **LOCKED** (status: accepted).
Precedence: ADR > SPEC > PRD > DOC. 25 locked decisions.

Amendment chains are reconciled inline in the source ADRs (see
`INGEST-CONFLICTS.md` INFO bucket). Where a later ADR supersedes an earlier
one on a specific point, the **current** ruling is noted here.

---

## ADR-001 — Event-sourced core (LOCKED)
- source: docs/adr/ADR-001-event-sourced-core.md
- decision: An append-only `memory_events` log is the single source of truth;
  every derived store (vector, graph, FTS, cache) is a rebuildable projector,
  never a second source of truth.
- scope: memory_events log, SQLiteStorage.append_event, projectors, core/events.py, core/projector.py, core/replay.py, services/storage
- related: ADR-011 (retention)

## ADR-002 — Slim core + extras packaging (LOCKED)
- source: docs/adr/ADR-002-slim-core-extras.md
- decision: Keep core dependencies small; push all heavy deps into optional
  `pip install memspine[extra]` extras. torch/transformers never enter core.
- scope: core dependencies, extras matrix, packaging, tooling (Python 3.13, uv, ruff, pytest, mkdocs)
- related: ADR-021 (LanceDB later added to core — deliberate, justified expansion)

## ADR-003 — Store defaults: SQLite · LanceDB · graph · LMDB (LOCKED)
- source: docs/adr/ADR-003-store-defaults.md
- decision: Embedded, zero-server default stores — SQLite (relational/event-log/FTS5)
  in core; LanceDB in core as sole vector store; graph adapter with kuzu `[kuzu]`
  first-class alt and `sqlite_adjacency` zero-dep fallback; LMDB `[lmdb]` hot cache.
  Prod swap-ins (Postgres/Weaviate/Neo4j/Valkey) are stubs in v0.1.
- current rulings (amended inline): vector = LanceDB-only (ADR-020/021 removed the
  SQLite brute-force vector fallback + `[lance]` extra); graph default =
  `sqlite_adjacency` (ADR-015 amended the LadybugDB default).
- scope: SQLite, LanceDB, graph store, sqlite_adjacency, LMDB, FTS5, vector store, lexical port
- related: ADR-021, ADR-015, ADR-020

## ADR-004 — Two-layer ports & adapters: services/ + clients/ (LOCKED)
- source: docs/adr/ADR-004-ports-and-adapters.md
- decision: Split capability ports (`services/`) from connection-owning adapters
  (`clients/`) so backends stay swappable. Services never open a connection.
- scope: services layer, clients layer, capability manifest, lifecycle manager

## ADR-005 — Background execution: TaskRunner seam (LOCKED)
- source: docs/adr/ADR-005-background-execution.md
- decision: Background pipelines are plain idempotent step functions
  (`workers/pipelines.py`) decorated by pluggable runners (inline/dbos/taskiq).
  No runner imports inside pipeline code — avoids worker-framework lock-in.
- scope: TaskRunner seam, inline/dbos/taskiq runners, consolidation, decay, compression, dead-letter
- related: ADR-017 (taskiq runner lands P7)

## ADR-006 — Config layering + templates with `extends:` (LOCKED)
- source: docs/adr/ADR-006-config-layering.md
- decision: Config layering order = schema defaults → template `extends:` →
  user YAML → env → kwargs, with per-key source tracking.
- scope: config loader/schema/templates, secrets port, namespace policy overrides, profiles
- related: ADR-009

## ADR-007 — Public OSS from day one (LOCKED)
- source: docs/adr/ADR-007-public-oss-posture.md
- decision: Ship as public open-source from day one — Apache-2.0, semver,
  additive/conservative schema, mkdocs-material + ADR discipline, evals packaged.
- scope: licensing, semver, public API (memspine.Engine), DDL, docs, ADR register

## ADR-008 — Code-level adoptions D-26…D-42 (LOCKED)
- source: docs/adr/ADR-008-code-level-adoptions.md
- decision: Adopt field-proven libraries + five MemOS patterns across dedup,
  NER, ingest, storage, hashing, community detection.
- scope: datasketch, gliner2, markitdown, chonkie, instructor, zstandard, litellm, SQLAlchemy Core, xxhash, fastuuid, pydantic, orjson, graspologic
- related: DEPENDENCY_ANALYSIS.md, PACKAGE_CATALOG.md

## ADR-009 — Customizable prompt subsystem (LOCKED)
- source: docs/adr/ADR-009-prompt-subsystem.md
- decision: First-class prompts package — versioned YAML default pack,
  PromptRegistry, config layering, pydantic output-model pairing, prompt CLI,
  self-optimization hook. Prompts are data, never inline strings.
- scope: prompts package, PromptRegistry, YAML pack, output-model pairing, prompt CLI

## ADR-010 — Async storage driver: aiosqlite + greenlet in core (LOCKED)
- source: docs/adr/ADR-010-async-storage-driver.md
- decision: Add aiosqlite + greenlet to core so SQLAlchemy Core storage runs a
  genuinely async engine; sync verbs wrap the async path.
- scope: storage service, SQLite client, aiosqlite, greenlet, SQLAlchemy async engine, sync wrappers

## ADR-011 — Configurable event-log retention: full/rolling/ephemeral + zstd (LOCKED)
- source: docs/adr/ADR-011-event-log-retention.md
- decision: `event_log.mode` (full default / rolling / ephemeral) + optional zstd
  payload compression to bound append-only storage. Pruning never passes projector
  high-water marks; rebuild/audit guarantees are preserved (audit taint on loss).
- scope: memory_events log, event_log.mode, prune_events, zstd, StorageService.can_rebuild, Engine.rebuild
- related: ADR-001, E1, D-32, D-45

## ADR-012 — HTTP client in core: httpx (LOCKED)
- source: docs/adr/ADR-012-httpx-core.md
- decision: httpx is the core async HTTP client (owned by `clients/http.py`) for
  local OpenAI-compatible LLM hosts, with MockTransport testing.
- scope: httpx, clients/http.py, services/llm/local.py, connection pooling
- current ruling: the hand-rolled openai_compat LLM path was later replaced by
  litellm (ADR-024); httpx remains core.

## ADR-013 — Memory-Firewall & M7 governance amendments (LOCKED)
- source: docs/adr/ADR-013-firewall-governance-amendments.md
- decision: Four firewall/M7 choices amending the event-sourced contract —
  content-mutable erasure, deterministic gate, canonical quarantine,
  allow-listed lifecycle deltas (DECAY_TRANSITION).
- scope: Memory Firewall, M7 hard erasure, quarantine, trust policy, core/erasure.py

## ADR-014 — P5 lifecycle amendments (procedural, prompts, reflections) (LOCKED)
- source: docs/adr/ADR-014-p5-lifecycle-amendments.md
- decision: Three P5 lifecycle rulings — staged plans via RESOLVING, prompts enter
  the ladder at active, reflections carry caller trust.
- scope: procedural/skill lifecycle, prompt registry/versioning, reflections, quarantine, SkillStage, namespace ownership
- related: ADR-013

## ADR-015 — Associative links as LINK events (P6) (LOCKED)
- source: docs/adr/ADR-015-associative-links.md
- decision: `EventKind.LINK` ("memory.link", payload {src,dst,rel,weight,reason})
  carries every association — links ride the log; graph is a rebuildable
  GraphProjector. Link budget enforced at creation (ConflictError); prune =
  weight-0 tombstone (replay-deterministic). Default graph store =
  `sqlite_adjacency` (D-26 amended; ladybug `[graph]` real adapter since
  2026-07-09, config default stays sqlite_adjacency pending a future ADR; kuzu
  `[kuzu]` first-class alt). PPR recall pure-Python; reorganizer writes
  consolidation-shaped community parents.
- scope: associative memory, EventKind.LINK, GraphProjector, link budget, PPR, D-42 reorganizer, sqlite_adjacency
- related: ADR-014 (amends ADR-003 graph default)

## ADR-016 — Prospective watches & shared-memory grants (P7) (LOCKED)
- source: docs/adr/ADR-016-prospective-shared.md
- decision: Prospective watches + cross-namespace shared-memory grants map onto
  existing event-log columns/events — no new DDL or EventKind.
- scope: prospective memory, shared memory, watches, grants, subscriptions, find_active_fact, trust capping
- related: ADR-014

## ADR-017 — P7 infrastructure: REST, taskiq, E5/E8 (LOCKED)
- source: docs/adr/ADR-017-p7-infrastructure.md
- decision: P7 infra — no-authn REST protocol, taskiq brokered runner, E5
  assembly-time compression, opt-in E8 retrieval-quality stages (rerank, static
  prefilter, hybrid RRF).
- scope: protocols/rest, taskiq runner, E5 compression, CompressionPolicy, E8 stages, rerank, hybrid RRF, lexical/FTS5, Engine.search
- related: ADR-005, ADR-016, ADR-018, ADR-019

## ADR-018 — P7 review hardening (security + robustness) (LOCKED)
- source: docs/adr/ADR-018-p7-review-hardening.md
- decision: Four security decisions + correctness closures from a 5-agent review;
  sharpens the REST deployment contract (REST /write trust boundary,
  forget/verify_forget/audit_taint, TrustPolicy external channels, TaintReport).
- scope: shared memory type, forget/verify_forget/audit_taint, REST /write trust boundary, TrustPolicy, taskiq, reranker, TaintReport
- related: ADR-017, ADR-014, ADR-016

## ADR-019 — Hybrid retrieval: lexical BM25 port + RRF fusion (L1) (LOCKED)
- source: docs/adr/ADR-019-hybrid-retrieval.md
- decision: Opt-in lexical BM25 port (SQLiteFTS5Lexical / TantivyLexical) fused
  with the vector leg via normalized RRF; LexicalProjector; corpus cache
  generation counter; query/content sanitization. Default off; v0.2 flips on.
- scope: hybrid retrieval, lexical BM25 port, RRF fusion, LexicalProjector, Engine.search, verify_forget
- related: ADR-017, ADR-018, D-25, D-53

## ADR-020 — Embedding quantization + float rescore, Matryoshka, model2vec (E4) (LOCKED)
- source: docs/adr/ADR-020-embedding-quantization.md
- decision: Two-stage quantized-prefilter/float-rescore vector search
  (LanceDB-native), Matryoshka truncation, opt-in model2vec static prefilter.
  All additive and default-off (byte-identical `profile="simple"`). Migration 0009.
- scope: embedding quantization, vector store, LanceDB, search_rescore, Matryoshka, model2vec, migration 0009
- related: ADR-021

## ADR-021 — LanceDB as sole core vector store (LOCKED)
- source: docs/adr/ADR-021-lancedb-core-vector.md
- decision: LanceDB is a core (non-extra) dependency and the sole vector backend;
  removes the SQLite brute-force vector store and `[lance]` extra. E4 rescore is
  LanceDB-native only.
- scope: LanceDB, vector store, SQLite brute-force vector store (removed), E4 quantization, FTS5 lexical, profile=simple
- related: ADR-020, ADR-003 (amends D-09)

## ADR-022 — Shared, pluggable KV cache (memory/LMDB/Redis/Valkey) (LOCKED)
- source: docs/adr/ADR-022-shared-cache-backends.md
- decision: Inject a single pluggable KVCache backend (memory/LMDB/Redis/Valkey)
  shared across the embedding and extractor caches.
- scope: KVCache, CacheConfig, MemoryKV, LmdbClient, RedisClient, CachedEmbedding, CachedExtractor, services/cache

## ADR-023 — Pluggable secrets resolver + AWS Secrets Manager (LOCKED)
- source: docs/adr/ADR-023-pluggable-secrets-aws.md
- decision: Pluggable secrets resolvers (AWS Secrets Manager, chained, env)
  selected via env var during pre-config bootstrap phase 1. boto3 behind `[aws]`.
- scope: SecretsService, AwsSecrets, ChainedSecrets, EnvSecrets, bootstrap secrets, [aws] extra

## ADR-024 — LiteLLM as unified LLM/embedding/rerank gateway (LOCKED)
- source: docs/adr/ADR-024-litellm-llm-embedding.md
- decision: litellm is a core, lazily-imported single adapter for cloud and local
  LLM, embedding, and rerank; removes the hand-rolled openai_compat path.
- scope: litellm, LLM gateway, embeddings, rerank, OpenAICompatLLM (removed), LLMRoleConfig, LlamaCppLLM, slim core
- note: supersedes the openai_compat approach implied by ADR-012's LLM path (httpx stays core).

## ADR-025 — PostgreSQL storage backend + dialect-neutral SqlStorage base (LOCKED)
- source: docs/adr/ADR-025-postgres-storage.md
- decision: Add a PostgreSQL storage backend via a shared dialect-neutral
  `SqlStorage` base so the event log + read model run identically on SQLite and
  Postgres (psycopg3, Alembic migrations, FTS5 lexical + sqlite_adjacency graph
  providers dialect-scoped).
- scope: SqlStorage base, SQLiteStorage, PostgresStorage, PostgresClient, psycopg3, StorageConfig, event log, Alembic
- related: ADR-021, D-36, D-45, D-10
