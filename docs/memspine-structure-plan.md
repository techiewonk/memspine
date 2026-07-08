# memspine — Repository & Feature Structure Plan (v1.3, consolidated)

**Status:** all design questions resolved. This document is the buildable blueprint — **single source of truth**, now incorporating the Enhancement & Optimization program (Part B: E1–E9), the code-level adoption decisions D-26…D-35 (Part C), **and the second adoption pass D-36…D-42 + MemOS-derived patterns (Part D)**.
**Positioning:** `memspine` is the open-source *engine* (Graphiti-style) — a product can wrap it later.
**v1.1 changelog:** added Part C decisions D-26…D-35 from the 2026-07-07 dependency scan of `D:\mem`; confirmed D-09 graph = LadybugDB default with kuzu as out-of-box-supported alternative (D-26).
**v1.2 changelog (2026-07-07, second pass):** adopted **SQLAlchemy Core + Alembic** for storage (revises D-09 hand-rolled SQL, D-36); **xxhash + fastuuid** for fingerprints/IDs (D-37); **orjson** alongside pydantic for hot-path serialization (D-38); **first-class local/open-weight model hosting** — Ollama/vLLM/llama.cpp/LM Studio/OpenAI-compatible (D-39); **graspologic** optional graph community-detection for hierarchical consolidation (D-40); **fakeredis** test double (D-41); **five MemOS-derived patterns** (D-42, Part D). **Dropped CJK/jieba** (D-34 reversed to skip) and confirmed audio (soundfile/librosa), igraph, dydantic, nltk, mmh3, rake-nltk are **not adopted** (rationale in Part D §D.2). Prior graph/vector/embedder defaults unchanged.
**v1.4 changelog (2026-07-07, P1):** added **D-46 httpx in core** (ADR-012) for the D-39 OpenAI-compatible LLM surface; P1 shipped with a zero-dep **SQLite brute-force vector fallback** (so the core install retrieves semantically without `[lance]`) and a deterministic **hash embedding provider** for offline tests/CI alongside the fastembed default.
**v1.3 changelog (2026-07-07, P0 kickoff):** adopted **aiosqlite + greenlet in core** for the SQLAlchemy async engine (D-44, ADR-010); added **D-45 configurable event-log retention** — `event_log.mode ∈ {full, rolling, ephemeral}` + optional zstd payload compression — so the append-only `memory_events` log can be bounded or disabled on storage-constrained deployments without silently breaking the event-sourced contract (ADR-011); removed the stale `services/lexical/cjk.py` / `[cjk]` remnants from the tree (D-34 was reversed to skip).

## Locked decision register (traceability)

| # | Decision | Value |
|---|---|---|
| D-01 | Name / facade | `memspine`, entry class `Engine` |
| D-02 | Python | 3.13 floor |
| D-03 | Packaging | slim core + extras: `pip install memspine[lance,graph,rest,aws,dbos,taskiq]` |
| D-04 | Tooling | uv, ruff, pytest, mkdocs-material |
| D-05 | `clients/` meaning | service clients (LLM, AWS) with settings — not the user API |
| D-06 | Protocols | REST only in v1; layout reserves WS/gRPC/MCP |
| D-07 | LLM providers v1 | local (Ollama/OpenAI-compatible) + AWS Bedrock |
| D-08 | Embedder default | fastembed (ONNX, CPU-friendly) |
| D-09 | Stores | SQLite (relational/event-log/FTS5) · **LanceDB** (vector) · **LadybugDB** (graph, default) · **LMDB** (cache) — see **D-26** for the graph alt; storage access layer = **SQLAlchemy Core + Alembic** (D-36). Graph default amended by D-49/ADR-015: v0.1 ships `sqlite_adjacency`, ladybugdb reserved until published |
| D-10 | Missing service | hard-fail; `strict_services: false` dev escape hatch |
| D-11 | Config layering | defaults → template → user YAML → env (.env ∥ AWS Secrets Manager as peers) → runtime kwargs |
| D-12 | Templates | overlays + `extends:` + `memspine config resolve` |
| D-13 | Memory deps | C1(b): auto-enable dependencies with logged notice |
| D-14 | Combination granularity | v0.1 per-instance type enablement + per-namespace *policy* overrides; per-namespace *type* enablement reserved for v0.2 (config key reserved now) |
| D-15 | Custom memory types | registry internal-only v1; interfaces written plugin-ready |
| D-16 | Background execution | `TaskRunner` seam with three runners: `inline` / **`dbos`** (blessed worker; SQLite→Postgres) / `taskiq` (Valkey-Streams & other brokers) |
| D-17 | Anti-lock-in rule | pipelines are plain idempotent step functions; runners decorate them |
| D-18 | Dead-letter severity | consolidation = warning; M7 hard-delete cascade = alert |
| D-19 | Evals | repo root, not in `src/`; hardening deferred |
| D-20 | Docs | mkdocs user docs + `docs/adr/` from day one |
| D-21 | Audience | public OSS from day one — semver, conservative schema |
| D-22 | Service architecture | two-layer ports-and-adapters: `services/` = capability ports (now incl. **llm** and **secrets**); `clients/` = shared transport only (boto3 session, httpx pool) |
| D-23 | Enhancement program | E1–E9 adopted (Part B): Memory Firewall (E1) is a Phase-4 headline with its 3 columns in Phase-0 DDL; cache-aware assembly (E2), embedding cache (E3), prompt micro-opts (E9) fold into Phases 1–3; E4/E5/E6/E8 are config-activated; E7 ships as a hook only |
| D-24 | Clients layer scope | `clients/` holds connection clients for **every** external system (sqlite, lancedb, ladybug, lmdb, aws, http, + prod stubs); services never open connections — lifecycle manager injects clients, closes them centrally |
| D-25 | Lexical search | first-class `services/lexical/` port: **LanceDB built-in Tantivy FTS default** with `[lance]`; SQLite **FTS5/BM25** core-install default; standalone Tantivy for non-Lance configs; ILIKE fallback; prod swap-ins tsvector+GIN and **VectorChord-BM25**; RRF implemented once in the port. **BUILT in v0.1 (ADR-017 §5): SQLite FTS5/BM25 + `rrf_fuse`, wired into `Engine.search` as a rebuildable projection; opt-in via `read.hybrid` (default off, default-on deferred to v0.2 for backward-compat); Tantivy stubbed `[tantivy]`; LIKE fallback when the build lacks FTS5** |
| **D-26** | **Embedded graph store** | **LadybugDB default** (out-of-box supported, `[graph]`); **kuzu first-class alternative adapter** (`[kuzu]`, mature embedded Cypher — proven by graphiti + cognee); `sqlite_adjacency` zero-dep fallback. **AMENDED by D-49/ADR-015: v0.1 default = `sqlite_adjacency`; `[graph]` extra empty until the pinned ladybugdb fork publishes; `ladybug` provider reserved (raises `MissingServiceError`)** |
| **D-27** | **Dedup engine** | **datasketch MinHash-LSH** stage-1 candidate generation → embedding-cosine stage-2 confirm (M5) |
| **D-28** | **Local entity extraction** | **gliner2** CPU zero-shot NER behind a config flag; LLM extraction default fallback (M13.3) |
| **D-29** | **Multi-format ingest / chunking** | **markitdown** (doc→text) + **chonkie** (chunking) under a `[ingest]` extra (P1) |
| **D-30** | **File-native profile** | **Skipped for v0.1** — vector/graph/SQLite-first; not a goal, interface not reserved |
| **D-31** | **Structured LLM output** | **instructor** for extract/judge roles (`[structured]`); **json-repair** always-on safety net (core) (E9) |
| **D-32** | **Cold-tier compression** | **zstandard** for dormant/cold memory compression + fingerprinting (M6) |
| **D-33** | **LLM gateway** | keep `services/llm` per-role providers (D-07/D-22); **litellm only as an optional adapter**, never core |
| **D-34** | ~~Multilingual lexical~~ | **REVERSED → SKIP.** CJK/jieba not needed; no `[cjk]` extra. (cn2an/xpinyin/hanziconv were CJK-normalization in MemoryBear only.) |
| **D-35** | **Eval assertions** | **deepeval** in `evals/` only, not shipped in the wheel (respects D-19) |
| **D-36** | **Storage access layer** | **SQLAlchemy Core + Alembic** — Core (not full ORM) preserves the single-write-door/event-log discipline (D0.1) while replacing hand-rolled numbered `.sql`; Alembic owns migrations. `sqlmodel` still rejected. |
| **D-37** | **Hashing & IDs** | **xxhash** (xxh64) for content fingerprints / simhash / cache keys / blob addressing; **fastuuid** for hot-path record IDs. (xxhash proven in MemoryBear graphrag.) |
| **D-38** | **Serialization** | **pydantic** for models/validation (not either/or with orjson) + **orjson** for hot-path JSON — event-log payloads, REST responses, structured logs. Both in core. |
| **D-39** | **Local / open-weight model hosting** | First-class: **Ollama · vLLM · llama.cpp (llama-cpp-python) · LM Studio · any OpenAI-compatible endpoint**, alongside Bedrock (extends D-07). `services/llm/local.py` targets the OpenAI-compatible surface all of these expose. |
| **D-40** | **Graph community detection** | **graspologic** (`hierarchical_leiden`) optional under `[community]` — algorithmic hierarchical clustering for associative-memory consolidation / summary-parent nodes (MemoryBear + MemOS-reorganizer pattern). |
| **D-41** | **Test doubles** | **fakeredis** for mocking cache/broker (Valkey/Redis) in unit tests; keeps `[valkey]`/`[taskiq]` paths testable without a server. |
| **D-42** | **MemOS-derived patterns** | Adopt five patterns (Part D): typed-memory MemCube container · rich provenance + versioned lifecycle · Redis-Streams scheduler w/ per-scope queue isolation · background graph reorganizer · hybrid + strategy reranking. Avoid KV-cache activation memory + backend sprawl. |
| **D-43** | **Prompt subsystem (customizable)** | First-class `prompts/` package (Part E): **YAML default pack (frontmatter: id/version/role/output_model/format + Jinja2 body)** + `PromptRegistry` keyed `(role, name, version)` + **config-layered overrides (D-11/D-12)** + per-role binding + **pydantic output-model pairing (instructor, D-31)** + versioned lifecycle (rides procedural draft→…→active) + `memspine prompts` CLI. Self-optimization is an RG hook only. |

