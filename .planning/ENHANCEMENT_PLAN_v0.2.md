# memspine v0.2 Enhancement Plan (research-backed)

Synthesized from four parallel investigations (2026-07-10): codebase current-state
audit, cashews cache evaluation, prompt-library comparison, and rerank/multi-call/
llmlingua research. Grounded in the shipped code + the D-register/ADRs.

**Framing:** most of the requested list is *already shipped* — the plan separates
**confirm/flip** (small) from **genuine builds** (the real work), and records the
library decisions with their disadvantages so they don't get re-litigated.

---

## Cross-cutting decisions (locked by research)

| # | Decision | Verdict | Why (short) |
|---|----------|---------|-------------|
| DEC-1 | **Cache = cashews?** | **ADOPT (user override, 2026-07-10) — cashews for disk + redis/valkey; DROP LMDB; `MemoryKV` stays the zero-dep core default** | User decision overrides the research REJECT. cashews (behind a new `[cache]` extra) becomes the unified adapter for on-disk (diskcache) + redis/valkey; `LmdbCache`, `clients/lmdb.py`, the `[lmdb]` extra, and the hand-rolled `RedisCache`/`clients/redis.py` are removed (cashews owns redis now). **Slim-core (D-03) retained:** `MemoryKV` stays the `memory` core default — cashews must not import into core. **Accepted trade-offs (recorded):** on-disk becomes diskcache not LMDB (reverses D-09); ~90% of cashews' surface (locks/bloom/circuit-breakers) unused for now. Amends ADR-022 + D-09. *(If cashews should also replace `MemoryKV` as the in-mem default, that needs a separate D-03/ADR-002 amendment.)* |
| DEC-2 | **Prompt library?** | **REJECT all — keep & enhance D-43 (Jinja2+YAML)** | Every option fails a hard constraint (Mirascope/BAML/DSPy/guidance break "prompts are data"/slim-core; LangChain hub breaks offline-first; banks/Prompt Poet duplicate what exists with a second, unaligned versioning model). D-43 already leads on versioned provenance + config-layer overrides + pydantic output-model pairing. |
| DEC-3 | **RerankerFactory** | **Build a registry-based factory; ONE new `[st]` torch adapter; cloud via existing litellm** | Registry keyed by provider id, lazy-load, graceful-degrade matching the current stage. Cloud rerankers (Cohere/Voyage/Jina/Bedrock) are a config string through the existing litellm adapter — NOT new per-vendor code. |
| DEC-4 | **DBOS+SQLite default** | **Flip the default in server-profile TEMPLATES, NOT the schema default** | A schema default of `runner="dbos"` makes a bare `pip install memspine` fail at start (`[dbos]` extra) — breaks slim-core (D-03) + profiles-stay-green. Template change = lower blast radius. |
| DEC-5 | **Community detection** | **graspologic `hierarchical_leiden` — CONFIRMED (already shipped, D-40)** | Already implemented: `memories/associative/communities.py` (`hierarchical_leiden`, lazy import), wired into the `reorganize` sleep-cycle pipeline (community-summary parents + `community` LINK events), behind `[community]`. Nothing to build; the plan only exposes its knobs as config (A6). **Caveat:** graspologic pins `numpy<2` and conflicts with `[ingest]` (magika `numpy>=2.1`) — declared in `[tool.uv] conflicts`, so `[community]` is excluded from the `[all]` bundle. |

**Real bug found (do first):** `engine.py:~360` validates `read.rerank` against a
three-value tuple that **excludes `litellm`** — so `read.rerank: litellm` (which the
schema and `_rerank_provider` both support, ADR-024) **raises `ConfigError` at engine
start today.** The RerankerFactory's registry becomes the single source for both
validation and construction, closing this drift.

---

## Item status (from the audit) — what's real work vs already shipped

