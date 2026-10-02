# Changelog

All notable changes to memspine are documented here. Format: [Keep a Changelog](https://keepachangelog.com/); versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added — integrity enforcement and read path (v0.3 research track; all opt-in, `profile="simple"` unchanged)
- **Enforced provenance (B0):** `integrity.implicit_parents: turn|session` records what a session reads (`search`/`shared_search(..., session_id=)`) and uses it as the parents of that session's next writes. Omitting `derived_from` can no longer launder trust. `end_session()` clears the ledger.
- **Live re-evaluation (B4′):** `integrity.live_reevaluation` re-checks each candidate against its current parents at read time. Quarantining, rolling back or revoking an ancestor propagates to its descendants.
- **Untrusted-note wrapper (B6):** `integrity.untrusted_wrap_below` renders low-trust records in assembly as labelled data, not instructions.
- **Action gate (B6) and messages (B5):** `authorize(record_ids, namespace, threshold)` → `AuthorizeDecision`, which permits a tool action only when every record behind it clears the threshold. `send()` delivers agent-to-agent messages through the write door. `effective_trust()` returns a record's view trust.
- **Repair and verification:** `repair_taint(seed)` rolls back a taint and re-derives benign descendants. `verify_integrity()` checks the hash chain and fingerprints, and re-checks MTI offline over the log (`IntegrityReport`).
- **Firewall parity (B8/B9):** `firewall.redact_secrets` (deterministic secret and PII regexes), `max_content_chars`, `protected_keys`. The summariser sanitises before it summarises, and summaries inherit instruction flags.
- **Temporal anchoring (C-1):** `write(..., valid_from=)` and per-message event times in `write_messages`. Dated rendering in the eval adapter.
- **Current-state view and retraction (C4′):** `read.current_state_view` renders `CURRENT (since …)` plus the superseded `HISTORY` for keyed facts. `retract(entity, attribute)` ends a fact with no successor, through a deterministic ladder rung.
- **Atomic-fact mining (C6′):** `consolidation.mine_facts` adds a sleep-cycle stage that mines dated facts once per session through the write door. Raw turns are kept.
- **Temporal and metadata legs (C3′):** `read.temporal_leg` and `read.metadata_leg` add RRF legs for absolute dates and entities named in the query.
- **Mode-routed `read()` (C7′):** full context when it fits, else session replay of neighbouring raw turns, else retrieve. The same erasure, taint and admission gates apply.
- **Anticipatory cues (C8′):** `add_cues(record_id, cues)` stores question-shaped retrieval keys. Cues are firewall-screened, their trust is capped at the target's, they are ignored below `read.cue_min_trust`, and they are never content.
- **Embeddings:** `embedding.request_dimensions` (e.g. Cohere embed-v4 at 1024) and asymmetric `query_input_type` / `document_input_type`, with a query-side cache.
- **Latest-dated slots (H19) and near-duplicate removal (H23):** the assembly options `latest_slots` (reserve the N most recent candidates) and `dedupe_jaccard` (drop near-duplicates at or above that word-set overlap). Off by default. H20 (hard exclusion of superseded facts) already holds: search never returns ARCHIVED records.
- **Anticipation stage (H8):** with `consolidation.anticipate`, a sleep stage asks the new `anticipate` role (falls back to `extract`) once per session for likely future questions. It stores them as firewall-governed cues on the turns that answer them, via `add_cues`, idempotent per session (T-Mem's mechanism, aimed at LoCoMo-Plus).
- **Core-terms lexical leg (H13):** `read.core_terms_leg` adds a BM25 probe over the question's content words, fused by RRF.
- **Contest + overlap rule (H9):** the conflict option `contest_ties` (with `contest_window_seconds` and `contest_trust_margin`) keeps both values when neither time nor trust decides between them. They are tagged `disputed`: the current fact stays the single active one, and the contender is retrievable with a zero-length interval. The current-state view marks `[DISPUTED …]`. Graphiti's overlap rule: a closed interval that ended before the current fact began never displaces it.
- **Session-mining prompt (H2):** a new `extract@session` variant (selected by `condition: session`) for `consolidation.mine_facts`: no pronouns, relative times rewritten to absolute dates, one self-contained fact each, a `date` field. A mined fact's `date` becomes its event time. The base `extract` prompt is unchanged.
- **Fact → source replay (H6):** in `read(mode="replay")`, a retrieved mined atomic fact also brings in the parent turn it best matches, plus that turn's neighbours (JustMem REPLAY).
- **Dated render (H5) and time order for ordering questions (H16):** `read.render: dated` prefixes records with their event date. `read.order_by_time_for_ordering` presents evidence chronologically for "first / latest / most recent" questions (`query_shape.is_ordering`).
- **Budget-aware candidates (H11) and relative floor (H4):** `read.candidate_pool` lets assembly draw from `candidate_pool × top_k` candidates, so the token budget decides how much evidence enters. `read.assembly.relative_floor` drops candidates scoring below that fraction of the best. Both off by default.
- **Compose read (H3):** `read(mode="compose")`, which `auto` picks for aggregation questions ("how many", "what books has X read", "list …"). It pools a 3× wider candidate set from the query and a core-terms probe, rank-fused, and selects session-diversely. Offline LoCoMo retrieval: going from top-10 to top-30 lifts multi-hop all-evidence coverage from 5% to 19% and any-evidence from 53% to 74%.
- **Relative-date resolution (H1):** `read.resolve_relative_dates` annotates phrases such as "yesterday", "last Friday", "two weeks ago" and "last month" with the absolute date, resolved against each record's event time (`core/temporal_resolve.py`, rules only). It agrees with LoCoMo gold in 126/129 resolvable temporal questions. The error analysis behind it: 88 of 120 wrong temporal answers gave a wrong date with the evidence in context. Evals: `error_analysis.py`, `temporal_check.py`.
- **Principal reputation (B7):** `integrity.principal_reputation`. Write trust is multiplied by min(1, 2 × Beta mean) of the principal's history, where bad = quarantined records, or the seed of a taint rollback (descendants are not blamed on their authors). It can only lower trust, so the trust horizon holds. `Engine.principal_reputation(p)`.
- **Relevance-first scoring (ADR-030, proposed):** `scoring.mode: relevance_first` ranks by relevance, and recency, importance and utility only break near-ties (`tie_break_weight`, default 0.05). The default stays `blend`. On LoCoMo, R@1 goes from 0.10 (blend) to 0.29.
- **Evals:** Bedrock helpers (Qwen3 reader/judge with `/no_think`, `CallBudget` hard cap), and a LoCoMo judge-label parser that uses the last verdict. Readers report cache-served input as `cached_prompt_tokens` (C9′). `read.record_access: false` makes reads side-effect free.

### Added — trust-horizon invariant, formerly "monotone trust invariant" (`integrity.*`, opt-in, ADR-029 *proposed*, D-56)
- **Provenance-carrying writes:** `write(..., derived_from=[ids])` records `source.parents`. With `integrity.enabled`, it caps trust at `min(base, view_trust(parent)…)` (× `derivation_decay`); an unreadable parent counts as 0.0.
- **Attenuated shared reads:** per-grant `kappa` (`edge_kappa` overrides, `product` or `min`) replaces the flat 0.3 cap when enabled. `search`/`shared_search` rank by score × view trust and drop records below `admission_threshold`.
- **Principal-bound corroboration:** promotion needs `source.principal`s that differ from the held record's and from every earlier corroborator's, checked against the log. Merges from a less-trusted duplicate no longer reinforce.
- **Forensics and repair:** reader-side `memory.expose` events (`EVENT_EXPOSE`); `audit_taint(..., cross_namespace=True)` follows declared parents across grants and lists exposed readers; `rollback_taint(seed)` archives the seed and every content-derived descendant.
- **`write_ex()`** → `WriteOutcome(record, action, occurrence_id)` exposes dedup merges to callers. **`assemble(..., shared=True)`** assembles over grants; under `integrity` its scores are rescaled so abstention still judges relevance.
- `integrity.verification_bonus` exists for baseline emulation only (it breaks the invariant on purpose).

### Fixed
- **`search` no longer returns grant/subscription bookkeeping** (`memory_type="shared"`) from the reader's own namespace. These records took top-k slots; `shared_search` already hid foreign ones.

### Added — evaluation harness (`evals/`, outside the wheel per D-35)
- **Plug-and-play evaluation:** any system x any dataset x one fixed protocol, in three interfaces — `DatasetAdapter`, `SystemAdapter` (sequential `insert` then bounded `query`, the LongMemEval-V2 precedent) and `RunProtocol` (reader, budget, judge, seed). Stdlib-only core: the baselines run with nothing installed.
- **Provenance is structural, not procedural.** `ResultWriter` cannot be built without a `RunManifest`; `DatasetInfo` cannot be built without a data revision; `JudgeSpec` has no default scale. A bare number is unreachable, and each run self-reports D16 admissibility.
- **Stage trace** per turn — `E_t`, `M_ctx,t`, `y_t`, `delta_t`, `P^u_t` — emitted from the first run, since retrofitting it costs a full re-run.
- **Metrics:** (accuracy, tokens, latency) reported together, per-stage cost across R/C/G/D/K and CPC per the loop-metric contract; unobservable stage cost is marked *unknown* rather than zero. 95% CIs bootstrapped by item, not by question.
- **Denominator preservation:** every scheduled question gets a row — `completed`, `truncated`, `error` or `unattempted`. `max_model_calls` caps a run; `expect_model_calls=False` makes "no model calls" a checked property.
- **Systems:** `no-memory`, `full-context`, `naive-rag`, `verbatim` (BM25 / fastembed / RRF hybrid) and a `memspine` adapter over the public facade only.
- **Datasets:** LoCoMo and LongMemEval S/M/oracle, plus a synthetic smoke set labelled unquotable. Nothing is ever downloaded by the harness.
- **CLI:** `python -m memspine_evals {smoke,c0-1,split}`. 74 offline tests, including the model-free multi-agent constructions (`memspine_evals.multiagent`).

### Changed
- **Vector (ADR-021):** LanceDB (`lancedb>=0.13`) is a **core dependency** — sole vector backend; P1 SQLite brute-force fallback and `[lance]` extra removed; E4 rescore is LanceDB-native (IVF_HNSW_SQ / IVF_PQ + refine).
- **Community detection (ADR-028, amends D-40):** `[community]` extra swapped `graspologic` → **`leidenalg`** (Leiden over `igraph`). `leidenalg` declares no `numpy` pin, so `[community]` no longer conflicts with `ingest` (numpy≥2.1) — the `[tool.uv].conflicts` block is removed, `community` is back in the `all` bundle, and `uv sync --all-extras` is unblocked. `detect_communities` keeps its signature/return; the hierarchical `max_cluster_size` bound is reproduced by a recursive re-partition splitter. Determinism preserved via fixed `seed`.

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