| **D-44** | **Async storage driver** | **aiosqlite + greenlet in core** — SQLAlchemy async engine (`sqlite+aiosqlite`) keeps the whole stack async-first (D-01) with two tiny pure deps; sync facade wraps the async engine, never a second engine. (ADR-010) |
| **D-45** | **Event-log retention (configurable)** | `event_log.mode`: **`full`** (default, append-only forever) · **`rolling`** (prune events already applied by *every* registered projector and older than `retention_days`; rebuild limited to the window) · **`ephemeral`** ("off": events dispatched to projectors but not persisted; `rebuild()`/`audit taint` unavailable — hard warning at startup). Optional `event_log.compress` = zstd payload compression at rest (reuses D-32). Pruning never passes a projector high-water mark. (ADR-011) |
| **D-46** | **HTTP client in core** | **httpx** — `clients/http.py` owns one async client pool (D-24); required by `services/llm/local.py` since every D-39 host exposes the OpenAI-compatible HTTP surface. Tiny, pure-Python (httpcore+h11). (ADR-012) |
| **D-47** | **Firewall & M7 governance amendments** | Log append-only for *structure*, content-redactable under M7 (shared redact/verify walker incl. `history`/merge/cold-tier carriers) · firewall gate deterministic-only (`firewall_flag` prompt = reserved second stage) · `RecordStatus.QUARANTINED` canonical, boolean column = projected index · lifecycle deltas ride `DECAY_TRANSITION` behind a projector allow-list (no trust laundering) · trust options bounds-validated; derived writes inherit min member trust. (ADR-013) |
| **D-48** | **P5 lifecycle amendments** | Staged plans held via `RESOLVING` (quarantine tier reserved for the firewall, ADR-013 §3); corroboration on procedural restores stage-consistent status (never blanket ACTIVATED) and `same_fact` is scoped by `memory_type` · prompt versions enter `active` by construction in v0.1 (registry ladder deferred; sync under ns write lock, stated firewall exemption) · reflections: `role="assistant"` default (never privileged system), trust capped at min parent trust, parents namespace-checked · `SkillStage` enum-typed in core; `reflection_depth ≤ 2` at the field. (ADR-014) |
| **D-49** | **Associative links & graph projection** | `EventKind.LINK` (`memory.link`, payload `{src, dst, rel, weight, reason}`) — links are new information and ride the log; graph = rebuildable GraphProjector over WRITE/LINK/FORGET + derivation payloads · link budget enforced at creation (`ConflictError`), prune = weight-0 tombstone LINK (replay-deterministic; provenance/reorganize links budget-exempt) · `sqlite_adjacency` default graph (ladybugdb unpublished; kuzu `[kuzu]` first-class alt) · PPR pure-Python bounded · reorganizer writes consolidation-shaped community parents (min-member trust, D-47 §5), no-op without `[community]`. (ADR-015) |
| **D-50** | **Prospective watches & shared grants** | Watch = `memory_type="prospective"` record, ONE trigger: due time rides `valid_from` (bi-temporal reuse, no new DDL) or watched fact key rides `entity`/`attribute` · firing = pure functions (explicit `now`; invalidation from M4 CONFLICT events with truth-changing actions at-or-after the watch) · pull-based v0.1 (`due()`, read-only `check_watches` sleep step; push deferred to taskiq) · acknowledge = delta `DECAY_TRANSITION` `reason="watch_fired"` · `find_active_fact` semantic-only (a watch/skill key must never be the M4 incumbent) · grant = WRITE of a `shared` bookkeeping record (grantor ns, entity=grantee, attribute="grant", canonical-JSON scope; firewall-exempt like prompt sync), revoke = delta archive, grantor-lock · cross-grant reads: trust capped at `TRUST_RETRIEVED_CAP`, quarantined/non-ACTIVATED never cross, live views (never copied), `shared` bookkeeping never crosses, no events appended for foreign hits · enforcement = `core.namespace.grant_allows` (ONE point) · subscriptions = standing-query records, pull-based. (ADR-016) |
| **D-51** | **P7 infrastructure: REST · taskiq · E5 · E8** | REST (`[rest]`): `create_app(engine)` wraps ONE caller-owned engine (D-06), namespace from `X-Memspine-Namespace` header via an overridable `resolve_namespace` dependency — **NO authn in v0.1; caller→namespace binding is the deployer's job** (ADR-016 Q2 answered "out of scope, seam provided") · errors `Conflict`→409 / `MissingService`→501 / `Memspine`→400 / unknown→500 (no stack leaks), orjson responses, `MemoryRecord` as the wire shape · taskiq runner (`[taskiq]`, D-42 §3): per-scope stream keys `memspine:<scope>:<label>`, priority labels pinned to the `PIPELINES` set, XADD→XREADGROUP→XACK with **XAUTOCLAIM claim-recovery** (dead-letter stays pending), broker outage degrades to inline; in-process execution honesty note (ctx not payload-addressable), tested via fakeredis (D-41) + pure fns · E5 assembly stage (config `read.compression.assembly`, default OFF): drop-lowest → llmlingua `[compress]` (skip-log) → provider-compaction seam; **persona / instruction-flagged / RESOLVING blocks never dropped or compressed** (E1 wrap survives) · E8 stages (default OFF): `[static_prefilter?] → hybrid → [rerank?] → score → MMR → assemble`, rerank = fastembed cross-encoder or flashrank (**new `[rerank]` extra**), concat_background strategy text (D-42 §5), min-max score normalization, lazy build + skip-log; **foreign trust-capped hits get NO rank penalty** (trust already carries the signal). (ADR-017) **UPDATED (ADR-017 §5, now recorded in full by ADR-019/D-53): the `services/lexical`/D-25 port is now BUILT — SQLite FTS5/BM25 + `rrf_fuse` (implemented once in the port) + `LexicalProjector` (rebuildable projection) + bounded BM25 LRU corpus cache; Tantivy stubbed `[tantivy]`; LIKE fallback when the build lacks FTS5. Hybrid RRF fusion is wired into `Engine.search` and is OPT-IN via `read.hybrid` (default off → vector-only, bit-identical to before, no lexical store/projector built; default-on is the intended v0.2 flip). `Engine.search` is documented "vector/hybrid (opt-in)". `static_prefilter` still applies POST-fusion (not a true pre-vector prefilter). The E1 quarantine/status gate runs on the FUSED candidates.** |
| **D-52** | **P7 review hardening (security + robustness)** | Engine-level tenant isolation closed beyond the documented no-authn REST stance: **`memory_type="shared"` reserved from the public `write()`** (grants come only from `grant()`/`subscribe()`, else a crafted content forges a live grant, SEC-H1) · **`forget`/`verify_forget`/`audit_taint` namespace-scoped** — a foreign seed raises the ADR-014 anti-oracle error (SEC-C2/C3); forget stays idempotent on an ABSENT id for the hard-delete redaction retry (durability) while soft-forget of a missing id raises · **REST writes forced onto the external `rest` channel** so a caller-claimed `role` cannot escalate trust past `TRUST_RETRIEVED_CAP` (SEC-C1); **REST body cap → 413** (`REST_MAX_BODY_BYTES`, SEC-M3); **broker-URL userinfo redacted from taskiq logs** (SEC-M1) · correctness: `due()` rejects naive `now` (COR-1), `shared_search` truncates merged results to `top_k` (COR-2), reranker-construction failures self-disable the stage (COR-3) · silent-failure closures: ephemeral-invalidation-watch warning (SF-1), degenerate-rerank-score log (SF-2), llmlingua per-record guard (SF-4), taskiq latch-reset + narrowed except + wired XAUTOCLAIM recovery (SF-5), REST typed-error logging (SF-6), per-grantor `shared_search` containment (SF-7) · completeness: acknowledge refuses a quarantined watch (CMP-1), fastembed reranker names the upgrade remedy (CMP-2), REST `POST/GET /subscriptions` + `GET /grants` (CMP-3). REST admin verbs (`/sleep`, `/rebuild`, `/audit/taint`) documented internal-only. (ADR-018) |
| **D-53** | **Hybrid retrieval hardening (L1)** | The D-25 lexical/BM25 port + RRF hybrid is recorded in its OWN ADR (ADR-019), replacing the amended-in-place ADR-017 §5 note (which dangled a reference to "the ADR-018 deferral" — ADR-018 carries no lexical text). Hybrid stays **opt-in via `read.hybrid` (default off); default-on is the v0.2 flip.** L1 review hardening: **RRF scores normalized to [0,1]** (divide by the theoretical max `2/(k+1)`) before feeding the M1 composite — raw `1/(k+rank)` collapses relevance so recency dominates · each leg fetches `top_k * LEXICAL_FETCH_MULTIPLIER` before fusion (recall) · **NUL/control-char stripped from indexed content** (a NUL bind is a projector poison-pill) **and from queries**; query bounded by `MAX_LEXICAL_QUERY_CHARS` **before** it becomes a cache key + `MAX_LEXICAL_QUERY_TERMS` cap on the OR-parse; LIKE fallback bounded (`LEXICAL_LIKE_SCAN_MAX_ROWS`); REST `query` gets `max_length` · lexical leg in `Engine.search` try/except → degrades to vector-only (never crashes) · **cache generation counter** — a search only caches if no mutation raced it (no stale-serve) · **`verify_forget` inspects the lexical `exists()`** (new port method) and reports `lexical_absent` (memory_fts holds raw content) · `Engine.search` raises on `top_k < 1` · quarantined content stays in cleartext FTS by design (promotion is a non-WRITE `DECAY_TRANSITION`; the retrieval gate excludes it — accepted tradeoff). (ADR-019) |
| **D-54** | **Embedding quantization + rescore (E4)** | The zero-dep SQLite vector store implements the E4 two-stage `search_rescore` in pure Python (no numpy in core): a **quantized (int8/binary) or Matryoshka-truncated prefilter** over an oversampled candidate window (`RESCORE_OVERSAMPLE`, 4×) → **exact float32 cosine rescore** → `top_k`. int8 = symmetric linear quantize to [-127,127] over the unit-sphere range; binary = sign-bit pack + Hamming prefilter. **Storage layout:** the float32 `vector` is kept for the rescore; migration 0009 adds nullable `memory_embeddings.quantized_vec` (the int8/binary codes of the optionally-truncated vector) + `quantization` (the scheme). Both NULL on the default path. **Manifest-driven with an explicit `vector.quantization` override** (`auto`|`none`|`int8`|`binary`); Matryoshka is manifest-only (smallest declared prefix dim). **Default OFF** — the default embedders (hash/fastembed) declare no capability, so `search_rescore` degenerates to `query` and `profile="simple"` is byte-identical; `Engine.search` dispatches to `search_rescore` only when active, at the vector leg before fusion/gate/rerank. model2vec is the opt-in `[static]` static-embedding prefilter (`services/embedding/static_local.py`): a missing extra hard-fails as a *provider* (D-10) but skip-logs and no-ops as a *prefilter stage*. LanceDB delegates quantization to its native index (rescore SQLite-store-only). (ADR-020) |

