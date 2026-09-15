# memspine v0.3 — Orchestration Facade (`add`/`read`) · Living Decision Log

**Status:** 🟡 DISCUSSION / planning — **PLANNING LOCK ONLY, do NOT execute.**
No code changes until the full v0.3 design is approved and a build phase opens.
**Started:** 2026-07-11 · **Owner discussion:** ongoing.
**Goal:** Link memspine's existing primitives into an ergonomic, auto-wired
**conversation-memory** layer — `add(messages)` / `read(query)` — without touching
the event-sourced core or breaking `profile="simple"`.

> This file is the running record of decisions + open forks for v0.3. Update it
> as we settle each step; promote accepted decisions into ADRs + the structure
> plan when we start building.

---

## 1. Problem statement

memspine exposes **42 low-level verbs**. All the machinery exists
(firewall → dedup → M4 conflict → project → sleep), but there is **no conversation
verb** (`add`) and **no unified recall** (`read`). Callers must chop transcripts
into records, pick a `memory_type`, key facts by hand, and stitch
`search`+`assemble` themselves. Every surveyed peer instead leads with a single
`add(messages)` / `search(query)` pair. **The gap is an orchestration facade, not
missing capability.**

## 2. Architecture stance (accepted)

- **Closest architectural analog = MemMachine** (SQL episode-log write-anchor →
  typed episodic/semantic → **background** LLM ingestion; typed
  `producer_role`/`produced_for`). Second = **MemOS** (pluggable cubes +
  reorganizer + scheduler). Evidence: similarity scorecard, this session.
- **Borrow the *wiring* from MemMachine/MemOS** (cheap governed write, reconcile
  in the background) and the ***ergonomics* from mem0** (2-call surface). Do **not**
  copy mem0's synchronous, vector-primary wiring — it fights our event-sourced grain.
- Event-sourced core, firewall, and all policies stay **unchanged**. Deliver via a
  new **`agent` profile** — *profiles over knobs*. `profile="simple"` stays
  byte-identical (golden rule).

## 3. Proposed verb surface

Each message: `{"role": "user"|"assistant"|"system", "content": ..., "message_id"?: ...}`
— **`role` is required per message** (drives DEC-5 routing). Core also recognizes
`operator`/`tool` (trust policy); `system` = high-trust persona/instructions.

```python
# WRITE — conversation verb
await engine.add(
    messages, namespace="user/42", *,   # messages carry per-msg role (DEC-5)
    session_id=None,         # the conversation
    thread_id=None,          # the exchange (links the turns); auto if omitted
    meta=None,               # flattened, filterable (absorbs tags)
)

# READ — recall verb = search + assemble fused; returns CONTEXT, not an answer
ctx = await engine.read(
    query, namespace="user/42", *,
    session_id=None,         # filter: conversation
    thread_id=None,          # filter: exchange
    message_id=None,         # filter: one message
    role="all",              # filter: "user" | "assistant" | "system" | "all" (default)
    meta=None,               # filter: all-match metadata
    budget_tokens=1500,
)
# ctx.records + ctx.text/boundary_index (prompt-ready, cacheable prefix)
```
Sync wrappers: `add_sync` / `read_sync`.

## 4. Decisions register (DEC-v0.3-N)

