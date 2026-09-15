# 📊 memspine — Status

> Manually refreshed · **last refresh: 2026-07-12 (IST)** · working tree: 33 uncommitted change(s) on `main`.
> ⚠️ The 30-min auto-refresh task appears to have stopped (previous auto-refresh was 2026-07-08). See note at bottom.

| | |
|---|---|
| **Milestone** | ✅ v0.1 Release Hardening done (bar infra-gated live check) · ✅ **v0.2 landed** · 🟡 **v0.3 in planning (lock — no code yet)** |
| **Tests** | **707** test functions across 445 files |
| **ADRs** | **29** (ADR-001 … ADR-028, +register) |
| **Version** | `pyproject` still `0.0.1` — pre-alpha (not yet bumped despite v0.2 work) |
| **Latest commit** | `7a66840` — docs(readme): community-detection usage + related() strategy example (2026-07-11 19:36) |

### 🏗️ Architecture — four-layer engine, event-sourced core
- **core** — audit · erasure · events · firewall · namespace · projector · records · registry · replay
- **memories (9/9)** — working · episodic · semantic · procedural · reflective · resource · associative · prospective · shared _(prospective + shared are memspine-rare/unique vs 14 surveyed peers)_
- **services** — cache · embedding · graph · lexical · llm · rerank · secrets · storage · vector
- **workers** — dbos_runner · inline · pipelines · runner · schedule · taskiq_runner
- **prompts** — registry · loader · YAML default pack
- **config** — loader · schema · templates
- **protocols** — REST (FastAPI factory; no-authn by design in v0.1)

### ✅ Done
- **v0.1 (P0–P7 + composable stores, Phases 1–14):** substrate · working memory + retrieval · semantic (M4 conflict ladder, M5 dedup) · episodic + lifecycle · Memory Firewall · procedural + reflective · associative graph · prospective + shared + REST · pluggable ports (cache → secrets → litellm → lexical → Postgres).
- **v0.1 Release Hardening:** Phase 15 ✅ (docs + drift test), Phase 17 ✅ (Alembic squash to single baseline). Phase 16 ⏭ **skipped** — needs live Postgres/Redis infra.
- **v0.2 (landed on `main`):** default-on hybrid retrieval · **Tantivy-core lexical** · `llmlingua-2` compression (per-band budgets) · configurable graph-traversal strategies for `related()` · `group_id` + tags sub-scoping (ADR-027) · autonomous sleep scheduler · `write_messages()` / `write_episode()` transcript verbs · graphiti-style async/sync WritePipeline (ADR-026) · community detection via **leidenalg** (ADR-028, drops numpy<2 conflict) · LanceDB as sole core vector store (ADR-021).

### 🟡 In planning — v0.3 (PLANNING LOCK, do NOT execute)
- **Orchestration Facade:** add an ergonomic `add(messages)` / `read(query)` conversation-memory layer over the existing 42 low-level verbs — no core changes, delivered via a new **`agent` profile** (`profile="simple"` stays byte-identical).
- Architectural analog = **MemMachine** (background LLM ingestion) + **MemOS** wiring; ergonomics borrowed from **mem0**'s 2-call surface. Per-type finalized plan in `.planning/PLAN_v0.3_memory_types.md`.
- Plans dated 2026-07-11/12 · status: discussion/design, **no build phase open yet**.

### 🔜 Next
- Approve the v0.3 design → promote accepted decisions to ADRs → open a build phase.
- Run the infra-gated live-backend verification (Phase 16) when a Postgres/Redis server is available.
- Bump `pyproject` version to reflect the v0.2 line.
- Commit / triage the 33 uncommitted changes (docs, engine, services, tests).

### 🕑 Recent commits
```
7a66840 docs(readme): add community-detection usage + related() strategy example
b6a418b deps(community): replace graspologic with leidenalg — drop numpy<2 conflict (ADR-028)
c6a9fe3 docs(readme): update for v0.2 — default-on hybrid, Tantivy-core, cashews, new verbs
a55c98c v0.2 hardening: Tantivy-core lexical, :memory: scheduler guard, UTF-8 logs
9d9fd39 v0.2 F1-F4: compression depth — llmlingua-2 model, per-band budgets, preserve, decision rule
```

### ⚠️ Notes / drift
- **Auto-refresh stopped:** header previously claimed "auto-refreshed every 30 min" but the last automated refresh was 2026-07-08. This refresh is manual. Re-enable or recreate the scheduled task if you want it live again.
- **Stale git lock:** `.git/index.lock` is present and could block commits — remove it if no git process is running.
- **Version drift:** `pyproject` version is still `0.0.1` while v0.2 features have shipped and the CHANGELOG `[Unreleased]` section is populated.

<sub>Refreshed from `git log` + `src/` + `.planning/` scan on 2026-07-12.</sub>