**Anti-decisions (recorded):** litellm as *core* LLM layer — rejected (adapter only, D-33) · sqlmodel for storage — rejected (D-36 uses SQLAlchemy Core) · celery/apscheduler workers — rejected (D-16) · baml — rejected for instructor (D-31) · **jieba/CJK — dropped (D-34)** · **nltk in core — rejected** (heavy corpora downloads; Tantivy/FTS5 tokenizers suffice) · **igraph — rejected** (networkx is the pure-Python default; graspologic covers the community-detection need, D-40) · **dydantic — deferred** (only a transitive of langmem/trustcall; revisit for D-15 dynamic custom-type schemas) · **soundfile/librosa — deferred** to a future `[audio]` multimodal extra (SimpleMem-Omni-only) · **mmh3 / rake-nltk — not needed** (xxhash covers hashing; keyword tags come from NER/LLM).

---

## 1. Repository tree (annotated — v1.1 additions marked ★NEW)

```text
memspine/
├── pyproject.toml                  # uv-managed; [project.optional-dependencies] defines the
│                                   #   extras matrix (§2); ruff+pytest config lives here too
├── uv.lock
├── LICENSE                         # Apache-2.0 (matches LanceDB/Graphiti ecosystem norms)
├── README.md                       # 90-second pitch + quickstart + template table
├── CHANGELOG.md                    # keep-a-changelog format; semver discipline (D-21)
├── CONTRIBUTING.md                 # dev setup (uv sync), test matrix, ADR process
├── .env.example                    # every env var the loader understands, commented
├── justfile                        # dev commands: just test / lint / evals / docs / rebuild
├── .github/workflows/{ci.yml,release.yml}
│
├── src/memspine/
│   ├── __init__.py                 # re-exports: Engine, __version__
│   ├── engine.py                   # ── THE FACADE ── (startup sequence §4)
│   ├── cli.py                      # `memspine` CLI (typer): config / tasks / rebuild / forget / audit taint
│   │                               #   / ★prompts list|show|diff|resolve|set|rollback (D-43)
│   ├── exceptions.py               # ConfigError, MissingServiceError (names the missing extra), ConflictError…
│   ├── py.typed
│   │
│   ├── core/                       # ── D0 SUBSTRATE: never optional, zero heavy deps ──
│   │   ├── events.py               # append-only memory_events log (D0.1)
│   │   ├── records.py              # universal record model (M1): bi-temporal cols, provenance, pii_tier,
│   │   │                           #   consent_tags, scoring state, + E1 trust/quarantined/instruction_flag,
│   │   │                           #   ★NEW simhash + minhash fields (D-27 two-stage dedup)
│   │   ├── namespace.py            # hierarchical paths, grants, the ONE enforcement point (M8.5)
│   │   ├── projector.py            # Projector ABC: idempotent apply(event), high-water mark, rebuild()
│   │   ├── registry.py             # internal registry (D-15): types + services declare themselves; deps graph
│   │   └── policies/
│   │       ├── base.py             # Policy protocol + config-binding helper
│   │       ├── scoring.py          # recency/relevance/importance + utility modifier (M1)
│   │       ├── consolidation.py    # triggers (session/heat/sleep) + 5-stage contract (M2)
│   │       ├── decay.py            # Ebbinghaus tiers, reinforcement rules (M3)
│   │       ├── conflict.py         # bi-temporal ADD/UPDATE/INVALIDATE/NOOP + R1–R5 ladder (M4)
│   │       ├── dedup.py            # ★NEW D-27: datasketch MinHash-LSH stage-1 → cosine stage-2;
│   │       │                       #   two-stage detect + union-preserving merge (M5)
│   │       ├── compression.py      # views-not-replacements summarization (M6);
│   │       │                       #   ★NEW D-32: zstandard cold-tier compress+fingerprint;
│   │       │                       #   E5 assembly-stage llmlingua fallback [compress]
│   │       ├── retention.py        # retention classes, legal hold, referential retention (M2/M3/M7)
│   │       ├── assembly.py         # ReadPolicy: placement order, envelopes, θ_abstain, MMR (M12) + E2
│   │       └── trust.py            # MEMORY FIREWALL policies (E1)
│   │
│   ├── memories/                   # ── ONE SUBPACKAGE PER TYPE; all optional & combinable ──
│   │   ├── base.py                 # BaseMemory ABC: needs[], required_services, on_write/on_read/on_manage
│   │   ├── working/                # manager.py, paging.py (MemGPT-style), persona.py
│   │   ├── episodic/               # store.py, sessions.py (boundary detection), timeops.py
│   │   ├── semantic/
│   │   │   ├── store.py            # fact store wired to conflict ladder
│   │   │   ├── entities.py         # ★NEW D-28: gliner2 CPU NER (flag) → entity resolution BEFORE conflict
│   │   │   │                       #   detection (M13.3); LLM extraction fallback; alias merges logged
│   │   │   └── facts.py            # (entity, attribute) keying, point-in-time queries
│   │   ├── procedural/             # skills.py, lifecycle.py (dry-run gate); prompt_registry.py = thin
│   │   │                           #   binding that stores prompt-as-memory versions by ID from prompts/
│   │   │                           #   registry (definition lives in prompts/, D-43 — no duplication)
│   │   ├── associative/            # projector.py, links.py (budget), ppr.py, evolution.py (bounded A-MEM);
│   │   │                           #   ★NEW D-40: communities.py — graspologic hierarchical_leiden →
│   │   │                           #   summary-parent nodes feed consolidation  [extra: community]
│   │   ├── reflective/             # reflections.py, guards.py (depth cap = 2)
│   │   ├── prospective/            # triggers.py, watches.py (fires on M4 invalidation)
│   │   ├── shared/                 # grants.py (source authority R2), subscriptions.py
│   │   └── resource/
│   │       ├── blobs.py            # content-addressed store (sha256 → file; S3-class later)
│   │       └── extraction.py       # ★NEW D-29: markitdown (docx/pdf/pptx/xls/html → text) +
│   │                               #   chonkie (chunking); only extracted text indexed, PII tier
│   │                               #   propagates (M13.9)                       [extra: ingest]
│   │
│   ├── services/                   # ── PORTS: capability interfaces the Engine consumes (M14) ──
│   │   ├── base.py                 # ServiceAdapter ABC + CapabilityManifest dataclass
│   │   ├── llm/
│   │   │   ├── base.py             # LLMService protocol, ROLES: extract / judge / chat;
│   │   │   │                       #   ★NEW D-31: instructor structured-output wrapper [structured] +
│   │   │   │                       #   json-repair always-on safety net (core)
│   │   │   ├── local.py            # ★D-39: Ollama · vLLM · LM Studio · any OpenAI-compatible endpoint
│   │   │   │                       #   via clients/http (all expose the OpenAI schema)
│   │   │   ├── llama_cpp.py        # ★D-39: in-process open-weight inference   [extra: llmlocal]
│   │   │   ├── bedrock.py          # AWS Bedrock via clients/aws           [extra: aws]
│   │   │   └── litellm_adapter.py  # ★NEW D-33: OPTIONAL unified gateway; never a core dep [extra: litellm]
│   │   ├── secrets/                # base.py, env.py (default), aws.py [extra: aws]
│   │   ├── embedding/              # base.py, fastembed_local.py (DEFAULT D-08), bedrock.py [aws]
│   │   ├── lexical/                # base.py (LexicalStore port + rrf_fuse here), sqlite_fts5.py (core DEFAULT),
│   │   │                           #   projector.py (rebuildable projection), tantivy.py [tantivy] (stub),
│   │   │                           #   (LIKE fallback lives in sqlite_fts5.py; postgres_tsvector / vchord_bm25 [postgres] later)
│   │   │                           #   (no CJK module — D-34 reversed to skip)
│   │   │                           #   ✅ BUILT in v0.1 (ADR-017 §5): FTS5/BM25 + RRF, opt-in via read.hybrid (default off; default-on = v0.2)
│   │   ├── rerank/                 # E8 cross-encoder rerank (D-51/ADR-017): base.py (Reranker port +
│   │   │                           #   concat_background), fastembed_rerank.py (core dep), flashrank_rerank.py [rerank]
│   │   ├── storage/                # ★D-36: SQLAlchemy Core (not ORM) + Alembic
│   │   │   ├── base.py             # relational + event-log persistence protocol (SA Core tables/metadata)
│   │   │   ├── sqlite/
│   │   │   │   ├── engine.py       # WAL, single write door (D0.1), JSON1; SA Core engine; orjson payloads
│   │   │   │   └── alembic/        # ★Alembic env + versioned migrations (replaces numbered .sql)
│   │   │   └── postgres.py         # thin stub v1 (same SA Core layer)         [extra: postgres]
│   │   ├── vector/
│   │   │   ├── base.py             # VectorStore protocol (upsert/query/filter/delete-by-subject)
│   │   │   ├── lancedb_store.py    # DEFAULT (D-09): disk indexes, Tantivy FTS  [extra: lance]
│   │   │   └── weaviate.py         # prod swap-in stub                          [extra: weaviate]
│   │   ├── graph/
│   │   │   ├── base.py             # GraphStore protocol (Cypher-ish surface)
│   │   │   ├── ladybug.py          # RESERVED (D-26 am. D-49): raises MissingServiceError until the fork publishes [extra: graph]
│   │   │   ├── kuzu.py             # ★NEW D-26: first-class embedded-Cypher alt  [extra: kuzu]
│   │   │   ├── sqlite_adjacency.py # zero-dep fallback for shallow graphs
│   │   │   └── neo4j.py            # prod swap-in stub                          [extra: neo4j]
│   │   └── cache/
│   │       ├── base.py             # KV protocol w/ TTL
│   │       ├── lmdb_cache.py       # DEFAULT (D-09): hot working-memory buffer  [extra: lmdb]
│   │       ├── semantic.py         # E3: embedding cache + extraction/judge cache + retrieval micro-cache
│   │       └── valkey.py           # prod swap-in stub                          [extra: valkey]
│   │
│   ├── clients/                    # ── ADAPTERS: connection clients for EVERY external system (D-24) ──
│   │   ├── base.py                 # Client ABC: settings, connect/close/health, retry/timeout/backoff
│   │   ├── sqlite.py · lancedb.py [lance] · ladybug.py [graph] · kuzu.py ★NEW [kuzu] · lmdb.py [lmdb]
│   │   ├── aws.py [aws] · http.py · postgres.py [postgres] · valkey.py [valkey]
│   │   ├── weaviate.py [weaviate] · neo4j.py [neo4j]
│   │
│   ├── workers/                    # ── BACKGROUND EXECUTION (D-16/17) ──
│   │   ├── pipelines.py            # ★ ANTI-LOCK-IN CORE: consolidation, decay sweep, compression
│   │   │                           #   (zstandard cold-tier D-32), tier transitions, hard-delete cascade
│   │   ├── runner.py · inline.py (DEFAULT) · dbos_runner.py [dbos] · taskiq_runner.py [taskiq] · schedule.py
│   │
│   ├── config/
│   │   ├── schema.py               # pydantic-settings tree; namespaces.<ns>.memories RESERVED (D-14)
│   │   ├── loader.py               # layering (D-11) + extends: (D-12) + per-line source tracking
│   │   ├── constants.py            # RRF k=60, dedup cos=0.92, ★NEW minhash_perm=128 / lsh_threshold (D-27),
│   │   │                           #   link budget=12, reflection depth=2, θ_abstain=0.25, decay thresholds
│   │   └── templates/              # base.yaml + voice/coding/personal/regulated_financial/multi_agent.yaml
│   │
│   ├── prompts/                    # ── ★NEW D-43: CUSTOMIZABLE PROMPT SUBSYSTEM ──
│   │   ├── base.py                 # Prompt object: id, version, role, template (Jinja2), output_model
│   │   │                           #   (optional pydantic schema → instructor D-31), token_budget,
│   │   │                           #   format∈{json,yaml,cod} (E9); render(context) → messages
│   │   ├── registry.py             # PromptRegistry keyed (role, name, version); resolves ACTIVE prompt
│   │   │                           #   via config layering (D-11); exposes prompt-version to cache keys
│   │   │                           #   (E3) + provenance; lifecycle draft→staged→verified→active→deprecated
│   │   ├── roles.py                # role catalog: extract · judge · chat · consolidate · summarize ·
│   │   │                           #   subcluster · query_rewrite · reflect · dedupe — each has a default
│   │   ├── loader.py               # loads defaults/ pack + merges user overrides (${prompt:name} refs);
│   │   │                           #   per-namespace overrides reuse the D-14 policy-override channel
│   │   ├── optimize.py             # ★RG hook only: langmem-style metaprompt/gradient self-tuning
│   │   │                           #   (no-op default)                          [extra: promptopt]
│   │   └── defaults/               # SHIPPED DEFAULT PACK — one YAML per prompt, human-diffable:
│   │       ├── extract.yaml        #   frontmatter: id, version, role, output_model, format; --- Jinja2 body
│   │       ├── judge.yaml · consolidate.yaml · summarize.yaml · subcluster.yaml
│   │       ├── query_rewrite.yaml · reflect.yaml · dedupe.yaml · chat.yaml
│   │       └── firewall_flag.yaml  # E1 instruction-shaped-content detection prompt
│   │
│   ├── protocols/rest/             # FastAPI factory wrapping ONE Engine (D-06)
│   └── observability/              # logging.py (structlog M11), metrics.py, exporters.py (Langfuse + OTel)
│
├── evals/                          # ── REPO ROOT (D-19) ──
│   ├── harness/ · probes/ · fixtures/ · configs/
│   └── assertions/                 # ★NEW D-35: deepeval LLM-output assertions (not shipped in wheel)
│
├── tests/{unit,integration,combinations}/
├── docs/{mkdocs.yml, adr/}         # ADR-001..007 + ★ADR-008 code-level adoptions (D-26…D-42) + ★ADR-009 prompt subsystem (D-43)
└── examples/
```