**Truly missing / real build:**
1. Autonomous decay/forgetting **scheduler** — mechanics (tiers, `decay_sweep`, `sleep()`) exist; nothing runs them on a timer (`sleep()` is manual). *Highest-value gap.*
2. **`group_id` + `tags`** on `MemoryRecord` — fields don't exist.
3. **write-from-messages/episode** verb — no chat-transcript path.
4. **Configurable graph traversal** — only PPR wired; BFS primitive `walk_neighbors` exists **unused**; no RRF blend, no strategy config.
5. **Additive salience on read** — only `access_count`/`last_accessed_at` advance; no `utility += 0.1`.
6. **Multi-call/graphiti writes** — single-pass extractor; no LLM edge-extraction call, no reflexion in the write path, no scenario-conditioned prompts.
7. **Prompt gaps** — no Jinja partials/loader; registry keyed by `id` only (no per-`memory_type`/scenario variant).

**Already shipped — confirm / config-flip only:**
- Hybrid vector+FTS+RRF ✅ (opt-in; v0.2 default-on flip, D-25/D-53) · Communities/`reorganize` (graspologic Leiden) ✅ wired in the sleep cycle · zstd cold + **E5 llmlingua** compression ✅ wired · DBOS+SQLite runner ✅ built (template default-flip) · Jinja2 named prompts ✅ (keyed by **role**, 10 roles).

---

## Phased plan

Sequenced low-risk/high-value → bigger builds. Each phase stays green + committed
(no co-author trailers), one ADR per register change.

### Phase A — Quick wins & fixes  *(low risk, mostly config)*
- **A0. Cache: adopt cashews, drop LMDB** — new `services/cache/cashews_cache.py` (`CashewsCache` wrapping a cashews `Cache` by backend URL: `disk://…`, `redis://…`/valkey) behind a new `[cache]` extra (`cashews[redis,diskcache]`); **delete** `lmdb_cache.py`, `clients/lmdb.py`, the `[lmdb]` extra, and the hand-rolled `RedisCache`/`clients/redis.py`. `cache.backend` values → `memory` (MemoryKV, core, unchanged) | `disk` | `redis` | `valkey`. Keep the `KVCache` port + `namespace` prefix + TTL semantics; `RedisClient` eager-ping D-10 posture becomes cashews `setup()`+ping. Update tests (drop lmdb/redis adapter tests, add cashews tests + fakeredis path). *ADR amends ADR-022 (+ D-09).*
- **A2. RerankerFactory** — `services/rerank/factory.py`: registry `{fastembed, flashrank, litellm}` (+ `sentence_transformers` behind `[st]`), lazy thunks, `RERANK_MODES` as the one source for schema+startup validation; move the construct-lazily + swallow-to-None into the factory; engine keeps caching + sticky-disable + per-call guard. Optional `register_reranker()` seam. *ADR amends D-51.*
- **A3. Hybrid default-on flip** — `ReadConfig.hybrid: False→True`; always build the lexical store; verify the C6 combination-matrix "simple" boot; update ADR-019 status. *No new ADR (D-25 intent).*
- **A4. DBOS+SQLite default via templates** — flip `runner` to `dbos` in server-profile `config/templates/*.yaml` (NOT the schema default). *ADR note amends D-16 posture.*
- **A5. Reinforcement on read** — additive salience in `RecordProjector._apply_retrieve` (`utility`/`importance += RETRIEVE_UTILITY_STEP`, clamped); new constant; fits event-sourcing (rides the RETRIEVE event). *No ADR (validate the 0.1 step against decay recency first).*
- **A6. Expose community + compression config** — surface the graspologic **Leiden** knobs currently hard-coded as constants (`REORGANIZE_MIN_COMMUNITY_SIZE`, plus Leiden `resolution` / `randomness` / `random_seed`) as `memories.associative.policies` keys, and compression `compress_tiers`/rate. Community detection engine stays graspologic `hierarchical_leiden` (DEC-5). *No ADR.*

