# Changelog

All notable changes to memspine are documented here. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Changed
- **Vector (ADR-021):** LanceDB (`lancedb>=0.13`) is a **core dependency** — sole vector backend; P1 SQLite brute-force fallback and `[lance]` extra removed; E4 rescore is LanceDB-native (IVF_HNSW_SQ / IVF_PQ + refine).

### Added — Phase 2 "Semantic memory"
- **Prompts subsystem (D-43):** prompts are data — 10-role YAML default pack (frontmatter + Jinja2, strict variables), `PromptRegistry` with config-layered overrides (auto version bump so E3 cache keys / E1 provenance change with content), `memspine prompts list|show|resolve`; prompt versions surface in `describe()` and extractor provenance.
- **Conflict ladder (M4):** deterministic R-ladder over `(entity, attribute)` fact keys — identity NOOP, trust gate (E1 seam), temporal supersede with bi-temporal interval closing + `evolve_to` chaining (D-42), historical backfill with closed validity; CONFLICT audit events; point-in-time `fact_at()` queries.
- **Two-stage dedup (M5/D-27):** datasketch MinHash-LSH stage-1 (signatures persisted on records, base64 in event payloads) + embedding-cosine stage-2 confirm; union-preserving merge (consent tags union, PII tier maxes upward, reinforcement bump) with MERGE audit events; signed-64-bit simhash.
- **Entity extraction (D-28):** LLM extractor (extract prompt + structured output) and guarded gliner2 `[ner]` provider behind `memories.semantic.policies.entity_extraction`; alias merges logged.
- **Structured output (D-31/E9):** format-aware parsing (YAML answers ≈ half the tokens of JSON) with the always-on json-repair net and pydantic output-model validation.
- Storage migration 0003 (fact keys + index); `:memory:` databases now use a named shared-cache URI with an anchor connection (true pooling — concurrent asyncio writes no longer race a single static connection).

### Added — Phase 1 "Working memory + retrieval"
- **Retrieval path:** `Engine.search()` — embed → vector search → M1 composite scoring (recency half-life, relevance, importance, utility modifier); `Engine.assemble()` — MMR selection under a token budget, θ_abstain honesty gate, **E2 cache-aware placement** (persona → skills → facts → [cache boundary] → episodic/working) with `boundary_index` exposed.
- **Embedding (D-08):** `EmbeddingService` port; fastembed default (lazy, threaded); deterministic `hash` provider for offline tests/CI; **E3 embedding cache** (`CachedEmbedding`, content-hash × embedder-id keys) over a KV port with in-process default.
- **Vector (D-09):** `VectorStore` port; zero-dep SQLite brute-force fallback (migration 0002); LanceDB adapter behind `[lance]` (auto-selected when installed); `VectorProjector` makes the vector index a rebuildable projection of the event log.
- **Working memory (M13.1):** MemGPT-style paging — bounded hot window (`policies.page_size`), overflow pages out to episodic via `DECAY_TRANSITION` events through the write door (identity preserved, version bumped); pinned persona block (`set_persona`) that paging never evicts and assembly places first.
- **LLM (D-07/D-39/D-46):** per-role router (extract/judge/chat); `OpenAICompatLLM` covering Ollama/vLLM/LM Studio/llama.cpp-server over the new core httpx client (ADR-012); guarded `llama_cpp` in-process provider `[llmlocal]`; always-on `lenient_json` repair (D-31).
- **Workers (D-16/D-17):** pipelines as plain idempotent functions (no runner imports — test-enforced), `TaskRunner` protocol, inline runner with D-18 dead-letter logging, sleep cycle (consolidate → decay → compress → [E7 slot] → event_log_prune) via `Engine.sleep()`; rolling-mode boot prune now runs through the pipeline.
- Examples: `01_quickstart.py`, `02_working_memory_and_assembly.py` (both offline-runnable).