## 2. Extras matrix (pyproject) — v1.1

| Extra | Pulls in | Enables |
|---|---|---|
| *(core, no extra)* | pydantic, pydantic-settings, **sqlalchemy, alembic** (D-36), **orjson** (D-38), **xxhash, fastuuid** (D-37), structlog, typer, fastembed, **json-repair** | SQLite storage (SQLAlchemy Core + Alembic) + FTS5, sqlite_adjacency graph fallback, inline runner, fast serialization + hashing/IDs — a working brain, zero heavy deps |
| `lance` | lancedb | default vector store + Tantivy FTS |
| `graph` | **ladybugdb** (pinned fork) | associative memory at depth — **empty/reserved in v0.1 (D-26 amended by D-49: default = `sqlite_adjacency`; provider reserved until the fork publishes)** |
| `kuzu` | **kuzu** ★NEW | embedded-Cypher graph alternative (D-26) |
| `lmdb` | lmdb | hot cache |
| `ingest` | **markitdown, chonkie** ★NEW | multi-format doc ingest + chunking (D-29) |
| `structured` | **instructor** ★NEW | schema-validated LLM extract/judge output (D-31) |
| `ner` | **gliner2** ★NEW | local CPU entity extraction (D-28) |
| `community` | **graspologic** ★NEW | Leiden hierarchical community detection for graph consolidation (D-40) |
| `llmlocal` | **llama-cpp-python** ★NEW | in-process open-weight inference; Ollama/vLLM/LM Studio need no extra (OpenAI-compatible HTTP) (D-39) |
| `promptopt` | **langmem** (or trustcall) ★NEW | RG-only prompt self-optimization hook (D-43); off by default |
| `compress` | llmlingua | E5 assembly-time context compression |
| `rerank` | **flashrank** ★NEW | E8 cross-encoder rerank alternative (D-51/ADR-017); fastembed's `TextCrossEncoder` rides the core dep where the installed version ships it, so this extra exists only for the flashrank fallback (upgrade fastembed otherwise) |
| `static` | **model2vec** ★NEW | E4 static-embedding prefilter (D-54/ADR-020): cheap CPU static embeddings narrow candidates before the real embedder + float32 rescore; opt-in, default off |
| `dbos` | dbos | durable worker runner |
| `taskiq` | taskiq, taskiq-redis | broker runner (Valkey Streams) |
| `rest` | fastapi, uvicorn | REST protocol |
| `aws` | boto3 | Bedrock LLM/embeddings + Secrets Manager |
| `litellm` | **litellm** ★NEW | optional unified LLM gateway adapter (D-33) |
| `all` | everything above | kitchen sink / CI |

