# memspine

## What This Is

`memspine` is an open-source, **event-sourced cognitive-memory engine** for AI agents: one clean
`Engine` API over a real write pipeline, hybrid + graph retrieval, and background learning
dynamics — with pluggable, composable stores. It is the *engine*, not a product. Nine memory
types (working · semantic · episodic · resource · procedural · reflective · associative ·
prospective · shared) sit on an append-only event log whose vector/graph/FTS/cache projectors are
always rebuildable. Target runtime: a pip-installable Python 3.13 library (uv, ruff, pytest,
mkdocs-material tooling). Status: pre-alpha, under active construction.

## Core Value

**Composable & swappable stores.** Every store — vector, graph, cache, SQL storage, LLM,
embedding, secrets — is pluggable behind a capability port. A developer swaps SQLite→Postgres,
local→cloud LLM (litellm), in-memory→Redis/LMDB cache, and env→AWS secrets **by config alone**,
with zero code change and `profile="simple"` staying byte-identical. If everything else fails,
this must hold: the engine never locks you into a backend.

## Requirements

### Validated

<!-- Shipped, review-passed, and green (full pytest suite + ruff + mypy --strict clean). -->

- ✓ **CORE-01…05** Event-sourced substrate (append-only log, rebuildable projectors, retention
  modes, config layering, hard-fail, firewall/provenance DDL) — Phase 1 (P0)
- ✓ **MEM-01…09** All nine memory types write→read through the real pipeline — Phases 2–8 (P1–P7)
- ✓ **RET-01…05** Cache-friendly assembly, semantic caching, hybrid RRF, rerank/MMR, quantized
  two-stage vector search — Phases 2/7/8
- ✓ **GOV-01…03** Memory Firewall (trust/quarantine/anomaly) + M7 hard erasure — Phase 5 (P4/E1)
- ✓ **LRN-01…03** Runner-agnostic pipelines, sleep-cycle, durable crash-resume — Phase 4 (P3)
- ✓ **PRM-01** Prompts-as-data subsystem (registry + YAML pack + output-model pairing) — Phase 3
- ✓ **PORT-01…06** Composable stores: LanceDB-sole vector, pluggable cache, pluggable secrets,
  litellm gateway, lexical parity, dialect-neutral Postgres storage — Phases 9–14 (ADR-021…025)
- ✓ **API-01…02** No-authn REST protocol + taskiq brokered runner — Phase 8 (P7)

### Active

<!-- Current scope: v0.1 release hardening. -->

- [ ] **REL-01**: USAGE.md / FEATURES.md / README document the shipped Engine/CLI/REST surface,
  verified against the code
- [ ] **REL-02**: Live-backend contract tests pass against a real Postgres (plus swappable
  cache/secrets/LLM round-trips), not just SQLite/in-memory
- [ ] **REL-03**: Alembic migration history squashed to a clean v0.1 baseline

### Out of Scope

<!-- Explicit v0.1 boundaries (deliberate non-goals, structure-plan §7 + locked ADRs). -->

- REST authentication/authorization — v0.1 ships **no-authn by design**; the `/write` path is an
  untrusted external channel behind the operator's own boundary (ADR-018). Deferred to v2.
- Per-namespace type enablement — config key reserved, not wired in v0.1.
- WS / gRPC / MCP protocols — seats reserved in `protocols/`, not built.
- Third-party memory plugins — interfaces provisional.
- File-native / Markdown profile — D-30 skipped.
- Weaviate / Neo4j production adapters beyond stubs — Postgres storage (ADR-025) and Redis/Valkey
  cache (ADR-022) shipped; other prod backends remain stubs.
- Benchmark leaderboard claims — harness-first; the (accuracy, tokens, latency) triplet rule.

## Context

- **Authoritative blueprint precedence.** `docs/memspine-structure-plan.md` (v1.5) is the buildable
  source of truth for phases and the decision register (D-01…D-54). The individual **ADRs are the
  locked decision records** it indexes. If a doc and the plan disagree, the plan wins — then the
  doc is fixed. This roadmap is derived from that plan's phase mapping (§5) + enhancement program
  (E1–E9) + the ADR-021…025 ports-completion effort.
- **Delivery history.** Two build efforts have landed and are green: (A) the P0–P7 cognitive-memory
  engine + the C6 combination matrix (15-boot round-trip harness); (B) a 6-phase composable-stores /
  ports-completion milestone (LanceDB-sole vector, shared cache, pluggable secrets, litellm,
  lexical parity, Postgres). 25 ADRs; decision register through D-54.
