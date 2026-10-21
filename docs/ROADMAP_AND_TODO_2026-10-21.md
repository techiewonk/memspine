> **Reference note only. Current task status is maintained exclusively in [PROVIDER_TODO.md](PROVIDER_TODO.md).** Its previous checklist has been imported there, preserving existing task IDs and assigning RM IDs only to unmatched suggestions. Claims below record an earlier scan and may need revalidation; do not treat suggested commands as current authorisation.

# MemSpine: verified state, future plan and to-do (2026-10-21)

Companion to [CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md](CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md).
That handoff is the consolidated context; it says it was **not code-audited**. This document
records what a read-only scan of the code, git state and docs found on 2026-10-21, and turns it
into an ordered plan. No tests were run beyond collection, lint and type checks (section 2).

Tags: **[local]** can be done in this repo now · **[owner]** needs a decision or authorization ·
**[deploy]** needs a real target deployment.

---

## 1. Headline findings

1. **Almost nothing after `e6596f44` is committed.** About 80 modified, 3 deleted and 277
   untracked paths. This includes the Temporal + Resonate runner, DBOS removal, Python 3.13
   dependency refresh, GLiNER pool, named model profiles and escalation, GLiNER mentions and
   structured candidates, the SimHash/MinHash/RRF kernels, ADR-070..078 and the provider tracker
   itself. Docs describe all of it as done; `HEAD` does not contain it. This is the largest risk.
2. **Background ingestion BI-01..12 (D-99) and context enrichment (D-100) have no runtime code.**
   Grep finds none of the planned types (`RequiredDerivation`, `DerivationJob`, `EffectIdentity`,
   `ingestion_status`, `wait_for_derivation`, `PredicateSpec`, `EventOccurrence`, ...). The only
   working fact path still needs an LLM role (`ingest/windows.py:122-131`).
3. **Evals cannot load under the new provider schema.** 181/187 arms use flat `storage.path` and
   flat `embedding.*`; 134 use the removed `read.rerank_model`; 44 have LLM roles without
   `provider`. This includes the adopted default `evals/arms/v2-PICK3-j.json`.
   `evals/run_v2_batch.sh:7` passes the removed `--extra dbos`. (PA-107, deferred.)
4. **The local toolchain is behind the project.** Installed `uv` is 0.8.0; `pyproject.toml`
   requires `>=0.13.1`, so `uv run`, `just test` and `just lint` fail. Fix: `uv self update`.
5. **Core docs are stale** (CLAUDE.md counts, STATUS.md from 2026-10-06, README, CHANGELOG, ADR
   status lines, tracker counts). Full list in section 6.

## 2. Health snapshot (run 2026-10-21 via `.venv`, Python 3.13.5)

| Check | Result | Notes |
|---|---|---|
| Test collection, `tests/` | 7,910 collected | CLAUDE.md says 2,378. Last full sweep on record: 7,434 passed, 61 skipped (pre-follow-up waves). |
| Test collection, `evals/tests` | 1,888 collected | PA-107 records 185 failures + 7 errors. |
| `ruff check .` | 166 errors | All in untracked `evals/bench/` (128), `docs/schema_review/` (36), `evals/audit_locomo_no_llm` (2). Mostly E702/E701 one-liners, 11 B023. `src/` and `tests/` clean. |
| `ruff format --check .` | 346 files would be reformatted | Includes formatter-only churn mixed into real diffs (e.g. `engine.py`). |
| `mypy src` | 11 errors | All `import-not-found` for optional extras not installed in `.venv` (neo4j, weaviate, opensearchpy, pgvector). No type errors in code. |

## 3. Verified status by program