Notes: **zstandard** (D-32) and **datasketch** (D-27) are small pure-ish wheels → folded into **core**. **deepeval** (D-35) and **fakeredis** (D-41) live in `evals/`/dev deps, never in the wheel. **orjson/xxhash/fastuuid** (D-37/38) are tiny compiled wheels → core.
Convenience bundles: `memspine[local]` = lance+graph+lmdb+dbos+ingest+ner; `memspine[prod-aws]` = aws+taskiq+rest.

## 3. Memory dependency graph (drives C1(b) auto-enable)

```text
working ──(none)                 reflective ──needs──> episodic
episodic ──(none)                prospective ──needs──> semantic
semantic ──(none)                associative ──needs──> semantic
procedural ──(none)              shared ──needs──> (≥1 of semantic|episodic) + namespace grants
resource ──(none)                consolidation pipeline ──needs──> episodic + semantic (config-time)
```
`registry.py` owns this table; `config validate` walks it; auto-enables log at INFO.

## 4. Engine startup sequence

1. **Bootstrap secrets, then resolve config** (two-phase, D-22) with `${secret:name}` refs resolved, per-key source recorded.
2. **Validate combination**: dependency closure (C1b), reserved-key rejection, policy value bounds; **resolve the prompt pack** (defaults → template → user overrides, D-43) and verify every enabled role has an active prompt + output_model.
3. **Construct services** from manifests; unmet `required_services` → `MissingServiceError` naming the extra (D-10), unless `strict_services: false`.
4. **Open the write door**: event log in WAL mode; register projectors for enabled memories.
5. **Start runner** (inline default; dbos/taskiq per config); register `schedule.py` sleep cycle.
6. **Catch-up**: projectors replay from high-water marks.
7. `engine.describe()` returns the effective world.

