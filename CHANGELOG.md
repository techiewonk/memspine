# Changelog

All notable changes to memspine are documented here. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