| ID | Status | Decision |
|----|--------|----------|
| **DEC-1** | ✅ accepted | Add `add(messages)` conversation verb + `read(query)` recall verb. `read` fuses `search`→`assemble`. Additive; the 42 primitives remain. |
| **DEC-2** | ✅ accepted | Wiring modeled on **MemMachine** (closest arch analog); ergonomics on **mem0**. |
| **DEC-3** | ✅ accepted | `read()` returns **context, not an answer** (engine-not-product; matches MemMachine). No LLM answer-synthesis verb. |
| **DEC-4** | ✅ accepted *(this session)* | **Finalised identifier hierarchy** (see §4b): `namespace` ⊃ `session_id` ⊃ `thread_id` ⊃ `message_id`, + flat `meta`. Each maps to an **existing** core field → **facade-naming, not a schema change**. `meta` is flattened + filterable and **absorbs `tags`**. |
| **DEC-4b** | ✅ accepted *(this session)* | **`session_id` = conversation; `thread_id` = exchange (flexible, 2…N msgs); `message_id` = message (auto).** **Complete core rename `group_id` → `thread_id`** (NOT an alias); `session_id`/`message_id` reuse `SourceInfo`. **Greenfield: delete the entire existing migration set and recreate a single `0001` baseline from `schema.py`** — no migration path, no back-compat (pre-alpha). Supersedes ADR-027's `group_id` naming. No generic `id` — `thread_id` **is** the interlink. Resolves FORK-META (`meta` absorbs `tags`). **Execution DEFERRED — planning lock only.** |
| **DEC-5** | ✅ accepted | Typed roles: **`producer_role` (+ optional `produced_for`)** on ingest (MemMachine); **request** values `user`/`assistant`/`system` (`operator` is engine-internal per DEC-12; `tool` also core). **Role-differential handling:** `user` turns → durable/keyed facts; `assistant` turns → facts only if the **linked** (shared-`thread_id`) user turn corroborates them; **`system` turns → persona/instructions (high trust), NOT episodic fact-mined** (not silently dropped — routed to persona/working). |
| **DEC-6** | ✅ accepted | Ship as a new **`agent` profile** (add/read on, role-aware ingest, extraction-in-sleep, scheduler on, rerank on). `simple` byte-identical. |
| **DEC-7** | ✅ accepted *(this session)* | **Ingestion request model — support BOTH:** (1) *single request* carrying user + assistant turns together (one `thread_id`); (2) *separate requests* for user then assistant, **linked by the same `thread_id`**. Roles distinguish turns; order preserved within `session_id`. |
| **DEC-8** | ✅ accepted *(this session)* | **One write, one read — collapse the surface.** `add()` is the **only** public write verb; `read()` the **only** public read verb. **Remove `ingest()` and `reflect()`**: `reflect` becomes **background-only** (sleep cycle), never a caller verb; **`resource`/document intake is DEFERRED out of v0.3** — `add()` is **conversation-only** (turns), not document ingest. Cuts API sprawl/complexity. Greenfield — removing verbs is fine. *(Open: whether the specialized verbs — `add_skill`/`watch`/`associate`/`grant` — also fold in or stay; see FORK-SURFACE.)* |
| **DEC-9** | ✅ accepted *(this session)* | **Firewall is fully configurable.** Global enable/disable **and** per-signal toggles + thresholds: trust-matrix, instruction-regex, AgentPoison/MINJA anomaly, quarantine, corroboration. A deployment can run it strict, relaxed, or off. |
| **DEC-10** | ✅ accepted *(this session)* | **Firewall runs in PARALLEL (non-blocking).** Its I/O cost (embed + vector query + recent-record scan) must not block the write. `add()` anchors the event fast; the firewall assesses **concurrently**. **Safe-parallel:** a record is **not retrievable until the firewall clears it** (status `PENDING_FIREWALL` → `ACTIVATED` \| `QUARANTINED`), so no poisoning window. Latency is hidden, security preserved. |
| **DEC-11** | ✅ accepted *(this session)* | **Firewall on BOTH `add` and `read`.** In addition to the write-side gate, a **deterministic read-side trust/verification gate** (the ecosystem gap — MemOS does this but LLM-based; ours stays deterministic). Each side carries the **same configurable signal set** (trust-matrix + instruction-regex + AgentPoison/MINJA anomaly + quarantine + corroboration). |
| **DEC-12** | ✅ accepted *(this session)* | **Split `operator` vs `system` roles.** `operator` = **engine-internal** authored writes (consolidation summaries, grants, prompt-sync) — highest trust, engine-owned, never from a request. `system` = the **`system`-role messages in a request** (caller's system prompt/instructions). Request roles = `user`/`assistant`/`system`; `operator` is reserved for the engine. Update the trust matrix accordingly. |
| **DEC-13** | ✅ accepted *(this session)* | **Graph service duality simplified.** **local = ladybug** (`[graph]`), **server = Neo4j**, **kuzu = alt dependency** (`[kuzu]`). **Remove `sqlite_adjacency`** (zero-dep default). ⚠️ *Implication:* graph is **no longer zero-dep core** — associative memory now requires the `[graph]` (ladybug) extra; amends D-26/D-49 and the slim-core posture for graph. Supersedes FORK-GRAPH. |
| **DEC-14** | ✅ accepted *(this session)* | **Workers duality simplified.** **local = DBOS + SQLite** system DB, **server = DBOS + PostgreSQL**. `inline` stays the zero-extra default; **remove `taskiq`** (brokered runner) entirely. Amends D-16/ADR-005. |
| **DEC-15** | ✅ accepted *(this session)* | **M4 → "Chronicle."** The deterministic bitemporal fact-reconciliation ladder is named **Chronicle** (keeps every version of a fact + its validity window; deterministic trust/time rungs, no LLM judge). Rename in code/docs when the semantic path is built. |
| **DEC-21** | ✅ **LOCKED** *(this session)* | **Per-type processing model** (full spec: `PLAN_v0.3_memory_types.md` §5). **(1) Responder = pre-compute units, assemble live.** The sleep cycle pre-computes the *units* (semantic facts via bg extract; `Reflection` syntheses; `Profile` distillation; `Community` summaries; session summaries via `consolidate`, ≤600 chars); `read()` fuses them **live** (MMR + `boundary_index` + θ-abstain). **No pre-rendered response cache** — every unit stays a rebuildable Chronicle projection. "pre-calculated summary" (reflection/profile/community/session-summary) and "already-existing" (turns/facts) are *both* just records, pre-computed in sleep, fused live at read. **(2) Read surface = ONE fused `read()` (4-leg RRF) + keep per-type verbs** (`timeline`/`skills`/`related`/`due`) for single-type reads. Evidence: EverMemOS recallers + cognee 15 `SearchType` prove per-type paths are worth keeping; MemMachine/OpenMemory/mem0 prove the *default* must be one fused response; **no peer ships per-type-only**. **(3) Response shape = flat `AssembledContext` (+citations +trust) in v0.3 core; typed per-type envelopes fast-follow** (per DEC-17). **(4) Decay = two axes, distinct jobs:** M3 tier ladder (7/30/90 d, per-`memory_type` policy) drives **lifecycle/compression**; a **category decay multiplier** (associative fastest · semantic/causal slow — DEC-18) feeds **PPR weight**. **Substrate:** one event-sourced `MemoryRecord` + per-type policies (`decay`/`compression`/`consolidation` already bind by `memory_type`) + node projection (DEC-20) — combined store, separated by policy + projection. |
| **DEC-20** | ✅ **LOCKED** *(this session)* | **Node-model lock (coexist axis).** `memory_type` (record discriminator) and `node-type` (graph projection) are **orthogonal axes that coexist**: `memory_type` stays on every event (Chronicle = SoT, golden rule untouched); the **graph projector** materializes the reified node model from it (rebuildable — `rebuild()` still holds). The `memory_type → node(s)` mapping is the **one new load-bearing projector logic** — MUST be pure/deterministic so replay is stable. **Core node set (6):** `Entity` (NEW, first-class) · `Statement` (reified fact) · `Episode` (1:1 episodic) · `Reflection` (1:1 reflective) · `Community` (from associative/Leiden) · `Profile` (NEW, from preference, derived cache). **Statement reifies `semantic` + `preference` + `prospective`** — prospective is a fact with `valid_at > now` (near-free, reuses Chronicle bitemporal; `due()` = scan `valid_at > now`). **`procedural` + `shared` stay RECORD-based, NOT reified** — a skill is a procedure with a `skill_stage` ladder + dry-run (not an entity↔entity claim) and is retrieved by its own leg (`skills`/`recall_plan`); `shared` is authz bookkeeping. Graph-reification of procedural is **deferred** until a skill-dependency-traversal consumer (`recall_plan`) exists — same treatment as `shared`. **DEFER `Scene` + `Concept`** (YAGNI — no v0.3 consumer: Concept's `is_a`/`part_of` are DEC-18 predicate flags, not a node; Scene's synthesis is covered by Reflection). **Node-deps:** `Statement→Entity` (SUBJECT/OBJECT), `Community→Entity`, `Profile→Statement`, `Reflection→Statement/Episode`; type-dep `preference→semantic`. **Risk flag:** `Entity` resolution (`resolve_entity`) now gates **graph correctness** (not just dedup) → carry a HaluMem-extraction eval gate before Community/PPR are trusted. `resource`→`Source` node stays DEFERRED with resource (DEC-8). |
| **DEC-21** | ✅ accepted *(this session)* | **Memory-type scope boundary — exclude agent-runtime for a lean/fast loop.** SOTA agentic-personalization research (A-Mem, Mem0, Zep, JetBrains, Mastra, LoCoMo) **validates memspine's set as SOTA-complete — NO new memory type.** **SEPARATE FEATURES (own modules, NOT in the core fast conversation loop):** `perceptual` (multimodal/sensory input, state trackers) · `procedural` (skills, kept as-is per DEC-20). **EXCLUDE (agent-runtime, not memory at all):** scratchpad/chain-of-thought · orchestration loop / tool-arbiter (the agent framework) · fine-tuned system weights · agent tool/API registries + tool-routing (agent config). **Already covered:** self-evolving links = A-MEM associative · Observer dated-notes = reflective/consolidation · cross-session graph + staleness = Chronicle bitemporal + Entity graph · active parallel search = 4-leg read · temporal reasoning = Chronicle · structured JSON = reified `Statement`. **Adopt as *field shape*, not new types:** structured-observation (Intent/Decisions/Open-Questions) on Episode/Statement; **"vibe"/communication-style = preference overlay + persona, NOT a type.** Net: types stay **8 + preference overlay** → faster processing/loop. |
| **DEC-20** | ✅ accepted *(this session)* | **Memory-type ↔ graph-node reconciliation.** **Coexist model LOCKED:** `memory_type` stays on events (**event log = SoT, golden rule intact**); the **graph *projector*** upgrades to the reified node model (projection-layer change, not a core rewrite; `rebuild()` still works). **Keep ALL memory types** (registry unchanged). **Core nodes built in v0.3:** `Entity`, `Statement`, `Episode`, `Reflection`, `Community`, `Profile`. **Defer** `Scene`, `Concept` (YAGNI — no consumer). `shared` stays **record-based** (grant/sub — not reified). **`procedural` stays CURRENT** — `MemoryRecord` + `skill_stage` ladder; reification (→ `Procedure`/`Statement` node) + background skill-mining **DEFERRED to future**, not v0.3. `resource` deferred (DEC-8). **Deps:** add `preference→semantic`; node-deps `Statement→Entity`, `Community→Entity`, `Profile→Statement`, `Reflection→Statement/Episode`. |
| **DEC-19** | ✅ accepted *(this session)* | **Personalized-memory standard (companion spec).** **Preference is NOT a 5th category** — it's a **pipeline overlay** on `semantic` Statements (preference predicates + `reinforced` flag + preference fields), reusing the **epistemic axis** (`world`=user-stated, `opinion`=agent-inferred) → DEC-18's 4 categories hold. **Adopt:** confidence ladder E/C/I/S (`pref_class`); *implicit≠durable* (S-class expiry, no auto-promote to explicit); anti-patterns as **MUST-NOTs enforced at CAPTURE** (no stereotype fill-in, no hypothetical/third-person capture, no situational promotion, no unprompted sensitive recall, no profile-as-truth, no sycophancy loops); `Profile` = **derived cache** (projection of Statements, never SoT); negative prefs first-class (`dislikes`/`avoids`); sensitive-domain gating **DEFERRED** (out of v0.3 — `sensitive` field kept on the schema, list configurable later); erasure via the **existing `forget --hard --verify` / tombstone (M7)** — **crypto-shred DEFERRED** (out of v0.3); reinforce-on-use; drift→`SUPERSEDES` (Chronicle bitemporal); DDL delta (`pref_class`/`strength`/`situational`/`domain`/`sensitive`/`source` + `CONFIRMED_BY`/`FEEDBACK_ON`); eval gates PersonaMem v1/v2 · PrefEval · PERMA · HaluMem-persona · drift-replay; **no self-fulfilling profiles** (agent output stored `experience`, never `world`-about-user). **BOUNDARY:** memspine owns storage/ladder/promotion/reinforce/drift/distill/erasure/sensitive-gating **hooks**; the **agent** drives clarify/apply/feedback (PAHF loop) — do NOT build the agent loop into the engine. **Deferred (out of v0.3):** crypto-shred erasure + configurable sensitive-domain list — revisit post-v0.3. |
| **DEC-18** | ✅ **LOCKED** *(this session)* | **Memory-graph standard core (compresses `MEMORY_STANDARD.md` v1.0).** Ratify **edge-type-aware PPR** as the read path (GAAMA/HippoRAG) — this is the consumer that justifies categories and **supersedes DEC-16's 2-category conclusion**. **Fact reification:** entity↔entity claims are `Statement` **nodes** (Kùzu can't index edge props) — kept as a **rebuildable projection of Chronicle** (event log = SoT, never a second SoT). **4 categories only** (compressed from the spec's 9): `structural` (engine edges, prune/firewall-exempt) · `semantic` (default Statement) · `causal` (PPR boost 2.0/1.5) · `associative` (weak/fast-decay fallback). Nuance rides **predicate flags** (`transitive`, `reinforced`, `n_ary`, `symmetric`, `inverse`, `normative`) not new categories. **Epistemic-role axis** (`world`/`experience`/`opinion`/`observation`) is orthogonal (a field on every Statement). Rule: *a category exists only if PPR/decay/prune branches on it.* `temporal`→Chronicle `valid_at`; `deontic` behavior deferred (no consumer). |
| **DEC-17** | ✅ accepted *(this session)* | **`read()` response contract** (from the memory-response survey — see §6d). `read()` stays a **0-LLM evidence verb** (differentiator: memspine/MemMachine/graphiti are the only engines with 0 LLM calls on the read hot path). **Core (v0.3):** (1) **two-tier, evidence-default** — `read()` returns context; `read(..., synthesize=True)` adds an LLM answer behind a flag (never forced); (3) **citation handles** — each placed record gets a stable `[i:record_id]` ref in `AssembledContext` (MemOS pattern; memspine gap); (4) **surface trust** — attach per-record `{trust, disputed, freshness}` from E1 in the response (**no peer does this — memspine-first**). **Fast-follow:** (2) **per-type envelopes** — typed response shapes (semantic→facts+validity, episodic→timeline, procedural→skill cards, associative→subgraph, working→persona+summary; MemMachine/EverMemOS pattern) instead of flat `MemoryRecord+score`; (5) **fusion order** — one budget, persona → semantic → episodic → associative. Keep the existing `boundary_index` (E2 cache prefix — memspine-alone) + MMR + θ-abstain. |
| **DEC-16** | ⚠️ **SUPERSEDED by DEC-18** *(adversarial review)* | *Superseded:* the 2-category conclusion below held only while **no code read `rel_type`**; DEC-18 introduces the consumer (**edge-type-aware PPR**), which restores the 4-category model. Retained for provenance — the *disciplines* still bind (importance rides `weight`; predicates are non-binding slugs; category-driven traversal needs a real consumer, now satisfied by DEC-18). **Relation *vocabulary*, not *taxonomy*.** 2-agent devil's-advocate review found **no code reads `rel_type` semantically** (PPR/Leiden = weight-only; `related()` = no rel filter; only behavioral split = `RESERVED_RELS` vs open). So: **KEEP 2 categories** — `structural` (reserved: `derived_from`, `community`; prune/firewall-exempt; = today's `RESERVED_RELS`) + `associative` (generic default `related` + **all** user-defined, graphiti-style). **REMOVE** `causal` behavior (redundant with `weight` — a heavier edge is already boosted; no "why" consumer) and `hierarchical`/transitivity (no closure consumer; `alias_of`→dedup, `part_of` blurs with `community`). **REMOVE** the fixed predicate→category map (schema commitment, asymmetric churn risk) and `supersedes` (collides with `invalidate_edge` temporal lifecycle) + `mentions` (no producer). Predicates (`is_a`/`causes`/…) ship as **non-binding recommended slugs** in `extract_edges.yaml` (LLM-read strings, zero behavior); importance rides `weight`. Name = **`related`** (code default), not `related_to`. Category-driven traversal **deferred behind an ADR + a real consumer**. **Forward-compat ACCEPTED (door open):** one open `category` string on edges (default `associative`, only `structural` enforced) with a hard invariant that **no v0.3 retrieval/traversal keys on it** — buys future analytics with zero dead machinery. |