## 5. Build-phase → file mapping (v1.1 — new libs marked ★)

| Phase | Lands |
|---|---|
| 0 Substrate | core/* (all), config/*, services/base + **storage/sqlite (SQLAlchemy Core + Alembic, D-36; async engine via aiosqlite, D-44)** + secrets, observability/logging, cli config commands, ADR-001..011 — incl. E1 columns + ★minhash/simhash + **xxhash fingerprint + fastuuid id + orjson payloads (D-37/38)**, ★MemOS provenance/lifecycle fields (`status`/`version`/`history`/`evolve_to`/`source`, D-42), **json-repair in core** (D-31), **★D-45 configurable event-log retention (full/rolling/ephemeral + zstd payload compression)** |
| 1 Working + retrieval | memories/working/*, services/llm (**local: Ollama/vLLM/LM Studio/OpenAI-compat + llama_cpp `[llmlocal]`, D-39**) + embedding+vector+cache defaults, policies/scoring+assembly (E2), workers/inline, E3 embedding cache, examples/01–02 |
| 2 Semantic | memories/semantic/*, policies/conflict+dedup **(★datasketch D-27)**, entity resolution **(★gliner2 D-28, `[ner]`)**, **★prompts/ subsystem: registry+loader+defaults pack, extract/judge/dedupe YAML prompts, config-layered overrides (D-43)**, E9 YAML/CoD format, **★instructor `[structured]` D-31** |
| 3 Episodic + lifecycle | memories/episodic/*, policies/consolidation+decay+compression **(★zstandard cold-tier D-32)**, workers/pipelines+dbos_runner+schedule, resource ingest **(★markitdown+chonkie `[ingest]` D-29)**, E3 extraction cache, E7 hook |
| 4 Governance + Firewall | M7 delete hooks, cli forget --verify, memories/resource/*, E1 full Memory Firewall, example 03 |
| 5 Procedural + reflective | memories/procedural/*, memories/reflective/*, E6 plan-skill subtype |
| 6 Associative | memories/associative/*, services/graph/sqlite_adjacency **(v0.1 default — D-26 amended by D-49; ladybug reserved)** + **★services/graph/kuzu `[kuzu]` alt**, **★associative/communities.py graspologic `[community]` (D-40)** + background reorganizer (D-42), E4 two-stage retrieval |
| 7 Prospective + shared + REST | memories/prospective+shared, protocols/rest, workers/taskiq_runner (**★per-scope Redis-Streams queues + priority labels, D-42**), E5 compress + E8 rerank/strategy-rerank opt-in (D-42), example 04 |

## 6. Combination test matrix (C6, v0.1 scope)

5 templates + 9 single-type minimal configs + 1 kitchen-sink = **15 boots**, each asserting clean startup, one write→read round-trip per enabled type, clean shutdown, `describe()` schema. Plus `config validate` golden tests (dep auto-enable, missing-service hard-fail, reserved-key rejection, `extends` cycles) and runner tests (dbos crash-resume, inline flush-on-exit, dead-letter severity). ★NEW: graph-adapter parity test (ladybug vs kuzu vs sqlite_adjacency same round-trip), dedup two-stage test (D-27), ingest smoke on docx/pdf/pptx (D-29).

## 7. Deliberate non-goals for v0.1

Per-namespace type enablement (key reserved) · third-party memory plugins (interfaces provisional) · WS/gRPC/MCP protocols (seats reserved) · Postgres/Weaviate/Neo4j/Valkey adapters beyond stubs · **file-native/Markdown profile (D-30 skip)** · litellm as a core dependency (D-33 — adapter only) · benchmark leaderboard claims (harness first; the (accuracy, tokens, latency) triplet rule).

---
---

# PART B — Enhancement & Optimization Program (E1–E9)

*(Unchanged from v1 — canonical copy. Each item: what → evidence → where it slots → tier. Tiers: **QW** quick win · **DF** differentiator · **RG** research-grade hooks only.)*

## E1. Memory-poisoning defense — "the Memory Firewall" 【DF — headline】
OWASP **ASI06 (Memory & Context Poisoning)** + LLM08. Attacks: MINJA (~98% injection, defenses ineffective), AgentPoison (<0.1% entries → >80% success), MemoryGraft (10 seeds → 48%), sleeper variants. No surveyed framework (Mem0/Letta/Zep/Cognee/MIRIX) ships poisoning defense. Ships as **M17** in `core/policies/trust.py` (Phase 4): trust scoring at write (source class × channel; retrieved content capped low), quarantine tier (excluded from consolidation/promotion until corroborated), write-path anomaly detection (embedding-outlier + MINJA heuristics), instruction-shaped-content flag (inert framing), blast-radius `audit taint`. Columns `trust`/`quarantined`/`instruction_flag` land in **Phase 0 DDL**.

## E2. Cache-friendly context assembly 【QW — highest ROI】
Provider prefix caching: relocating volatile working memory out of the prefix took cache hit 7%→84%, cut LLM cost 59%. `policies/assembly.py` (Phase 1): stability-sorted placement `persona → skills/rules → semantic facts → [cache boundary] → retrieved episodic + working + query`; LLM manifest carries `prompt_cache_ttl_tiers`; metrics track `cached_tokens`.

## E3. Semantic + operation caching layer 【QW】
Semantic cache hit 30–70% in prod; agents make 3–10× more calls. `services/cache/semantic.py`: embedding cache (content-hash, Phase 1), extraction/judge cache (semantic-keyed, Phase 3), retrieval micro-cache. Keys include embedder-id + prompt-version.

## E4. Embedding storage & speed 【QW→DF】
Binary/int8 quantization + float rescore (~32× storage cut, ~95% quality); Matryoshka truncation; static-embedding prefilter (model2vec). `Embedder` manifest gains `matryoshka_dims` + `quantization`; `vector/base.py` gains `search_rescore()`. **BUILT (D-54/ADR-020):** the zero-dep SQLite store implements the two-stage `search_rescore` in pure Python — int8/binary prefilter over an oversampled candidate window → exact float32 cosine rescore — plus Matryoshka prefix truncation. Manifest-driven with an explicit `vector.quantization` override; **default OFF** (the default embedders declare no capability), so `profile="simple"` is byte-identical. model2vec is the opt-in `[static]` prefilter. Migration 0009 adds the `memory_embeddings.quantized_vec`/`quantization` columns (nullable, NULL on the default path).

## E5. Assembly-time context compression 【DF】
LLMLingua-family up to ~20× on retrieved chunks. `CompressionPolicy.assembly_stage` ordered fallbacks: drop-lowest-score → LLMLingua block-compress `[compress]` → provider compaction. Never compress persona/disputed facts.

## E6. Plan & tool-value caching → procedural extension 【DF】
Agentic Plan Caching + TVCACHE. A `plan` skill subtype in M13.4: validated multi-step plans stored on task success, retrieved by task-embedding similarity, entering at `staged` — held out of retrieval via `RESOLVING` like all pre-active ladder stages (quarantine tier stays firewall-only, ADR-014). Phase 5.

## E7. Sleep-time compute 【RG — hook only】
Letta sleep-time agents. Fourth sleep-cycle stage slot (after consolidate→decay→compress): pre-compute reflections, pre-warm caches, pre-assemble bundles. No-op default, Phase 3.

## E8. Retrieval-quality stages 【DF, opt-in】
Cross-encoder rerank (fastembed ONNX rerankers / flashrank) + query rewriting (HyDE). `ReadPolicy` ordered optional stages: `[static_prefilter?] → hybrid RRF → [rerank?] → score → MMR → assemble`. Off by default; templates opt in (Phase 7). **Hybrid RRF is BUILT (D-25/ADR-017 §5): opt-in via `read.hybrid` (default off) — the SQLite FTS5/BM25 lexical leg is fused into the vector leg via `rrf_fuse`; default-on is the intended v0.2 flip.**

## E9. Token micro-optimizations 【QW】
YAML/TSV output ≈ half the tokens of JSON; Chain-of-Draft matches CoT at ~7.6% tokens. Template changes in `config/templates` + `prompt_registry` (Phase 2). ★ Pairs with D-31 instructor (structured output) for reliable + cheap extraction.

## Consolidated priority view

| Tier | Items | When |
|---|---|---|
| **Phase-0 schema** | E1 columns + ★minhash/simhash fields (D-27) | day one DDL |
| **QW** | E2 · E3 · E9 · ★json-repair (D-31) · ★datasketch dedup (D-27) · ★zstandard compress (D-32) | Phases 1–3 |
| **DF** | E1 firewall (P4) · ★gliner2 NER (D-28, P2) · ★kuzu graph alt (D-26, P6) · E6 (P5) · E4 (P6) · E5+E8 (P7) | OSS headline |
| **RG** | E7 anticipatory sleep · certified-defense integration | hooks reserved |

---
---

# PART C — Code-level adoption decisions (D-26…D-35)

*New in v1.1. Sourced from a 2026-07-07 dependency scan of all repos in `D:\mem` — full evidence in `DEPENDENCY_ANALYSIS.md` and `PACKAGE_CATALOG.md`. Each decision names the library, the module it lands in, the extra, and the ecosystem proof.*

| # | Decision | Library → module | Extra | Proven by | Tier |
|---|----------|------------------|-------|-----------|------|
| D-26 | Graph store: LadybugDB default, kuzu supported | `services/graph/ladybug.py` (default) · `services/graph/kuzu.py` (alt) | `graph` / `kuzu` | graphiti, cognee, mofsl-graphiti ship kuzu | DF |
| D-27 | Two-stage dedup | `datasketch` → `policies/dedup.py` (LSH) + cosine confirm | core | MemOS (`pref-mem`) | QW |
| D-28 | Local NER | `gliner2` → `memories/semantic/entities.py` (flag) | `ner` | graphiti (`gliner2` extra) | DF |
| D-29 | Multi-format ingest + chunk | `markitdown`+`chonkie` → `memories/resource/extraction.py` | `ingest` | MemOS (`mem-reader`) | QW |
| D-30 | File-native profile | **skipped** | — | (trend: ReMe, EverOS) | — |
| D-31 | Structured LLM output | `instructor`+`json-repair` → `services/llm/*` | `structured` / core | cognee, honcho, langmem | QW |
| D-32 | Cold-tier compression | `zstandard` → `policies/compression.py` | core | MemOS, ReMe, SimpleMem | QW |
| D-33 | LLM gateway | `litellm` → `services/llm/litellm_adapter.py` (optional) | `litellm` | A-mem, mem0, cognee, hindsight | — |
| D-34 | CJK lexical | `jieba/rjieba` → `services/lexical/cjk.py` (gated) | `cjk` | ReMe, powermem, EverMemOS, MemOS | — |
| D-35 | Eval assertions | `deepeval` → `evals/assertions/` | dev-only | cognee | 🧪 |

**Positioning update:** the code scan confirms memspine's core stack (fastembed/ONNX, LanceDB, structlog, typer, pydantic-settings, langfuse/OTel/Prometheus) is exactly what the field independently converges on — so the defaults are *validated, not speculative*. The DF additions (kuzu-alt graph, gliner2 NER, datasketch dedup, instructor output) each replace a hand-rolled or LLM-only step with a proven library, and none enlarge the zero-extra core beyond two small pure wheels (datasketch, zstandard) plus json-repair. **D-30 (file-native) is the one deliberate pass** — noted as a real 2026 trend but out of scope for v0.1.

---
---

# PART D — Second adoption pass: MemOS patterns + package rulings (D-36…D-42)

*New in v1.2. From a code-usage investigation and a MemOS deep-dive (2026-07-07). Evidence quoted with file paths.*

## D.1 — Five MemOS patterns to adopt (D-42)

MemOS (`D:\mem\MemOS`) is the richest engine in the set; these patterns are proven in its code and map cleanly onto memspine without importing MemOS itself.

1. **Typed-memory container ("MemCube").** `GeneralMemCube` (`mem_cube/general.py`) composes `text_mem / act_mem / para_mem / pref_mem` backends behind one factory, each nullable via an `"uninitialized"` sentinel, with per-type `load/dump`. → memspine `memories/` already is per-type; adopt the **serializable container + null-backend sentinel** so a namespace can enable any subset and round-trip to disk. (Fits D-14 combination granularity.)
2. **Rich provenance + versioned lifecycle.** `TextualMemoryItem` metadata (`textual/item.py`): `status ∈ {activated,resolving,archived,deleted}`, `version`, `history: list[ArchivedTextualMemory]`, `evolve_to`, `is_fast`, and `SourceMessage` (role/content/doc_path/message_id). → fold into `core/records.py` (M1): our bi-temporal record gains `status`, `version`, `history[]`, `evolve_to`, and a first-class `source` provenance object. This is what makes conflict-resolution (M4) and `audit taint` (E1) auditable.
3. **Redis-Streams scheduler with per-scope queue isolation.** `SchedulerRedisQueue` (`mem_scheduler/.../redis_queue.py`) uses consumer groups + `xautoclaim` recovery, keyed per `(user_id, mem_cube_id, task_label)`; `SchedulerDispatcher` is a priority thread-pool router; labels include `add/mem_read/mem_organize/mem_update/mem_archive`. → validates the **taskiq (Valkey-Streams) runner (D-16)**; adopt **per-scope stream keys + priority labels + claim-recovery** as the taskiq_runner design, and mirror the label set onto `workers/pipelines.py`.
4. **Background graph reorganizer (hierarchical consolidation).** `GraphStructureReorganizer` (`tree_text_memory/organize/reorganizer.py`) consumes op-prioritized `add/remove/merge/update` messages, does subclustering + `build_summary_parent_node`. → this is exactly the associative-memory **community → summary-parent** consolidation; implement it with **graspologic `hierarchical_leiden` (D-40)** for the clustering step instead of MemOS's LLM-only subcluster (cheaper, deterministic), with an optional LLM summary of each community.
5. **Hybrid + strategy reranking.** `Searcher` (`tree_text_memory/retrieve/searcher.py`) fuses graph-recall + `EnhancedBM25` (LRU corpus cache) + vector, then strategy rerankers (`concat_background`, `concat_docsource`). → memspine read pipeline (E8) already has RRF + rerank stages; adopt the **"concat node background/source before rerank"** strategy and the **LRU corpus cache** for the BM25 leg.

**Avoid from MemOS:** KV-cache **activation memory** (`activation/kv.py`, `DynamicCache`) — powerful but couples you to a self-hosted `transformers` model; out of scope unless we own inference. And **backend sprawl** (neo4j/nebula/polardb/postgres/milvus in parallel) — memspine keeps one graph + one vector default with stubbed swap-ins (M14).

## D.2 — Package rulings from the code-usage investigation

| Package | Where actually used (verified) | Ruling |
|---------|-------------------------------|--------|
| **datasketch** | MemOS `prefer_text_memory/utils.py` `deduplicate_preferences(num_perm=256, threshold=0.6)` MinHashLSH | **Adopt (D-27)** — stage-1 dedup; note MemOS's is greedy/order-dependent, so pair with cosine confirm |
| **xxhash** | MemoryBear `graphrag/utils.py` `xxhash.xxh64()` for content hashing | **Adopt (D-37)** — fingerprints, cache keys, blob addressing |
| **fastuuid** | transitive only (no direct import found) | **Adopt (D-37)** anyway — cheap hot-path ID gen; low risk |
| **graspologic** | MemoryBear `graphrag/general/leiden.py` `hierarchical_leiden`, `largest_connected_component` | **Adopt optional (D-40)** — community detection for consolidation |
| **igraph** | LightMem `semantic_graph/SemanticMemory.py` `import igraph as ig` | **Reject** — networkx is the pure-Python default; graspologic covers clustering |
| **dydantic** | not directly imported (transitive of langmem/trustcall in SimpleMem) | **Defer** — dynamic pydantic models; revisit for D-15 plugin custom-type schemas |
| **soundfile / librosa** | SimpleMem `OmniSimpleMem/.../audio_processor.py`, `audio_trigger.py` (`librosa.load`) | **Defer** — audio-only; future `[audio]` multimodal extra, not v0.1 |
| **nltk** | broad (tokenize/stopwords across A-mem/MemOS/etc.) | **Reject in core** — heavy corpora downloads; Tantivy/FTS5 tokenizers suffice |
| **rake-nltk** | MemOS (declared; RAKE keyword extraction) | **Reject** — tags come from NER (gliner2)/LLM |
| **cn2an / xpinyin / hanziconv** | MemoryBear `rag/nlp/__init__.py` (CJK numeral/pinyin normalization) | **Reject** — part of the dropped CJK track (D-34) |
| **mmh3** | telemem (transitive of chromadb) | **Reject** — xxhash covers hashing (D-37) |
| **jieba / rjieba** | ReMe/powermem/EverMemOS/MemOS CJK tokenization | **Drop (D-34 reversed)** — CJK not a v0.1 goal |
| **sqlalchemy + alembic** | 15 / 8 repos respectively (near-universal) | **Adopt (D-36)** — Core + Alembic for the storage layer |
| **orjson** | 5 repos (hot-path JSON) | **Adopt (D-38)** — with pydantic, not instead of it |
| **fakeredis** | cognee (test double) | **Adopt (D-41)** — mock Valkey/Redis in tests |
| **llama-cpp-python / vllm / ollama** | cognee / LightMem / A-mem+MemOS+powermem | **Adopt (D-39)** — first-class local open-weight hosting |

---
---

# PART E — Customizable prompt subsystem (D-43)

*New in v1.2. All internal LLM calls (extract, judge, consolidate, summarize, subcluster, query-rewrite, reflect, dedupe, firewall-flag) run off named, versioned, **user-overridable** prompts. Design synthesized from a survey of how the strongest repos structure prompts.*

## E.1 — What the field does (evidence)

| Repo | Structure | Customization | Takeaway |
|------|-----------|---------------|----------|
| **graphiti** | `prompts/` package, one typed module per task; each prompt **paired with a pydantic output model**; `lib.py` registry | code-level (subclass) | ★ typed prompt + output-model pairing → perfect with instructor (D-31) |
| **cognee** | `infrastructure/llm/prompts/*.txt` loaded by name (system/user split) | ★ **external files** — edit without code | ★ prompts as data, not code → user-diffable |
| **powermem** | `prompts/` split by domain + `__init__.py` registry + **`custom_prompts_usage.md`** | ★ **documented override API** | ★ explicit, supported customization is a feature |
| **mem0** | single `configs/prompts.py` constants | pass a custom string to config | simplest; hard to version |
| **MemOS** | `templates/*_prompts.py` per subsystem (reader/search/scheduler/feedback) | code-level | grouping by role/subsystem scales |
| **hindsight** | prompts co-located with each engine stage (`consolidation/prompts.py`) | code-level | keep prompt near its consumer |
| **langmem** | `prompts/` with gradient/metaprompt **self-optimization** | automatic tuning | RG-tier; hook only for us |

## E.2 — The chosen structure (best of the above)

**A dedicated `src/memspine/prompts/` package** (not buried under procedural), combining graphiti's typing, cognee/powermem's external-file customization, and memspine's own config layering:

1. **Prompts are data, shipped as a YAML default pack** (`prompts/defaults/<role>.yaml`). Each file: frontmatter (`id`, `version`, `role`, `output_model`, `format`) + a `---` Jinja2 body. Human-readable, diffable, semver-tracked with the package.
2. **`PromptRegistry` keyed `(role, name, version)`** resolves the *active* prompt through the **existing config-layering chain (D-11)**: `defaults pack → template (D-12) → user YAML → env → runtime kwargs`. So customizing a prompt is exactly like customizing any other config — no new mechanism. `${prompt:name}` references work in config; per-namespace overrides reuse the D-14 policy-override channel.
3. **Each prompt is paired with an optional pydantic `output_model`** → passed straight to **instructor (D-31)** for validated structured output; `format` picks json/yaml/CoD token profile (E9).
4. **Versioned lifecycle**: prompt versions enter the procedural ladder at `active` (a resolved prompt IS the active version by construction; the full draft→…→active prompt lifecycle is deferred until a prompt promotion actor exists — ADR-014); `procedural/prompt_registry.py` becomes a thin store of prompt-as-memory versions referencing IDs in `prompts/registry.py` (definition lives once, in `prompts/`). Prompt-version is part of the E3 cache key so upgrades invalidate cleanly, and part of provenance so `audit taint` (E1) can trace which prompt produced a memory.
5. **CLI parity with config**: `memspine prompts list|show|diff|resolve|set|rollback` — `resolve` prints the merged prompt with `# source:` per layer, just like `config resolve`.
6. **Self-optimization is an RG hook** (`optimize.py`, `[promptopt]`): langmem-style metaprompt tuning, no-op by default (fits the EvolveMem/E7 research tier).

## E.3 — Why this is the right call

- **One customization model.** Users already learn config layering + templates; prompts reuse it verbatim — nothing new to learn, and `regulated_financial` / `voice` templates can ship their own prompt overrides the same way they ship policy overrides.
- **Safe by construction.** External YAML + versioning + provenance means a prompt change is auditable and rollbackable (matters for the regulated profile and the Memory Firewall E1).
- **Cheap + reliable extraction.** Output-model pairing (instructor) + format=CoD/YAML (E9) makes every internal call typed and token-lean.
- **No lock-in.** Prompts are plain files + a registry; swapping a model (D-39 local/open-weight) or a provider doesn't touch prompt definitions.

**Non-goal (v0.1):** automatic prompt optimization ships as a hook only (D-43 RG); a prompt *marketplace* / remote prompt hub is out of scope (local files + config layering only).

*Companion docs:* `UNIMEM_V2_REWORK_PROPOSAL.md` (architecture rationale), `DEPENDENCY_ANALYSIS.md` (adoption reasoning), `PACKAGE_CATALOG.md` (every package, does-what).
