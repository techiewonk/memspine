# 📊 memspine — Status

> Manually refreshed · **last refresh: 2026-10-06 (IST)** · branch `feat/integrity-mti` (not yet merged to `main`).
> ⚠️ The 30-min auto-refresh task stopped in July (last automated refresh 2026-07-08). This refresh is manual.

| | |
|---|---|
| **Milestone** | ✅ v0.1 Release Hardening done (bar the infra-gated live check) · ✅ **v0.2 landed** · 🟡 **v0.3 in planning (lock: no code yet)** · 🧪 **integrity (MTI) opt-in: implemented on a branch, ADR-029 *proposed*** |
| **Tests** | **1940** in `tests/` (1918 passed, 23 skipped, 0 failed in chunked runs; 2026-10-06) + **360** in `evals/tests` (all pass) |
| **ADRs** | **50** (ADR-001 … ADR-051; 038 unassigned; ADR-029/030/031 *proposed*) + template · decision register through **D-75** |
| **Version** | `pyproject` still `0.0.1` (pre-alpha; not bumped despite v0.2) |
| **Latest commit** | `a414e04`: merge of the final privacy-review fixes. All non-paid items of the research master absorb list (Waves 1–4) are merged on `feat/integrity-mti`; see CHANGELOG `[Unreleased]` |

### 🏗️ Architecture — four-layer engine, event-sourced core
- **core**: audit · erasure · events · firewall · **integrity** · namespace · projector · records · registry · replay
- **memories (9/9)**: working · episodic · semantic · procedural · reflective · resource · associative · prospective · shared _(prospective and shared are rare or unique among 14 surveyed peers)_
- **services**: cache · embedding · graph · lexical · llm · rerank · secrets · storage · vector
- **workers**: dbos_runner · inline · pipelines · runner · schedule · taskiq_runner
- **prompts**: registry · loader · YAML default pack
- **config**: loader · schema (+ `integrity` block) · templates
- **protocols**: REST (FastAPI factory; no-authn by design in v0.1)
- **evals/** (outside the wheel, D-35): system-agnostic benchmark harness plus model-free multi-agent constructions

### ✅ Done
- **v0.1 (P0–P7 + composable stores, Phases 1–14)** and **v0.1 Release Hardening** (Phase 15 ✅, Phase 17 ✅; Phase 16 ⏭ skipped, since it needs live Postgres/Redis).
- **v0.2 (on `main`):** default-on hybrid retrieval · Tantivy-core lexical · `llmlingua-2` compression · graph-traversal strategies for `related()` · `group_id` + tags (ADR-027) · autonomous sleep scheduler · `write_messages()` / `write_episode()` · WritePipeline (ADR-026) · leidenalg communities (ADR-028) · LanceDB as the sole core vector store (ADR-021).
- **Evaluation harness (`evals/`, committed 2026-09-29):** `DatasetAdapter` / `SystemAdapter` / `RunProtocol`; provenance enforced in types; baselines `no-memory`, `full-context`, `naive-rag`, `verbatim`; LoCoMo / LongMemEval adapters (no data on disk yet).
- **Integrity / trust-horizon invariant, formerly MTI (branch, 2026-09-29, ADR-029 *proposed*, D-56):**
  - `derived_from` provenance with MTI-D;
  - per-grant attenuation, trust-weighted ranking and an admission threshold;
  - principal-bound corroboration;
  - a merge reinforcement gate;
  - `memory.expose` events, cross-namespace `audit_taint` and `rollback_taint`.

  In the same pass: EI-1 (search excludes grant bookkeeping), `write_ex()`, `assemble(shared=True)`. This is the evidence base for the AAMAS 2027 paper in the research repo (`paper_aamas27/`).

### 🟡 In planning — v0.3 (PLANNING LOCK, do NOT execute)
- **Orchestration Facade:** an ergonomic `add(messages)` / `read(query)` layer over the low-level verbs, via a new `agent` profile (`simple` stays byte-identical). Plans in `.planning/PLAN_v0.3.md` and `.planning/PLAN_v0.3_memory_types.md`.
- **Open forks blocking a build phase:** FORK-A4 (extraction timing on `add()`), FORK-A6 (wire `INVALIDATE`/retraction), FORK-R8 (rerank default), plus the 🟡 FORK-SURFACE and FORK-GRAPH.

### 🔜 Next (needs the maintainer)
- **Review ADR-029 and merge `feat/integrity-mti`.** Note that EI-1 changes default `search` output.
- Resolve the v0.3 forks, then approve the design, promote decisions to ADRs and open a build phase.
- Run the infra-gated live-backend verification (Phase 16) when Postgres/Redis are available.
- Bump the `pyproject` version to reflect the v0.2 line.
- Harness: fetch LoCoMo + LongMemEval (pin revisions), commit the dev/test split, run C0-1 in retrieval mode.

### 🕑 Recent commits
```
1216e36 feat(integrity): opt-in monotone trust invariant for shared memory (ADR-029, proposed)
d4edd18 commit
7a66840 docs(readme): add community-detection usage + related() strategy example
b6a418b deps(community): replace graspologic with leidenalg — drop numpy<2 conflict (ADR-028)
c6a9fe3 docs(readme): update for v0.2 — default-on hybrid, Tantivy-core, cashews, new verbs
```

### ⚠️ Notes / drift
- **Flaky on Windows:** `tests/integration/test_concurrent_engines.py::test_two_writers_one_file_no_lost_updates` also fails at `d4edd18`, so it predates the integrity work. `test_erasure_persistence.py::test_hard_forget_survives_reopen_and_rebuild_does_not_resurrect` fails only intermittently inside the full run and passes in isolation. Both look like file-lock/timing issues.
- **Stale git lock:** resolved (no `.git/index.lock` present).
- **Stale worktrees:** two Cursor worktrees (`worktree/pass5-catalog-a3f8c2e1`, `worktree/prompts-c9d2e4f1`) are marked prunable by git; left untouched.
- **Version drift:** `pyproject` is `0.0.1` while v0.2 features have shipped.

<sub>Refreshed from `git log`, `src/`, `.planning/` and a test collection on 2026-09-30.</sub>