| Program | Status | In `HEAD`? |
|---|---|---|
| Provider architecture (ADR-068/069) | 103/136 DONE at local gates; 5 OPEN, 19 DEPLOYMENT_CHECK, 3 NEEDS_INPUT, 1 NEEDS_TOOLCHAIN, 1 DEFERRED, 4 EXCLUDED | Core yes (`e6596f44`); follow-ups no |
| Temporal + Resonate (ADR-071) | ACD-01/02/03/05/06/08/10 done; **ACD-04/07 pending** (ingestion outbox → Temporal); ACD-09/11/12 need environment | No |
| Python 3.13 deps (ADR-070) | Done locally; PA-109 llama-cpp Windows build needs toolchain | No |
| GLiNER shared pool (ADR-074) | Implemented, wired (`container.py:666`); not measured for RAM/latency | No |
| Named model profiles + escalation (ADR-075) | Implemented and wired | No |
| GLiNER grounded mentions (ADR-076) | Implemented on semantic + window-prepass paths. **Gap:** the graph ignores them (`memories/associative/entities.py:121-136`) | No |
| GLiNER structured candidates (ADR-072) | Standalone adapter `derive/gliner_structured.py`; **nothing imports it** | No |
| Performance kernels (ADR-073/074) | SimHash ~25x, MinHash ~7x, bounded RRF (only 2 graph-seed callers) implemented and measured at kernel level; no full-engine re-measure | No |
| Background ingestion BI-01..12 (ADR-077) | **PLANNED, no code** | — |
| Context enrichment (ADR-078), schema Q-S1..5 | Policies and choices accepted; review artifacts only | — |
| Evals migration (PA-107) | Deferred; arms broken by the schema change | — |

## 4. Plan

Phases are ordered by dependency. Each phase ends with its gate green before the next.

### Phase A: secure the work (do first)

- **[owner]** Authorize committing the working tree to `restructure` (no push). The plan docs
      say no commit is implied by approval of a document.
- **[local]** `uv self update` (>= 0.13.1), then `uv sync --locked --extra dev`.
- **[local]** Format-only commit first (`ruff format`), so later diffs are readable.
- **[local]** Commit in themed chunks, each with its focused tests rerun:
  - C1 Python 3.13 dependency refresh (pyproject, uv.lock, ci.yml, audit script, docs)
  - C2 DBOS → Temporal + Resonate (runner, workflow, step journal, schema, templates, tests, docs)
  - C3 Performance kernels (dedup.py, rrf_fuse, legs.py, constants, tests, benchmark scripts)
  - C4 GLiNER pool, model profiles, escalation (models/runtime, profiles.py, schema, container)
  - C5 GLiNER structured candidates and source-bound mentions (derive/gliner_*, entity_mentions,
    assertion/records/compile changes, `workers/windows.py` offset fix)
  - C6 Small fixes (community summary event time PA-114, intake_store loop variable)
  - C7 Verification tooling and evidence
  - C8 Design docs, ADR-077/078, schema review, research benchmarks
  - C9 Doc sync (last; see Phase B)
- **[owner]** Decide the fate of ~190 `docs/provider_verification/*.log|*.json` files:
      commit, prune, or `.gitignore`.
- **[local]** Fix the 166 ruff errors in `evals/bench/` and `docs/schema_review/` before C8,
      or exclude those research paths in ruff config.

### Phase B: make the docs true

- **[local]** CLAUDE.md: test count, ADR count/range (ADR-001..078), register range (D-100),
      `workers/pipelines.py` path (now `derive/pipeline/stages/table.py`), the `-p no:deepeval`
      rule (DeepEval removed), Cache/KV "LMDB" (default is `memory`), entities/gliner2 row
      (now `models.profiles`/`bindings`, gliner2.5), `uv sync --all-extras` vs locked baseline,
      repo layout (ADR-067 tree).
- **[local]** Replace STATUS.md with a current snapshot.
- **[local]** README: ADR count; OpenSearch/Weaviate are shipped extras, not "planned".
- **[local]** CHANGELOG entries for ADR-068/069/070/072-076.
- **[local]** ADR status lines: ADR-068 and ADR-071 ("in progress" → implemented, locally
      verified); ADR-070 superseded-by-ADR-071 note on DBOS; ADR-074 superseded-by-ADR-075 note
      on `models.gliner`; ADR-066 needs its register row.
- **[local]** PROVIDER_TODO.md: one ID count (136) everywhere; duplicate "## H." heading;
      stale "Recommended next sequence"; DONE rows that depend on unfinished rows
      (PA-042/093/094 on PA-039, PA-052 on PA-051, PA-055 on PA-053); PA-048/049/055 marked
      DONE but "Partial" in PROVIDER_PERFORMANCE_FOLLOWUP.md.
- **[local]** PROVIDER_ARCHITECTURE_PLAN.md stale sections: §2 current-code facts, §11
      pre-implementation boxes, "no commit performed" lines.