### Added — Phase 0 "Substrate"
- **Event-sourced core (ADR-001):** append-only `memory_events` log behind a single write door; `Projector` ABC with durable high-water marks; `catch_up`/`rebuild` replay.
- **Configurable event-log retention (D-45, ADR-011):** `event_log.mode = full | rolling | ephemeral` + optional zstd payload compression; rolling prune never passes the slowest projector; reduced modes fail loudly (`RebuildUnavailableError`).
- **Universal memory record (M1):** bi-temporal columns, provenance + versioned lifecycle (D-42), PII/consent governance, scoring state, Memory-Firewall columns (E1), dedup sketch fields (D-27) — all in the initial Alembic migration.
- **Storage:** SQLAlchemy Core schema + async engine via aiosqlite (D-36/D-44, ADR-010); Alembic env + migration 0001; SQLite client owns WAL/pragmas (D-24).
- **Config system (D-11/D-12, ADR-006):** layered loader (defaults → template → user → env → kwargs) with `extends:` chains, cycle detection, per-key source tracking, `${secret:}` resolution; 6 shipped templates; design constants.
- **Registry (D-13):** §3 memory dependency graph with C1(b) auto-enable closure; reserved `namespaces.<ns>.memories` key rejected (D-14).
- **Policy contracts:** typed option schemas for all 9 policies (`extra="forbid"`); logic phased P1–P4.
- **Observability:** structlog setup + M11 vocabulary, test-locked to `EventKind`.
- **Secrets (D-22):** `SecretsService` port + `EnvSecrets` (env > `.env`), two-phase bootstrap.
- **Engine facade (D-01):** async `start/write/retrieve/rebuild/describe/stop` + thin sync wrappers; `memspine config validate|resolve` CLI with per-key source annotation.
- ADR-001…ADR-011; `docs/RESEARCH_NOVELTY.md` (paper contribution catalog); GitHub Actions CI (ubuntu + windows).

### Changed
- Structure plan v1.3: D-44 (aiosqlite core), D-45 (event-log retention); stale CJK tree remnants removed (D-34).
- `[graph]` extra temporarily empty until the ladybugdb fork is published (P6); `[community]` declared conflicting with `[ingest]`/`[all]` (graspologic numpy<2 vs magika numpy≥2.1).

### Fixed (post-implementation review — 8-angle multi-agent pass)
- `memory_events.seq` now uses SQLite `AUTOINCREMENT`: rowid reuse after rolling-mode pruning could resurrect seqs below projector high-water marks (silent event loss).
- Ephemeral mode keeps seqs **and** projector offsets purely in memory — a prior ephemeral run can no longer poison a later full-mode run on the same database file.
- `can_rebuild`/`rebuild()` honor the documented rolling-window semantics: an unpruned rolling log rebuilds; a pruned one raises `RebuildUnavailableError` with the reason.
- Rolling engines prune applied history at boot (`retention_days` is now real; continuous pruning joins the P3 sleep cycle).
- Offset checkpoints are atomic, advance-only native upserts (concurrent writes can no longer race an `IntegrityError` or regress a high-water mark); record upserts use native `ON CONFLICT`.
- `EnvSecrets`: a set-but-empty env var now beats the `.env` file (falsy-`or` bug).
- Loader: unknown `MEMSPINE_*` env vars are ignored instead of crashing the strict schema; YAML-hostile env values fall back to strings; missing config files raise `ConfigError`, not `FileNotFoundError`.
- `describe()` requires a started engine (previously reported a healthy world after `stop()`); rebuildability now comes from the storage capability, not re-derived config.
- Sync wrappers dispatch onto one long-lived background loop (no more per-call `asyncio.run` stranding aiosqlite connections) and refuse to run inside a live event loop.
- File-backed databases now migrate through Alembic on startup (`ensure_schema`, with pre-Alembic stamping); `create_all` remains only for `:memory:`.
- D-10 wired for real: `MissingServiceError` names the extra; `strict_services: false` degrades with a warning.
- Shipped `py.typed`; deduplicated `flatten_dotted`/canonical serialization/M11 constants; policy defaults bind `config/constants.py`; `RecordProjector` depends on a narrow `RecordStore` protocol.

### Notes
- 89 unit tests green; ruff + mypy --strict clean. Phase 1 (working memory + retrieval) is next.