- **Read-first docs:** `docs/memspine-structure-plan.md`, `docs/UNIMEM_V2_REWORK_PROPOSAL.md`,
  `docs/DEPENDENCY_ANALYSIS.md` + `docs/PACKAGE_CATALOG.md`, `docs/FEATURES.md` + `docs/USAGE.md`.
- **Provenance caveat.** Requirements here are DOC/ADR-derived (the ingest set contained no PRD or
  SPEC) — treat them as capability requirements, not formal PRD acceptance contracts.

## Constraints

- **Event-sourced truth**: the append-only `memory_events` log is the single source of truth;
  vector/graph/FTS/cache are rebuildable projectors, never a second source of truth — ADR-001/011.
- **Slim core**: zero heavy deps in core; everything optional lives behind `pip install
  memspine[extra]`; torch/transformers never enter core. Only ADR-sanctioned core additions
  (LanceDB, litellm lazy-import, aiosqlite/greenlet, httpx) — ADR-002/010/012/021/024.
- **Ports & adapters**: Engine/memories/policies talk only to `services/*` capability ports;
  `clients/*` own connections; services never open a connection — ADR-004.
- **No runner lock-in**: background pipelines are plain idempotent step functions; runners
  (inline/DBOS/taskiq) decorate them — no runner imports inside pipeline code — ADR-005.
- **Profiles stay green**: every change keeps `profile="simple"` stable and byte-identical;
  default-off features (E4 quantization, hybrid RRF, ladybug graph) leave it untouched — ADR-006/020/021.
- **Hard-fail clearly**: a missing service raises `MissingServiceError` naming the extra, unless
  `strict_services: false` — ADR-006 / D-10.
- **Storage dialect-neutral**: event log + read model run identically on SQLite and Postgres via
  the shared `SqlStorage` base — ADR-025.
- **Graph default**: `sqlite_adjacency` (zero-dep); ladybug `[graph]` + kuzu `[kuzu]` opt-in — ADR-015.
- **Vector**: LanceDB is the sole core vector store; no SQLite brute-force fallback — ADR-021.
- **Async-first**, types everywhere (`py.typed`, mypy --strict clean); **prompts are data**
  (no inline strings) — ADR-009/010.
- **OSS posture**: Apache-2.0, semver, additive/conservative schema, mkdocs-material + one ADR per
  decision, evals packaged in-repo — ADR-007.

## Locked Decisions

Every ADR in the ingest set is **LOCKED (accepted)**. Precedence: ADR > SPEC > PRD > DOC. Amendment
chains are reconciled inline; the **current ruling** is stated where a later ADR superseded an
earlier one.