- **[local]** Stale-in-place sections in performance and enrichment docs (section 6).
- **[local]** DBOS references in active `docs/v2` briefs/areas/plan.json/tracker.html.
- **[local]** Fix UTF-8 mojibake in CPU_NON_LLM_BENCHMARK_RESULTS.md.

### Phase C: re-establish a verified baseline

- **[owner]** Authorize the broader sweep (PA-039).
- **[local]** Chunked full unit run with `-p no:deepeval`, on the committed revision; record
      with `scripts/record_verification_run.py`.
- **[local]** Rerun live Temporal gate (`MEMSPINE_TEST_TEMPORAL_ADDRESS`) and the strict
      native Docker gate on the same revision.
- **[local]** Close skip-review P2 milestones M-MODEL, M-BRANCH, M-LIVE, M-PLAN
      (review date 2026-10-25).
- **[local]** Scoped review of learned-alias isolation (engine-wide, not namespace-scoped).

### Phase D: unblock evals (small, high value)

- **[local]** `evals/run_v2_batch.sh`: drop `--extra dbos`.
- **[local]** `evals/memspine_evals/multiagent/constructions.py:47`: nested storage settings.
- **[owner]** Resume PA-107 early, at least for `v2-PICK3-j.json`.
- **[local]** Mechanical arm migrator: flat → `provider`/`settings`, `rerank_model` →
      rerank settings, add `provider` to LLM roles. Historical run records stay untouched.

### Phase E: background ingestion BI-01..12 (main feature work)

Follow handoff section 8; Q-S1..5 and the operating policy are settled, do not reopen them.

- **BI-01** contracts + config: runtime types for Q-S1..5 and operating defaults; annotation
      event vocabulary; receipt/status API (`ingestion_status`, `wait_for_derivation`); policy
      revision boundary. Rename the planned `SourceSnapshot` (collides with
      `providers/switching.SourceSnapshot`).
- Fixtures F1..F5 as executable schema + behaviour tests with negative/unresolved variants.
- Pure semantic registry `semantics/` with the 43 predicates / 9 attribute keys (no model or
      DB deps).
- **BI-02/03** required-derivation manifest at acceptance; durable raw → job discovery on the
      existing outbox; crash/reopen tests. Must land before any background default is enabled.
- **BI-04** isolated windows. **BI-05** persist mentions for every source regardless of fact
      yield. **BI-06** English rule packs, zero generative calls, unresolved outcomes.
      (BI-05 and BI-06 can run in parallel after BI-01.)
- **BI-07** fenced effect commit with erasure-generation checks, then **BI-08** graph
      occurrence projection merging rule `turn_mentions` and GLiNER mentions into one contract.
- **BI-09** readiness API, **BI-10** lossless compression verification, **BI-12**
      one-namespace smoke. BI-11 summaries stay separately selected.
- Q-S1-B is a coordinated breaking rewrite (compiler, gateway, storage, projectors, graph,
      readers, fixtures). Plan it as one cut-over.
- **ACD-04/07** outbox → Temporal ingestion: design together with BI-02/03 so there is one
      durable route; add the missing directive §8 tests (crash after business write, interrupt
      during LLM call, old-history versioning).

### Phase F: performance follow-ups (need owner budgets)

- **[owner]** PA-046: set a cold-start budget. **[local]** Re-measure on the committed
      revision (the +12.1% predates the kernels and later waves). Levers: ~180 ms SQLite/migration
      import on the startup path; LanceDB import ~858 ms.
- **[owner]** PA-051 controlled host; PA-053 foreground p95 and derivation-lag budgets.
- **[local]** Extend the PA-055 soak beyond 30 cycles.
- **[local]** Repair the HF cache snapshot, rerun embedding batching from a clean cache.
- **[local]** CPU model envelope: thread sweep, batch variants, ONNX/int8 parity.

### Phase G: release and deployment

- **[owner]** PA-095/096: PR destination and release workflow. Optionally trigger
      `provider-native.yml` remotely (never run remotely yet).
- **[local]** PA-113 dependency audit at release preparation (needs network).
- **[deploy]** PA-056 first (deployment, owner, runbook); unlocks PA-057..073 and PA-082.
- **[deploy]** ACD-09 (check for old DBOS jobs), ACD-11 (volume failover drill),
      ACD-12 (canary rollout).

## 5. Owner decisions waiting