## 4b. Identifier hierarchy (DEC-4/4b/7) — ✅ FINALISED (planning)

Four nested levels for filtration + a flat `meta` bag. `session_id`/`message_id`
**reuse existing `SourceInfo` fields**; `group_id` is **completely renamed to
`thread_id`** in the core (greenfield — see DEC-4b).

```
namespace  ⊃  session_id  ⊃  thread_id  ⊃  message_id    + meta (flat, filterable)
 tenant        conversation    exchange       message
```

| Facade field | Means | Core field |
|---|---|---|
| `namespace` | tenant / owner (isolation boundary) | `namespace` |
| `session_id` | the whole **conversation** (many exchanges) | `SourceInfo.session_id` *(reuse)* |
| `thread_id` | an **exchange** — user turn + AI response **+ any ask-backs/clarifications** (**flexible: many messages per `thread_id`**, not strictly a pair) — **the interlink** | **`thread_id`** *(core field, renamed from `group_id`)* |
| `message_id` | individual message; **auto-generated**, optional to pass | `SourceInfo.message_id` *(reuse)* |
| `meta` | arbitrary labels/kv, **flattened + filterable** (absorbs `tags`) | — |
| `role` | ingest: `user` \| `assistant` \| `system` **required per message** (core also: `operator`/`tool`); read **filter**: `user`/`assistant`/`system`/**`all`** (default `all`) | `SourceInfo.role` |

- **`thread_id` is the interlink** — a user msg, its AI reply, and any follow-up
  ask-backs **share one `thread_id`** (replaces the earlier generic `id`).
- **Flexible cardinality** — a `thread_id` may hold 2…N messages (the assistant
  can ask back / send multiple messages); the default is the user+assistant pair.
- **Filtration:** `read(query, namespace, session_id?, thread_id?, message_id?, role="all", meta?)`
  narrows at any level. `role` accepts `user`/`assistant`/`system`/**`all`** (default
  `all` = no role filter — e.g. `role="user"` returns only user-stated content).
- **Ingestion modes (DEC-7):** single request `add([user, assistant, …], session_id,
  thread_id)` *or* multiple requests sharing the same `thread_id`. Same `thread_id`
  ⇒ same exchange ⇒ the assistant-corroboration rule (DEC-5) can fire.

## 4c. Firewall (E1) in v0.3 (DEC-9/10/11/12)

The firewall stays memspine's differentiator (the survey confirms **no peer has a
write-path content-governance firewall**; only MemOS has a *read-time, LLM-based*
one). v0.3 evolves it on four axes:

- **Configurable (DEC-9):** global on/off + per-signal toggles/thresholds
  (trust-matrix · instruction-regex · AgentPoison anomaly · MINJA bridge ·
  quarantine · corroboration).
- **Parallel / non-blocking (DEC-10):** `add()` anchors the event immediately;
  the firewall runs concurrently. **Safe-parallel** via a `PENDING_FIREWALL`
  status — the record is un-retrievable until the gate clears it to `ACTIVATED`
  or `QUARANTINED`. Hides the embed+query+scan latency with no poisoning window.
- **Two-sided (DEC-11):** the same deterministic gate runs on **`read()`** too — a
  read-side trust/verification pass (deterministic, unlike MemOS's LLM prompt) so
  retrieved content is trust-checked *before it reaches the model*. Ecosystem-empty.
- **Role split (DEC-12):** `operator` (engine-internal, top trust) ≠ `system`
  (request system-prompt). Request roles = `user`/`assistant`/`system`.

Trust matrix (updated): `operator` 0.9 *(engine only)* · `system` 0.9 *(request)* ·
`user` 0.7 · `assistant` 0.5 · `tool` 0.4; external channels capped at 0.3.

**Callers today (to be reshaped):** the write door (`write`), `ingest`, `reflect`,
`watch` all pass the gate; `grant`/prompt-sync are exempt (engine-built). Under
DEC-8, `ingest`/`reflect` verbs go away but their content still passes the gate
via `add()`/background.

## 5. Open forks (to resolve before building)

| Fork | Question | Lean |
|------|----------|------|
| **FORK-A4** 🔴 *pivotal* | Extraction timing on `add()` — background-only vs sync vs **hybrid**? | **Hybrid**: turns + explicitly-keyed facts sync; LLM fact/edge mining in the sleep cycle; optional `add(..., extract="now")` to force a sync mine. |
| **FORK-A6** 🔴 | `INVALIDATE`/negation is currently **unreachable** (enum exists, ladder never returns it). Wire "user retracts a fact"? | Wire it — needs an ADR + a ladder rung or an LLM-judge seam. |
| **FORK-R8** 🔴 | Rerank default — flashrank is declared, 4+ peers rerank hot. On by default? | **On in `agent` profile**, off in `simple`. |
| **FORK-SURFACE** 🟡 *(from DEC-8)* | Do the specialized verbs (`add_skill`/`watch`/`associate`/`grant`/`skills`/`timeline`…) also collapse into `add()`/`read()`, or stay distinct? | Lean **stay distinct** — they're different *operations*, not add/read; only the write/read paths collapse. Revisit per verb. |
| **FORK-GRAPH** 🟡 *(from §mem-types)* | Associative store default — keep slim `sqlite_adjacency` or promote **kuzu** (field best-practice, embedded graph)? | Lean **keep `sqlite_adjacency` default** (slim-core D-03), document `kuzu` as recommended production graph. |
| ~~FORK-META~~ ✅ *resolved (DEC-4b)* | Does `meta` replace `group_id`+`tags` or coexist? | **Resolved:** `meta` absorbs `tags`; `group_id` → first-class **`thread_id`** (also flattened into `meta`). |
| FORK-lower | `reflect` prompt wiring (background); `query_rewrite` R2; graph-leg RRF fusion R5; buffer-then-flush A1. | Defer to a later v0.3 phase. |

## 6. Flow step-map (canonical recipe ⇄ memspine)

**`add()` write flow** — `A2` event-log anchor (sync, +typed roles) → `A5/A6`
dedup + M4 conflict (sync, keyed facts) → `A7/A8` embed + project (sync) →
**`A4/A9` LLM fact/edge mining + reconcile (background sleep cycle)**.

**`read()` recall flow** — `R1` embed → `R3` vector + `R4` BM25 → `R6` RRF →
`R7` E1 gate (ACTIVATED/non-quarantined/group/tags/**meta**) → `R9` M1 score →
`[R8` rerank`]` → `R11` assemble → `R10` reinforce-on-read.
*(R2 query-rewrite, R5 graph-leg fusion = deferred gaps.)*

## 6b. Per-memory-type extract/store (ecosystem survey) → add() routing

Survey of 14 peers (`ECOSYSTEM_MEMORY_TAXONOMY.md` §3.15, `_METHODOLOGY`, `_PROMPTS`).

| Memory type | Extraction type | Data repr | Extraction methodology (best practice) | Storage (best practice) | memspine | v0.3 `add()` routing |
|---|---|---|---|---|---|---|
| **semantic** | **LLM** (+NER) | embedding + graph | extract self-contained atomic facts, keyed (entity/attr), role-attributed → dedup (MinHash→cosine) → **bitemporal conflict** (graphiti, memspine) — *not* ADD-only (mem0 v3) | event-log record / bitemporal graph edge + vector | extract→firewall→2-stage dedup→**M4 ladder** (leads) | **HEAVY → background** |
| **episodic** | **deterministic** | event-log + embedding | store the turn **verbatim**, derive later (honcho/graphiti/memspine) — never LLM the raw turn | raw record / EpisodicNode (session_id, ts) | deterministic event append → consolidate in sleep | **deterministic store (sync)** |
| **working** | **deterministic** (+LLM summary on overflow) | KV / in-mem hot window | bounded hot window; rolling LLM summary on eviction (honcho/langmem); KV-activation cache (MemOS) | hot-window records / KV-cache | page-out→episodic + pinned persona | **deterministic store (sync)** |
| **resource** | *(deferred)* | — | multi-format parse → chunk → embed; cognee typed Document/Chunk richest | Document node + chunk vectors (cognee) | flat chunk WRITEs (firewall-gated; thin) | **DEFERRED — out of v0.3 (conversation-only)** |
| **procedural** | **LLM-derived** / API | relational (+graph) | derive skills: cluster-synth (EverMemOS), distill+merge (powermem); memspine leads **governance** | skill record w/ stage / graph skill node | add_skill/record_plan + **ladder + dry-run gate** (unique) | **background-derived (+ skill API)** |
| **reflective** | **LLM** (background) | embedding + graph (parent-linked) | background summaries/clusters; honcho dreamer (deductive/inductive, tracked premises), hindsight mental_model richest | insight/observation node w/ premises + parent links | `reflect()` depth≤2 parent-guarded (unique) | **background-derived** |
| **associative** | **LLM** edge-extract | **GRAPH** (edges/communities) | LLM entity+edge extraction graph-native (cognee/graphiti) + community detection (Leiden) | **embedded graph DB (kuzu)** — cognee/graphiti | LINK events → `sqlite_adjacency` (kuzu/ladybug extra) + PPR + Leiden (**thin store**) | **background-derived (sleep reorganize)** |
| **prospective** | **none** (API/config) | relational | `watch(due_at / entity-invalidation)` — memspine-**unique** real due/ack API | prospective record (due, entity, ack state) | watch/due/acknowledge + fact-invalidation | **config write, no extraction** |
| **shared** | **none** (API/config) | relational (grants) | `grant`/`subscribe` scoping; trust-capped live views (memspine); bank/mission cascade (hindsight) | grant record (namespace, scope, trust cap) + subscription | grant()/subscribe(); foreign records never copied; trust-capped `shared_search` | **config write, no extraction** |

*Coverage: semantic + associative near-universal (14/12); episodic/procedural/working/reflective common (10–11); resource ~7; prospective + shared memspine-rare/unique.*

**The convergent routing map (matches the field's division of labor):**
1. **semantic** → the heavy path (LLM extract + dedup + M4 conflict) — deferred to background (FORK-A4).
2. **episodic / working** → **deterministic store, synchronous** — never per-turn LLM. *(resource same class but DEFERRED out of v0.3 — DEC-8.)*
3. **reflective / procedural / associative** → **background-derived** (post-write / sleep).
4. **prospective / shared** → config/API writes, **no extraction**.

memspine **leads** on: the 9-type registry over one event-sourced `MemoryRecord`,
the semantic write pipeline (only graphiti matches on conflict, none on firewall),
and prospective/shared/reflective/procedural as first-class governed types.
**Thin vs field:** associative store (`sqlite_adjacency` vs embedded graph),
resource ingest schema (vs cognee typed docs), no working KV-activation tier.

## 6c. Extraction-prompt design (ecosystem prompts → `add()`) 

From `ECOSYSTEM_PROMPTS.md`. **Semantic extraction** is the only prompt `add()`
needs (episodic/working = deterministic; the rest = background prompts already in
the D-43 pack).

**Peer semantic-extraction prompts:**
| Peer | Instructs | Output | Role handling | Grounding / anti-halluc | Update/conflict |
|---|---|---|---|---|---|
| **mem0 V3** | "sole operation is **ADD**" — self-contained facts 15–80w (`:4620,5082`) | JSON `{id,text,attributed_to,linked_memory_ids}` (`:4613`) | `attributed_to: user\|assistant` (`:5083`); system skipped | "**no implicit attribute inference**" (`:4828`); existing shown by id | **ADD-only** — `DEFAULT_UPDATE_MEMORY_PROMPT` is **DEAD/legacy** (`:4589,4602`) |
| **graphiti** | entities + edge facts; speaker→first node (`:3127`) | structured nodes/edges | "extract the **speaker before the colon** as first entity" | "**do not infer data not present**" (`:930`); fact = one sentence, **never reasoning/hedging** (`:4203`); no integer IDs (`:1215`) | **inline bitemporal** invalidate_at |
| **cognee** | KG triples; session-distillation lessons | instructor `KnowledgeGraph` | distill: "**don't promote a claim that exists only in an assistant answer unless the user backs it**" (`:2536`) | **GROUNDED** — only what's in the slice | deterministic uuid5 merge |
| **MemOS** | structured items; `NAIVE_JUDGE_UPDATE_OR_ADD`; `MERGE` | JSON | chat windows | read-side 4-step verify | **background** relation-detect/resolve |
| **honcho** | explicit atomic facts about the **target peer** | observations | observer/observed peer-attributed | other turns as context | background dreamer, cosine supersede |
| **MemMachine** | per-domain profile update | add/delete commands | `producer_role` | — | **background** feature-update + consolidate |
| langmem / A-mem / powermem | trustcall patch / keyword-tag / FACT_RETRIEVAL | schema / JSON | — | trustcall grounding | in-place (no history) |

**Consensus & recommendation for memspine `add()`:**
1. **Extract-only, reconcile-later is the consensus** — mem0 V3 *killed* the ADD/UPDATE decision prompt; MemOS/MemMachine reconcile in background; only graphiti does it inline. → memspine's extraction prompt should **only emit clean facts; the conflict decision stays in the deterministic M4 ladder — NOT an LLM update prompt.** That determinism is a *differentiator* (no injectable "should I update?" LLM step).
2. **Fact shape:** self-contained atomic facts, one sentence, **no reasoning/hedging**, keyed with `entity`/`attribute` (memspine already keys — better than mem0's flat text).
3. **Role attribution:** stamp `attributed_to = producer_role`; **drop `system`**; **"don't promote assistant-only claims unless the user corroborates"** — cognee's prompt line **validates memspine's DEC-5 rule verbatim.**
4. **Grounding devices to copy:** "use ONLY explicitly stated facts", "no implicit attribute inference (gender/age/…)", present existing memories by short id (avoids UUID hallucination).
5. **memspine already has (D-43 + v0.2 C1/B3):** `extract` role, `extract_edges`/`resolve_entity`/`invalidate_edge` (graphiti-style), `consolidate`/`reflect`, anti-injection framing. **The add() extraction prompt is mostly an assembly of prompts we already ship** — add role-attribution + the DEC-5 corroboration clause + strict grounding, then hand to M4.

## 6d. `read()` response — survey + per-type shapes (DEC-17)

Memory-response survey (14 peers, `ECOSYSTEM_COMPARISON.md` §3.5/§3.11/§3.14,
`ARCHITECTURE_FLOWS.md` §9).

**The spectrum** (what a read returns): `(a) raw records → (b) ranked+scores →
(c) assembled context → (d) synthesized answer`. **Engine/product line = between
(b/c).** memspine, MemMachine, graphiti = **only engines with 0 LLM calls on the
read hot path** (differentiator to keep). Mature systems offer **both tiers under
different verbs** (hindsight `recall`/`reflect`, cognee `CHUNKS`/`GRAPH_COMPLETION`)
→ memspine: cheap `read()` + opt-in `synthesize=True`, never forced.

**Best per-type response shape** (fast-follow envelopes, move 2):
| Type | Response shape | Precedent |
|---|---|---|
| semantic | facts + **validity window** + confidence | graphiti, MemOS |
| episodic | **chronological timeline**, session-grouped | MemMachine, EverMemOS |
| procedural | **skill card** (intent/approach/steps) | EverMemOS AgentSkillRecaller |
| associative | **subgraph / PPR-ranked neighbors** | graphiti, memspine `related()` |
| working | **persona + recent-window summary** | EverMemOS, honcho |

**memspine leads:** `AssembledContext.boundary_index` (E2 cache prefix — *no peer*),
MMR + token-budget + **θ-abstain** ("honest I-don't-know"). **Gaps → DEC-17 core:**
no citation handles (add `[i:record_id]`); trust not surfaced in output (add
`{trust, disputed, freshness}` — memspine-first). memspine already has per-type
read **verbs** (`timeline`/`skills`/`related`/`due`) but a **uniform return shape** —
the envelopes (move 2) make the *shape* typed too.

## 6e. Master processing pipeline — input → type → process → store → retrieve

**Two layers:** *anchored* (sync, verbatim, queryable t+0) vs *derived* (background,
projection of anchored + Chronicle, converges <60s). Chronicle = SoT for all.
**Scope:** v0.3 is **conversation memory** — `resource` (document intake) is
**DEFERRED** (out of v0.3, no `ingest()`). **8 types** below.

| Memory type | Input | Classify (→type) | Process (timing) | Store (node / backing) | Retrieve leg |
|---|---|---|---|---|---|
| **episodic** | turn | direct (always) | deterministic verbatim (**sync**) | Chronicle event + `Episode` node + vector | timeline; feeds all legs |
| **working** | recent turns | tail window | hot window + persona (**sync**) | Chronicle tail + `Episode` + persona slot | STM prefix of every read |
| **semantic** | turn | LLM extract (**bg**) | extract→firewall→dedup→**Chronicle** conflict | `Statement` (category, epistemic) + SUBJECT/OBJECT + vector | semantic kNN + graph PPR |
| **preference** | turn | LLM extract (**bg**) | ladder E/C/I/S + reinforce + distill | `Statement` (semantic+`reinforced`) + `Profile` | personal (proportional) |
| **associative** | Statements/entities | derived (**bg**) | edge-extract + A-MEM autolink + Leiden | structural graph edges + `Community` | graph PPR |
| **reflective** | episodic/semantic | derived (**bg**) | LLM synth, depth≤2 | `Reflection` + SYNTHESIZED_FROM + vector | semantic (high-level) |
| **procedural** | API + bg mining | API + derived | ladder + dry-run; optional cluster-mine | **record + `skill_stage`** (NOT reified — DEC-20; graph-reify deferred) | skills / recall_plan |
| **prospective** | API (`watch`) | API | store `valid_at`>now (**sync**) | `Statement` (Foresight) | due() (time-triggered) |
| **shared** | API (`grant`) | API | grant/sub record (**sync**) | grant/subscription record | shared_search (trust-capped) |

**Read-combine (one `read()`):** 4-way parallel legs — semantic (Statement kNN) ·
lexical (BM25) · graph (edge-type-aware PPR) · temporal (write-time metadata) →
**RRF** → cross-encoder rerank → **typed-envelope assembly** under one token budget,
ordered **persona → semantic facts (+validity) → episodic timeline → associative
neighbors**, with citations + trust surfacing (DEC-17). **STM legs (working/episodic
tail) are always fresh (t+0); LTM legs (semantic/associative/reflective) may lag** but
converge — `recall@k` at t+0 MUST ≈ t+∞ for verbatim-Episode facts (eval E5).

## 6f. Node model (DEC-20) — memory_type → graph projection

Two orthogonal axes **coexist**: `memory_type` on every event (Chronicle = SoT) and
`node-type` in the graph (a **rebuildable projection**). The projector's
`memory_type → node(s)` map is the only new load-bearing logic — pure/deterministic.

| memory_type (SoT) | Projects to node(s) | New? | Node-dep | Reify? |
|---|---|---|---|---|
| episodic | `Episode` | maps 1:1 | — | anchor |
| working | `Episode` (tail) + persona slot | reuses Episode | — | — |
| semantic | `Statement` | reify record→node | `Entity` (SUBJECT/OBJECT) | ✅ |
| preference *(overlay on semantic)* | `Statement` (+`reinforced`) + `Profile` | Profile NEW | `Statement` | ✅ |
| prospective | `Statement` (Foresight, `valid_at`>now) | reify | `Entity` | ✅ (near-free via Chronicle) |
| reflective | `Reflection` | maps 1:1 | `Statement`/`Episode` | ✅ |
| associative | structural edges + `Community` | maps (Leiden) | `Entity` | edges only |
| procedural | **record + `skill_stage`** | unchanged | — | ❌ deferred (own leg; reify when `recall_plan` traverses skills) |
| shared | grant/subscription **record** | unchanged | — | ❌ authz bookkeeping, not a fact |
| resource | `Source` | — | — | ⏸ DEFERRED with resource (DEC-8) |

**Core node set (6):** `Entity` · `Statement` · `Episode` · `Reflection` · `Community`
· `Profile`. **Deferred (YAGNI, no v0.3 consumer):** `Scene` (Reflection covers
episode-synthesis), `Concept` (`is_a`/`part_of` are DEC-18 predicate flags, not a node).
**Highest-risk new build:** `Entity` resolution now gates *graph correctness* → HaluMem
eval gate before Community/PPR are trusted.

## 6f. Master memory-type registry (FINAL)

Two orthogonal axes on every `Statement`: **category** ∈ {structural, semantic,
causal, associative} · **epistemic_role** ∈ {world, experience, opinion, observation}.

| # | Type | Verdict | Timing | Store (node) | Retrieve | Key params | Feature |
|---|---|---|---|---|---|---|---|
| 1 | **episodic** | ✅ v0.3 core | sync | Chronicle + `Episode` + vec | timeline; feeds all | role · session_id · thread_id · message_id · content · valid_at | verbatim never-lossy history; session/thread grouping |
| 2 | **working** | ✅ v0.3 core | sync | Chronicle tail + persona | STM prefix (t+0) | window_size · persona · page-out | bounded hot context; persona pin; cache-prefix |
| 3 | **semantic** | ✅ v0.3 core | LLM bg | `Statement` + `Entity` + vec | semantic kNN + PPR | predicate · category · epistemic_role · fact · confidence · weight · valid_at/invalid_at · attested · derived_from | durable facts; Chronicle bitemporal; entity graph; edge-type PPR |
| 4 | **preference** *(overlay)* | ✅ v0.3 overlay | LLM bg | `Statement`(reinforced) + `Profile` | personal (proportional) | pref_class E/C/I/S · strength · situational · domain · sensitive · source | personalization; confidence ladder; drift; Profile |
| 5 | **associative** | ✅ v0.3 core | derived bg | structural edges + `Community` | graph PPR | rel/predicate · category · weight · symmetric · Community(level,summary) | link graph; PPR recall; Leiden; A-MEM self-evolve |
| 6 | **reflective** | ✅ v0.3 core | LLM bg | `Reflection` + vec | semantic (high-level) | insight · reflection_depth · confidence · parent/source | higher-order insight; Observer notes; cross-session synthesis |
| 7 | **prospective** | ✅ v0.3 core | API sync | `Statement`(valid_at>now) | due() | due_at ∨ entity/attribute · ack | reminders; deadlines; fact-invalidation watches |
| 8 | **shared** | ✅ v0.3 core *(record)* | API sync | grant/sub record | shared_search | namespace · scope · trust_cap · memory_types | cross-namespace share; trust-capped live views |
| 9 | **procedural** | 🔷 **separate feature** | API + mining | `MemoryRecord` + skill_stage | skills / recall_plan | skill_stage(draft→active) · dry_run_passed · task_embedding | reusable skills; promotion ladder; plan cache |
| 10 | **perceptual** | 🔷 **separate feature** | *(own module)* | embedding registries *(TBD)* | *(TBD)* | modality · tensors · state flags | multimodal/sensory input; state tracking |
| 11 | **resource** | ⏸ deferred | sync chunk | chunk / `Document` | vector + lexical | doc_id · chunk_id | document RAG; chunk store |

**Core fast conversation loop (v0.3):** types 1–8 (episodic, working, semantic,
preference-overlay, associative, reflective, prospective, shared). **Separate
features (own module, off the fast loop):** procedural (9), perceptual (10).
**Deferred:** resource (11).

## 7. Evidence base

- `docs/ARCHITECTURE_FLOWS.md` — memspine write/read/sleep, code-traced.
- `docs/ECOSYSTEM_METHODOLOGY.md` — canonical write/read recipe across 25 peers.
- `docs/ECOSYSTEM_COMPARISON.md` — differentiators; MemMachine = closest event-log analog.
- Session traces (2026-07-11): memspine current orchestration (auto-vs-manual boundary);
  MemMachine `add_episodes` → background-ingestion wiring; cross-peer canonical recipe.

## 8. Build backlog (DEFERRED — execute only when a v0.3 build phase opens)

Captured now so the scope isn't lost; **none of this is executed yet**.

- **B1 — Rename `group_id` → `thread_id`** (DEC-4b). Scope (~58 refs): `core/records.py`,
  `engine.py` (write/write_messages/write_episode/search/retrieve params),
  `protocols/rest/{app,models}.py`, `services/storage/{base,sql_base,sqlite/schema}.py`,
  tests (`test_engine_group_tags.py` → `…thread…`, `test_app.py`, `test_sqlite_engine.py`),
  `ADR-027`, README. **Migration: delete `0001`+`0002` and recreate a single `0001`
  baseline from `schema.py`** (greenfield). Verify `mypy --strict` + ruff + tests green.
- **B2 — `add()` / `read()` facade** (DEC-1/3) with the §4b identifiers + `meta` flattening.
- **B3 — role-typed ingest** (DEC-5): `producer_role`/`produced_for`, drop system turns.
- **B4 — `agent` profile** (DEC-6): add/read on, extraction-in-sleep, scheduler on, rerank on.
- **B5..** — resolved forks (A4 extraction timing, A6 negation, R8 rerank) become tasks once decided.
- **B6 — Per-type `add()` dispatch** (see `PLAN_v0.3_memory_types.md` §4): sync-deterministic (episodic/working) · background-LLM (semantic/preference/reflective/associative) · config (prospective/shared). *(resource dispatch deferred — DEC-8.)*
- **B7 — Wire `reflect`** as a background sleep stage (DEC-8 removes the verb; the `reflect` prompt is currently unwired) + **wire `invalidate_edge`** (bitemporal LINK invalidation, graphiti parity).
- **B8 — Graph projector upgrade** (DEC-20): materialize the 6-node reified model (`Entity`/`Statement`/`Episode`/`Reflection`/`Community`/`Profile`) from Chronicle via a pure, deterministic `memory_type → node(s)` map; `rebuild()` must reproduce it byte-stable. `Statement` reifies semantic/preference/prospective; procedural + shared stay record-based; `Scene`/`Concept`/`Source` not built. **Gate:** `resolve_entity` HaluMem-extraction eval before Community/PPR trust `Entity` identity.

---

*Next up for discussion:* **FORK-A4** (extraction timing) — it decides whether
facts are live-on-write or live-after-sleep, and everything downstream keys off it.