### Phase B — Prompt subsystem overhaul  *(D-43 amendment)*
- **B1. Jinja loader + partials** — replace `from_string` env with a loader over `defaults/_partials/`; fold repeated boilerplate (YAML-only footer, anti-injection safety block, format instructions) into fragments; **fingerprint included partials into `prompt_version`** so E3 cache + E1 provenance still fire on fragment change; `prompts.partials.<name>` override layer.
- **B2. Scenario/conditional selection** — generalize the registry key from `id` to `(role, selector)`; variants as data (`<role>@<scenario>` YAML with a `when:` block); `registry.select(role, memory_type=…, condition=…)` picks most-specific, **falls back to `id==role`** (keeps every caller + `profile="simple"` green); config-drivable via `prompts.selection`. Migrate `engine.py` call sites from `for_role` to `select`.
- **B3. Enhance all 10 prompts** — consistent anti-injection framing (E1), every structured prompt has `output_model` + token-lean `format` (yaml/cod, E9), set `token_budget`; split double-duty prompts into scenario variants (judge escalation cheap/arbiter; extract short-text/document; summarize length tiers).
- **B4. Prompt test harness** — golden/snapshot renders per prompt+variant; output-model round-trip through instructor+json-repair (offline); selector-coverage + fallback assertions; partial-fingerprint version-bump assertion; lint (no inline literal that should be a partial; every `StrictUndefined` var supplied). *ADR amends D-43 (adds keying dimension + partials to the prompt contract).*