| # | Decision | Blocks |
|---|---|---|
| 1 | Commit the working tree (no push) | Everything |
| 2 | Verification evidence files: commit / prune / ignore | C7 commit |
| 3 | PA-039 full sweep authorization | Phase C |
| 4 | PA-107 resume (at least default arm) | Phase D |
| 5 | GLiNER default: Base (code) vs Small (proposed in CPU report, BI plan, handoff) | BI-05/06 |
| 6 | Reranker role: none / TinyBERT / MiniLM; 2K/3K comparison profile beside accepted 4K | Read path tuning |
| 7 | Vocabulary extensions (10 predicates / 35 keys) and 6 schema refinements | BI-06 scope |
| 8 | Unresolved/provisional entity representation | BI-01 |
| 9 | SQL-only (no embedding) ingestion support; reply session inheritance | BI-02 |
| 10 | PA-046 cold-start budget; PA-051 host; PA-053 budgets; PA-088 quality budget | Phase F |
| 11 | fastembed batch-size default; clean-cache requirement | Phase F |
| 12 | BM25S/BM25x offline evaluation (LX1-A) | Lexical |
| 13 | ACD-04/07 integration design selection | Phase E |
| 14 | PA-095/096 PR and release destinations; PA-109 toolchain | Phase G |
| 15 | Deployment inputs PA-056/064/070/071/072/073 | Phase G |

## 6. Known contradictions and stale text (fix list for Phase B)

- CLAUDE.md:9 counts (2,378 tests; 64 ADRs; D-89); :13/29/88 register ranges; :22 pipelines path;
  :25 deepeval rule vs :70; :39 LMDB; :45 gliner2 flag; :75 sync command; :95 layout.
- STATUS.md entirely stale (2026-10-06, `feat/integrity-mti`, 1,958 tests, 55 ADRs, dbos_runner).
- README.md:31 ADR count; :81/90/125/134/138 OpenSearch/Weaviate "planned".
- ADR-068:3 and ADR-071:3 "in progress"; ADR-070:14 DBOS 3.x; ADR-074:26-33 `models.gliner`.
- PYTHON313_DEPENDENCIES.md:37,52-53,122 list DBOS as current.
- PROVIDER_TODO.md ID counts (125/123/129/136), duplicate heading, typos at :15, stale sequence.
- PROVIDER_NEXT_MILESTONES.md:92-95 "8 OPEN" (now 5).
- provider_verification/README.md:38-40 guard findings "remain open" (fixed).
- TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md :82-84 vs :274-276; :157 vs :4-5; :186-187.
- ENGINE_PERFORMANCE_AUDIT.md:19-20 and PERFORMANCE_ALTERNATIVES_NON_JSON.md:68-69 predate the
  implemented kernels.
- GLINER25_INGESTION_PLAN.md:112 optional LLM enrichment vs zero-generative D-99 profile.
- Superseded-in-file sections: INGESTION_CONTEXT_ENRICHMENT.md:112-116,
  NON_LLM_GAP_REGISTER.md:84-97, ENRICHMENT_SCHEMA_CONTRACTS.md:217, PROVIDER_TODO.md:387,396.
- Static-embedding loader can download weights, vs "Automatic downloads: Off".

## 7. Known risks and defects on record

- Uncommitted working tree (section 1).
- No clean full sweep since the follow-up waves; later work ran focused gates only.
- Cold start +12.1% and first config serialization +967% measured on `e6596f44` only.
- Learned aliases were engine-wide, not namespace-isolated (fixed by RM-18: scoped per namespace).
- Graph edges have no temporal fields; controlled mirror switching rejected; Neo4j binding breaks
  on credential rotation; prospective watches pull-only.
- Temporal: no volume affinity/failover; journal fencing does not fence external effects; native
  embedded SQLite Resonate unavailable; live evidence is one local run.
- Search p95 grows roughly linearly (116/252/503 ms at 100/500/1,000 records).
- 12-17 LanceDB cleanup warnings; ONNX cached snapshot resolution needed a workaround; Postgres
  `connect_timeout=1s` takes ~2.0 s.

## 8. Excluded (do not propose again)

PA-097 database copying · PA-098 historical re-embedding · PA-099 compatibility aliases/registries
· PA-100 production approval from local tests. Generative calls in the selected ingestion profile.
Benchmark-specific names, aliases or answer rules.