<decisions>
<decision id="ADR-001" status="LOCKED">Event-sourced core: append-only `memory_events` log is the single source of truth; every derived store (vector/graph/FTS/cache) is a rebuildable projector.</decision>
<decision id="ADR-002" status="LOCKED">Slim core + extras packaging: heavy deps live behind `pip install memspine[extra]`; torch/transformers never enter core.</decision>
<decision id="ADR-003" status="LOCKED">Store defaults: SQLite (relational/event-log/FTS5) + LanceDB in core; graph adapter (sqlite_adjacency default) + LMDB cache. Current ruling: vector = LanceDB-only (amended by ADR-021); graph default = sqlite_adjacency (amended by ADR-015).</decision>
<decision id="ADR-004" status="LOCKED">Two-layer ports & adapters: capability ports (`services/`) split from connection-owning adapters (`clients/`); services never open a connection.</decision>
<decision id="ADR-005" status="LOCKED">Background execution: pipelines are plain idempotent step functions decorated by pluggable runners (inline/DBOS/taskiq); no runner imports inside pipeline code.</decision>
<decision id="ADR-006" status="LOCKED">Config layering: schema defaults → template `extends:` → user YAML → env → kwargs, with per-key source tracking and cycle rejection.</decision>
<decision id="ADR-007" status="LOCKED">Public OSS from day one: Apache-2.0, semver, additive/conservative schema, mkdocs-material + ADR discipline, evals packaged.</decision>
<decision id="ADR-008" status="LOCKED">Code-level adoptions D-26…D-42: field-proven libraries (datasketch, gliner2, markitdown, chonkie, instructor, zstandard, litellm, SQLAlchemy Core, xxhash, fastuuid, pydantic, orjson, graspologic) + five MemOS patterns.</decision>
<decision id="ADR-009" status="LOCKED">Customizable prompt subsystem: versioned YAML default pack, PromptRegistry, config layering, pydantic output-model pairing, prompt CLI. Prompts are data, never inline strings.</decision>
<decision id="ADR-010" status="LOCKED">Async storage driver: aiosqlite + greenlet in core so SQLAlchemy Core storage runs a genuinely async engine; sync verbs wrap the async path.</decision>
<decision id="ADR-011" status="LOCKED">Configurable event-log retention: `event_log.mode` (full/rolling/ephemeral) + optional zstd payload compression; pruning never passes projector high-water marks (audit taint on loss).</decision>
<decision id="ADR-012" status="LOCKED">httpx is the core async HTTP client (`clients/http.py`). Current ruling: the hand-rolled openai_compat LLM path was replaced by litellm (ADR-024); httpx remains core.</decision>
<decision id="ADR-013" status="LOCKED">Memory-Firewall & M7 governance amendments: content-mutable erasure, deterministic gate, canonical quarantine, allow-listed lifecycle deltas (DECAY_TRANSITION).</decision>
<decision id="ADR-014" status="LOCKED">P5 lifecycle amendments: staged plans via RESOLVING, prompts enter the ladder at active, reflections carry caller trust.</decision>
<decision id="ADR-015" status="LOCKED">Associative links are LINK events riding the log; graph is a rebuildable GraphProjector; link budget enforced at creation; prune = weight-0 tombstone. Default graph store = sqlite_adjacency (amends ADR-003 D-26).</decision>
<decision id="ADR-016" status="LOCKED">Prospective watches + cross-namespace shared-memory grants map onto existing event-log columns/events — no new DDL or EventKind.</decision>
<decision id="ADR-017" status="LOCKED">P7 infrastructure: no-authn REST protocol, taskiq brokered runner, E5 assembly-time compression, opt-in E8 retrieval-quality stages (rerank, static prefilter, hybrid RRF).</decision>
<decision id="ADR-018" status="LOCKED">P7 review hardening: REST `/write` untrusted-channel trust boundary, forget/verify_forget/audit_taint scoping, TrustPolicy external channels, TaintReport.</decision>
<decision id="ADR-019" status="LOCKED">Hybrid retrieval: opt-in lexical BM25 port (SQLiteFTS5/Tantivy) fused with the vector leg via normalized RRF; LexicalProjector; default off, v0.2 flips on.</decision>
<decision id="ADR-020" status="LOCKED">Embedding quantization: two-stage quantized-prefilter/float-rescore vector search (LanceDB-native), Matryoshka truncation, opt-in model2vec static prefilter; default OFF; migration 0009.</decision>
<decision id="ADR-021" status="LOCKED">LanceDB is a core dependency and the sole vector backend; removes the SQLite brute-force vector store and `[lance]` extra; E4 rescore is LanceDB-native only.</decision>
<decision id="ADR-022" status="LOCKED">Shared, pluggable KVCache backend (memory/LMDB/Redis/Valkey) injected across the embedding and extractor caches.</decision>
<decision id="ADR-023" status="LOCKED">Pluggable secrets resolvers (AWS Secrets Manager / chained / env) selected via env var during pre-config bootstrap; boto3 behind `[aws]`.</decision>
<decision id="ADR-024" status="LOCKED">litellm is a core, lazily-imported single adapter for cloud + local LLM, embedding, and rerank; removes the hand-rolled openai_compat path. Supersedes the "litellm adapter-only" non-goal.</decision>
<decision id="ADR-025" status="LOCKED">PostgreSQL storage backend via a shared dialect-neutral `SqlStorage` base so the event log + read model run identically on SQLite and Postgres (psycopg3, Alembic; FTS5 lexical + sqlite_adjacency graph dialect-scoped).</decision>
</decisions>

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Event-sourced core (ADR-001) | Rebuildable projectors, auditability, no dual source of truth | ✓ Good |
| LanceDB as sole core vector store (ADR-021) | One vector path; native two-stage rescore; drop brute-force fallback | ✓ Good |
| litellm unified gateway (ADR-024) | Swap local↔cloud LLM/embedding/rerank by config; delete hand-rolled path | ✓ Good |
| Dialect-neutral SqlStorage + Postgres (ADR-025) | Prove the composability metric end-to-end (SQLite↔Postgres by config) | ✓ Good |
| Ports & adapters two-layer (ADR-004) | Every backend swappable behind a capability port | ✓ Good |
| REST ships no-authn in v0.1 (ADR-018) | `/write` is an untrusted external channel behind the operator boundary | — Pending (authn deferred to v2) |
| sqlite_adjacency default graph (ADR-015) | Zero-dep default; ladybug/kuzu opt-in pending a future ADR | ⚠️ Revisit (default may flip) |

---
*Last updated: 2026-07-10 after ingest bootstrap (new-project-from-ingest)*