### Phase C — Multi-call / graphiti-style writes  *(biggest build)*
- **C1. New roles + output models** — `extract_edges` → `ExtractedEdges` (`{src_entity, rel, dst_entity, fact, valid_from?, confidence}`), `resolve_entity` (extend `dedupe`), `invalidate_edge` (reuse `judge`/`ConflictVerdictOut` — its `add|update|invalidate|noop` already matches graphiti's edge verbs); `reflect` (exists) called as a second extraction round, max-rounds config.
- **C2. Async `extract_graph` pipeline** (primary path) — new idempotent step in `workers/pipelines.py` (plain step fn, D-17): read recent episodic/semantic records → LLM edge extraction + reflexion → emit `LINK` (new non-reserved rel e.g. `asserted`, so `links.py` budget/prune rules still apply) + `WRITE` via `ctx.append_event`; register in `PIPELINES`, slot **after `consolidate`, before `reorganize`** so communities form over LLM edges. Opt-in.
- **C3. Optional sync `WritePipeline`** — composable steps (`extract_entities→resolve_entities→extract_edges→invalidate_edges`) in `SemanticMemory._write_locked` before the conflict ladder, behind `memories.semantic.policies.write_pipeline: single|graph`; keeps `profile="simple"` byte-identical. E1 provenance inheritance (`trust=min(member)`, `instruction_flag`). *ADR — multi-call write pipeline.*
- **C4. `write_messages()` / `write_episode()` verb** — chat transcript (`[{role, content}]`) → per-turn episodic records via `_write_locked` (`memory_type="episodic"`, `SourceInfo(role=turn.role)`), optional `detect_sessions`. Additive, fits contracts. *No ADR.*

### Phase D — Lifecycle & record model
- **D1. Autonomous forgetting/decay scheduler** — `workers/scheduler.py` background asyncio interval loop calling `run_sleep_cycle`; `workers.sleep_interval_seconds` config; engine start/stop wiring; (optional) decay→`FORGET` auto-archival rule for dormant records. *ADR amends D-16 (always-on interval is a new execution concern) + M7 governance if auto-forget.* Also tighten the CLAUDE.md/plan wording that implies an active scheduler today.
- **D2. `group_id` + `tags` on the record** — new `MemoryRecord` fields (`group_id: str|None`, `tags: list[str]`), an Alembic migration (**clean now — single 0001 baseline after the Phase-17 squash**), thread through `Engine.write()`/`write_messages()` + REST body + `storage.list_records`/vector/lexical filters + the retrieval gate; orthogonal sub-scoping facet over the existing namespace isolation. *ADR — universal-record contract change (D-21 conservative-schema).*

### Phase E — Retrieval depth
- **E1. Configurable graph traversal** — `strategy` selector (`ppr|bfs|rrf`) on `AssociativeMemory.related` reading `memories.associative.policies` (no schema change — uses the per-type policy dict); wire the unused `walk_neighbors` (BFS) + an RRF blend of graph-rank + vector-rank; optionally expose a graph leg fused into `engine.search` via `rrf_fuse`. *ADR amends D-49 (`related` is PPR-by-contract today).*

### Phase F — Compression depth  *(additive, `[compress]`)*
- **F1. llmlingua-2 + configurable model** — make `_load_llmlingua()` config-driven (`llmlingua_model` default `llmlingua-2-xlm-roberta-large-meetingbank`, `use_llmlingua2=True`, `device_map="cpu"` — torch stays in `[compress]`, never core).
- **F2. Per-assembly-stage token budgets** — `stage_budgets: dict[str,int]` keyed by the existing `_PLACEMENT_RANK` bands; compress within each band (squeeze volatile episodic/working hard, protect the cacheable persona/procedural/semantic prefix → **protects E2 prefix caching**).
- **F3. Rate/target-token per stage + preserve** — `compress_rate`/`target_token` per band; a `preserve` list (entity names/numbers via llmlingua `force_tokens`) so compression can't mangle the `(entity, attribute)` keys retrieval depends on. Sane single-value default = today's behavior.
- **F4. Decision rule + safety** — document zstd-cold (at-rest, reversible) vs llmlingua (in-context, lossy) as orthogonal; assert `fit_assembly` operates on **inflated** content (a zstd'd record has `content==""`); emit a `compression.block_compressed` M11 observability event.

---

## Dependencies / sequencing

- **A1 first** (real bug). A2 (factory) enables clean A3-independent rerank work.
- **B before C** — C's scenario-conditioned prompts *depend on* B2's `(role, selector)` registry.
- **D2 (migration)** is cheap now because Phase-17 left a single clean `0001` baseline to extend.
- **C is the heaviest** — gate it behind opt-in policy; it must not perturb `profile="simple"`.
- **E, F** are independent and can land any time after A.

## New ADRs (register deltas)
- Reranker factory (amends D-51) · Prompt subsystem partials + scenario keying (amends D-43/ADR-009) · Multi-call write pipeline (new) · Forgetting scheduler (amends D-16) · Record `group_id`/`tags` (amends D-21) · Graph traversal strategies (amends D-49). Cache (DEC-1) and prompt-library (DEC-2) rejections recorded as anti-decisions; DBOS template default (DEC-4) as a D-16 note.

## Explicitly rejected (with disadvantages captured)
Mirascope/BAML/DSPy/guidance/LangChain-hub/banks/Prompt-Poet/priompt/Promptify as prompt libs (DEC-2) · per-vendor reranker SDKs and the answer.ai `rerankers` wrapper (covered by litellm + one `[st]` adapter) · ColBERT/late-interaction reranking (deferred — doesn't fit the single-score port).

---

## Master to-do — all 22 tasks (one table for verify/track)

**Type:** 🐛 bug · 🔧 config/flip · 🏗 build · 📦 dep-swap  **Risk:** 🟢 low 🟡 med 🔴 high  **Status:** ☐ todo · ◐ in-progress · ☑ done

| ID | Phase | Task | Type | ADR delta | Depends | Risk | Status |
|----|-------|------|------|-----------|---------|------|--------|
| A0 | A · Quick wins | Adopt **cashews**, drop **LMDB** + hand-rolled Redis; `cache.backend = memory\|disk\|redis\|valkey`; MemoryKV stays core | 📦 | amends ADR-022, D-09 | — | 🟡 | ☑ |
| A1 | A · Quick wins | Fix `read.rerank=litellm` **startup-validator bug** (ConfigError today) | 🐛 | — | — | 🟢 | ☑ |
| A2 | A · Quick wins | **RerankerFactory** (registry keyed by id, lazy, graceful-degrade; single validation source) | 🏗 | amends D-51 | A1 | 🟢 | ☑ |
| A3 | A · Quick wins | Hybrid retrieval **default-on** flip (`read.hybrid → true`); verify C6 matrix | 🔧 | D-25 intent, ADR-019 amend | — | 🟡 | ☑ |
| A4 | A · Quick wins | **DBOS+SQLite default** via server-profile templates (not schema default) | 🔧 | D-16 note (ADR-005) | — | 🟢 | ☑ |
| A5 | A · Quick wins | **Reinforcement on read** — additive salience (`utility += step`) in `_apply_retrieve` | 🏗 | — | — | 🟢 | ☑ |
| A6 | A · Quick wins | Expose **graspologic Leiden** knobs (`min_size`/`resolution`/`randomness`/`seed`) + compression `compress_tiers`/rate as config | 🔧 | — | — | 🟢 | ☑ |
| B1 | B · Prompts | Jinja **loader + partials** (`_partials/`); fingerprint partials into `prompt_version` | 🏗 | amends D-43 (ADR-009) | — | 🟡 | ☑ |
| B2 | B · Prompts | **Scenario/conditional selection** — `(role, selector)` key, `<role>@<scenario>` variants, `select()` + role fallback | 🏗 | amends D-43 | — | 🟡 | ☐ |
| B3 | B · Prompts | **Enhance all 10 prompts** — anti-injection framing, `output_model`+lean `format`, split double-duty into variants | 🏗 | — | B1,B2 | 🟢 | ☐ |
| B4 | B · Prompts | **Prompt test harness** — golden renders, output-model round-trip, selector coverage, lint | 🏗 | amends D-43 | B1,B2 | 🟢 | ☐ |
| C1 | C · Multi-call | New roles + output models: `extract_edges`, `resolve_entity`, `invalidate_edge`, reflexion round | 🏗 | — | B2 | 🟡 | ☐ |
| C2 | C · Multi-call | Async **`extract_graph` pipeline** → `LINK`/`WRITE` events (after `consolidate`, before `reorganize`), opt-in | 🏗 | new ADR | C1 | 🔴 | ☐ |
| C3 | C · Multi-call | Optional **sync `WritePipeline`** in semantic door + E1 provenance inheritance | 🏗 | new ADR | C1 | 🔴 | ☐ |
| C4 | C · Multi-call | **`write_messages()`/`write_episode()`** verb (chat transcript → episodic records + session detect) | 🏗 | — | — | 🟡 | ☐ |
| D1 | D · Lifecycle | **Autonomous forgetting/decay scheduler** — `workers/scheduler.py` interval loop → `run_sleep_cycle` | 🏗 | amends D-16 (+M7) | — | 🟡 | ☐ |
| D2 | D · Lifecycle | **`group_id` + `tags`** on `MemoryRecord` — fields + migration + write/REST/filter threading | 🏗 | amends D-21 | — | 🟡 | ☐ |
| E1 | E · Retrieval | **Configurable graph traversal** — `strategy: ppr\|bfs\|rrf`; wire unused `walk_neighbors` + RRF blend | 🏗 | amends D-49 | — | 🟡 | ☐ |
| F1 | F · Compression | **llmlingua-2** + configurable model (`_load_llmlingua`) | 🔧 | — | — | 🟢 | ☐ |
| F2 | F · Compression | **Per-stage token budgets** (`stage_budgets` by placement band; protect cacheable prefix) | 🏗 | — | F1 | 🟡 | ☐ |
| F3 | F · Compression | Per-stage rate/target-token + **entity `preserve`** list | 🏗 | — | F2 | 🟢 | ☐ |
| F4 | F · Compression | zstd-vs-llmlingua **decision rule** + inflate-before-compress assert + observability event | 🏗 | — | F1 | 🟢 | ☐ |

**Totals:** 22 tasks · 🐛1 🔧6 🏗14 📦1 · risk 🟢12 🟡8 🔴2 · ~6 ADR deltas (ADR-022, D-43, multi-call, D-16, D-21, D-49, D-51).

**Sequencing:** A first (A0/A1 are the openers) → B before C (C1 needs B2) → D/E/F independent after A. C is the heaviest (🔴, opt-in — must not perturb `profile="simple"`).

*Plan version: v0.2 · updated 2026-07-10 (cashews adopted DEC-1, graspologic Leiden confirmed DEC-5).*
