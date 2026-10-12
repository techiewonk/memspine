# MemSpine restructure plan: work packages and status

Generated from `data/build_plan.py` (the source of truth for rows); status is also kept on the live tracker page and synced back here.
112 packages cover all 155 decisions. Kinds: **move** = pure restructure (tests and CPU golden), **migration** = persisted schema change (upcaster and replay-equivalence tests), **api** = new contract or lifetime (contract tests), **behaviour** = needs a GPU screen through the paired gate, **measure** = a run, **infra** = tooling.

Layers follow the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph, L4 observations and profile, L5 standing answers, procedures and plans; ctx = working context. Ops are the loop operators (D deposit, K_C consolidate, K_P personalise, R retrieve, C compose, G generate). Each package is one task for one agent (Sonnet); packages in a `serial` lane run one at a time, different lanes may run in parallel once their dependencies are Done. Owns = first owned path and the count of further patterns (full list in the ownership table at the end; `OWNS` in `data/packages.py`).

| Status | Count |
|---|---|
| Not started | 93 |
| Ready | 0 |
| In progress | 0 |
| Blocked | 1 |
| Needs measurement | 3 |
| Done | 15 |
| Dropped | 0 |

## Phase 0: settle and prepare

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P0-01 | Paired adoption gate (focus, blind, full set) | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01, G-03 | ER-35 | - | infra | infra | `evals/eval_blind.py` +15 | Replay: identical arm NO CHANGE; missing set INCOMPLETE | **Done** | Manifests wired into focus/blind/paired (INCOMPARABLE), sealed blind exposure ledger, runner identity + cost (30101ed, fca7a00) |
| P0-02 | Environment pinning: separate venv per pinned run | - | R G | G-24 | ER-35 | - | infra | infra | `evals/pinned_run.sh` +3 | A fresh per-run venv reproduces clean-jina-full | **Needs measurement** | Per-run venv cache keyed by lock hash, dry-run, shared escape hatch (5f5a7f2); one GPU reproduction run pending (GPU stopped) |
| P0-03 | Run manifest and comparability key | - | D R C G | G-25 | ER-35, ER-34 | - | infra | infra | `evals/pinned_run.sh` +3 | Manifest records commit, env hash, flags, prompts, models, data hashes | **Done** | manifest.py, comparability key incl. budget, INCOMPARABLE verdict, cost block (5f5a7f2, 7346aca); runners populate identity in P0-01 |
| P0-04 | CPU golden, import-boundary contracts, method-to-module map of engine.py | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | - | X-03 | infra | infra | `.importlinter` +4 | Golden identical on two runs at HEAD; map covers every engine method | **Done** | 439 units mapped (0 heuristic), 14 import contracts, run_golden.sh (8689693) |
| P0-05 | Engine LLM fixes: hard per-call bound, think=False on Ollama | L1 L4 | G | P-15 | - | - | behaviour | infra | - | Regression tests | **Done** | 2d05ac7, 798c4b5; re-screens in P0-06 |
| P0-06 | Re-screen engine-LLM arms on the thinking fix (f-gc9, f-gc3, f-win, f-gc2) | L0 L1 L3 ctx | D K_C K_P R | O-07, O-16, W-12 | - | P0-01, P0-05 | measure | infra | - | Paired gate vs f-jref2-i2 on the focus slice | **Needs measurement** | GPU stopped by the owner; f-gc9 was net +15 before the fix |
| P0-07 | A03 query contract: full set, then blind sets | ctx | R | R-03 | - | P0-01 | measure | infra | - | PROMOTE on 1,540 vs clean-jina-full, then CONFIRMS on MAB-CR and ConvoMem | **Needs measurement** | Focus +15/-7 net +8 HOLD; full run stopped partway |
| P0-08 | Benchmark suite and behavioural controls | L0 L1 L4 L5 ctx scope | K_P R C G | G-02, G-04 | ER-36, ER-37 | P0-01 | infra | infra | `evals/suite/**` +3 | Dev: LoCoMo (+cat 5), OP-Bench; blind: MAB-CR, ConvoMem, PrefEval; behavioural suite blocks adoption | **Done** | evals/suite: 6 gates, judge ids validated, LongMemEval refused; behavioural baseline 5 PASS / 5 FAIL; safety FAIL rejects (2824ef5, 4e9e438) |
| P0-09 | Prompt gold-lint over partials and rendered prompts | L1 L3 | D | P-13, W-18 | ER-16, ER-38 | - | infra | infra | `tests/unit/test_prompt_gold_lint.py` +1 | Lint fails on any gold token in any prompt or partial | **Done** | Partials, 51 rendered prompts, 265 inline strings; 4 allowlisted false positives (5bee0d9) |
| X-00 | RESTRUCTURE.md: S0-S16, target tree, owned file sets per package | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | ER-01, ER-04 | - | infra | infra | - | Every F package has an owned-file set; reviewed by the owner | **Done** | Approved by the owner 2026-10-13 |
| X-01 | Agent brief template and owned-file checker in build_plan.py | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | ER-02, ER-04 | X-00 | infra | infra | `docs/v2/data/build_plan.py` +2 | No two concurrently runnable packages share an owned file | **Done** | OWNS map, checker, --brief generator (9a13ba1) |
| X-02 | CI: golden, import-linter, blocking gold-lint, single Alembic head, flag-evidence check | L0 L1 L4 L5 ctx scope | D R C | G-22 | ER-04, ER-38 | P0-04 | infra | infra | `.github/workflows/**` +7 | CI fails on each violation | **Done** | gates job; lint-imports 10 kept 0 broken (uvx 2.15); 13 default-on flags, 3 frozen pre-G-22 UNKNOWN (c526255) |
| X-03 | Golden fixture: synthetic conversations, hash embedder, reranker off | L0 L1 L4 L5 ctx scope | R C G | G-04 | - | - | infra | infra | `tests/golden/**` | No benchmark text; deterministic | **Done** | tests/golden (c4d61ac): 4 synthetic conversations, 14 queries, frozen clock; stable 6/6 runs |
| X-04 | Docs and ADR: ADR-067 restructure, no-shim wording, G-23 migration-time upcaster | L0 L1 L4 L5 ctx scope | D K_C | G-23 | ER-01 | - | infra | infra | `docs/adr/ADR-067-*` +2 | Docs consistent with 'no shims' | **Done** | ADR-067 (40fb1e2) |
| X-05 | Labelled sets: grounding, alias/homonym/numeric pairs, contradiction recall, global questions | L1 L3 | D K_C | W-17, W-19, O-13, O-12, O-17 | ER-12, ER-37 | - | infra | infra | `evals/labelled/**` +1 | Invented names, several languages; versioned | **Done** | evals/labelled: 62 grounding, 80 entity pairs, 44 contradictions, 10 conv + 20 global questions (da4c961, 2b81427) |
| MOD-01 | memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas | L0 L1 L3 L4 L5 ctx scope | D K_C K_P R C | G-16, M-26 | ER-32, ER-33 | X-00 | infra | other | `src/memspine/schemas/**` +4 | Every step in pipelines/*.md has a schema; export reproducible in CI | **Done** | 28 durable (8 kinds v1 + fixtures), 14 public, internal step types; invariants; strict-tier check. Open for W2-06a: planned+negated, ItemV1 chg (I-3) |
| X-06 | Licence check for a permissive reranker fallback | L0 L1 | R | R-14 | - | - | infra | infra | `docs/v2/data/reranker_licences_*.md` | Named fallback with licence | **Done** | Qwen3-Reranker-0.6B (Apache-2.0) primary, MiniLM-L6 CPU (5544e88); owner to confirm |

## Phase 1: foundations (restructure in place)

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| F-05 | Package skeleton; move store and trust leaves | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | - | P0-04, X-00, X-01, X-02 | move | S1 split (serial) | `src/memspine/store/records.py` +23 | Both suites and golden green; no old import paths | **Done** | S5: 11 modules moved, codemod_move.py; unit 3343, evals 1435, golden, 14 contracts kept (afb3d16) |
| F-06 | Move read, compose and models leaves | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | - | F-05 | move | S1 split (serial) | `src/memspine/read/**` +24 | Same as F-05 | **Not started** | S6 |
| MOD-02 | Typed registries + entry points (discovery is not enablement: pinned capability manifest) | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | ER-03, ER-33, ER-34 | F-06, MOD-01 | api | S1 split (serial) | `src/memspine/*/registry.py` | Built-ins register at import; entry-point plug-in test; golden unchanged | **Not started** | Owner 2026-10-13 |
| F-07 | Config split and flag registry (per-area flag modules) | L0 L1 L4 L5 ctx scope | D R C | G-21, G-22 | - | MOD-02 | move | S1 split (serial) | `src/memspine/config/schema/**` +2 | Same YAML keys; every default flag has evidence | **Not started** | S7 |
| F-08 | Wiring out of the engine into a dishka container (app, job, request scopes) | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | ER-03, ER-10, ER-34 | F-07 | api | S1 split (serial) | `src/memspine/runtime/container.py` +1 | Golden | **Not started** | S8 |
| F-09 | Write path out of the engine | L0 L1 L5 | D | W-01, W-09 | - | F-08 | move | S1 split (serial) | `src/memspine/ingest/**` +1 | Golden | **Not started** | S9 |
| F-10a | Read path out: legs, fusion, rerank | L0 L1 L3 | R | R-05 | - | F-09 | move | S1 split (serial) | `src/memspine/read/**` +1 | Golden byte-identical hit order | **Not started** | S10 |
| F-10b | Read path out: lift, gates, filters | L0 L1 L3 | R | R-05 | - | F-10a | move | S1 split (serial) | `src/memspine/read/**` +1 | Golden byte-identical hit order | **Not started** | S10 |
| F-11 | Compose out of the engine | ctx | C | R-20 | - | F-10b | move | S1 split (serial) | `src/memspine/compose/**` +1 | Golden context hash identical | **Not started** | S11 |
| F-12 | Trust, admin and memory-type mechanisms out | L4 ctx scope | K_C R C | M-21, M-24 | - | F-11 | move | S1 split (serial) | `src/memspine/trust/**` +2 | Golden | **Not started** | S12 |
| F-13a | Scoped handle; engine reduced to lifecycle and admin | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | ER-03 | F-12 | api | S1 split (serial) | `src/memspine/api/**` +3 | Golden | **Not started** | S13; no aliases |
| F-13b | REST and MCP on the handle | ctx scope | D R | G-17, G-18 | - | F-13a | move | S1 split (serial) | `src/memspine/api/**` +3 | Protocol tests | **Not started** |  |
| F-13c | Eval-harness adapter and test-import migration; paired-gate replay | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | - | F-13a | move | S1 split (serial) | `src/memspine/api/**` +3 | Paired-gate replay NO CHANGE vs f-jref2-i2 (GPU, one run) | **Not started** | Exit of the split |
| F-15 | Split the pipelines module into derive stages | L0 L1 L3 L4 L5 ctx | K_C K_P | O-04 | - | F-13c | move | S1 split (serial) | `src/memspine/derive/stages/**` +1 | Golden; stage order unchanged | **Not started** | S15 part 1 |
| F-14a | Retire measured-negative and owner-retired code (slot loop, fact chain, word vectors, decider gate, kuzu, opensearch stub, full/compose read modes, v1 read headers) | L0 L1 L4 L5 ctx scope | D R C | G-22 | ER-40 | F-13c | move | S1 split (serial) | - | Golden unchanged; retired flags raise ConfigError | **Not started** |  |
| F-14b | Retire unscreened candidates | L0 L1 L4 L5 ctx scope | D R C | G-22 | ER-40 | F-14a | move | S1 split (serial) | - | Owner decision per item | **Blocked** | Needs owner: event ledger, maxsim, causal walk, bridge hop, prefilter, novelty exclusions, agentic query |
| F-01 | Graph key identity: kind, occurrence, A-B-A, old-key aliases | L0 L1 L3 | D | M-17, W-20 | ER-13, ER-20 | F-15 | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/keys.py` +1 | A-B-A returns to A; same-day events stay two; replay idempotent; built flag-off, then GC arm screen | **Not started** | S1; G-W50 |
| F-02a | Record envelope: layer field, type tag, provenance class, axes, fingerprint; Alembic and upcaster | L0 L1 L3 L4 L5 ctx scope | D K_C R | M-02, M-03, M-06, M-07, M-08, P-12 | ER-03, ER-14, ER-32 | F-01, MOD-01 | migration | S2 foundations (serial: Alembic) | `src/memspine/store/envelope.py` +4 | Replay byte-stable; upcaster round-trips; golden unchanged | **Not started** | S2+S3; Q1 |
| F-02b | Tri-temporal times with precision; LayerRegistry (template-enabled and populated layers) | L0 L1 L3 L4 ctx | D K_C | M-10, M-02 | ER-03, ER-18 | F-02a | migration | S2 foundations (serial: Alembic) | `src/memspine/store/envelope.py` +4 | Golden unchanged; registry reports layers per namespace | **Not started** | Legs run only when their layer exists |
| F-02c | Edge compiler and link projection from assertions (graph ON by default with a worker model) | L1 L3 | D R | M-16 | ER-20 | F-02b | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/envelope.py` +4 | GC arm screen before the default counts in results | **Not started** | I-7: L3 on by default |
| F-03 | Model upgrades: versioned vector spaces, selective re-derivation | - | K_C | P-25 | - | F-02a | behaviour | S2 foundations (serial: Alembic) | - | Shadow index switch test | **Not started** |  |
| F-04 | Durable outbox and inline runner, read barrier; model calls outside namespace locks | L0 L1 L3 ctx | D K_C | W-02, W-10, W-13, W-25, G-19, P-21 | ER-06, ER-07, ER-09 | F-02b | behaviour | S2 foundations (serial: Alembic) | `src/memspine/runtime/**` +7 | Fault-injection tests; golden unchanged with barrier | **Not started** | S4; Q2 |
| RT-01 | Taskiq transport over the outbox: in-memory broker (library), external broker + worker entry point (server) | L0 L1 L3 ctx | D K_C | G-19, W-13 | ER-06 | F-04 | behaviour | S2 foundations (serial: Alembic) | `src/memspine/runtime/**` +7 | Crash tests with both brokers; unfinished rows re-sent on start | **Not started** | Owner 2026-10-13: outbox = truth, taskiq = transport |
| RT-02 | Executors, priority classes, global model semaphore, backpressure (coalesce, never drop) | L0 L1 ctx | D K_C R | P-20, W-25, P-21 | ER-08, ER-10, ER-39 | RT-01 | behaviour | S2 foundations (serial: Alembic) | `src/memspine/runtime/**` +7 | Load test: reads keep p95 under background load; no dropped work | **Not started** | Async core; GPU encoders micro-batched in dedicated executors |
| F-16a | Lineage table and the single derived-write door (layer, parents, trust cap, fingerprint) | L0 L1 L3 L4 L5 ctx scope | D K_C | M-09, G-08 | ER-11, ER-22, POL-2 | RT-02 | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | Every L1-L5 writer goes through the door; erasure contract test registered | **Not started** |  |
| F-16b | Event upcasters and migration by replay of the retained log (limited rebuild advertised) | L0 L1 L3 L4 L5 ctx scope | D K_C | M-01, M-26, G-23 | ER-27, ER-33 | F-16a | migration | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | v1 log replays byte-stably | **Not started** |  |
| F-16c | Stable memory scope in every key (session as provenance); viewer required on every multi-party read, bound to the authenticated principal | L0 L1 L3 L4 L5 ctx scope | D R | M-05, G-12 | ER-13, ER-29 | F-16a | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | Isolation probes | **Not started** |  |
| F-16d | Storage-profile contract; Postgres server profile and its Alembic tree | L0 L1 L3 L4 L5 ctx scope | R | M-04 | - | F-16b | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | Both profiles pass the store contract tests | **Not started** |  |
| F-16e | Neo4j graph adapter fed through the outbox | L3 | R | M-18 | - | F-16d, F-04 | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | Graph contract on Neo4j | **Not started** | Q4 |
| F-16f | Isolation, erasure and rebuild matrix on both profiles | L0 L1 L3 L4 L5 ctx scope | K_C | M-25 | ER-05, ER-30 | F-16e, F-16c | behaviour | S2 foundations (serial: Alembic) | `src/memspine/store/lineage.py` +6 | Matrix green on embedded and server | **Not started** |  |
| EX-1a | Embedded milestone: one synthetic end-to-end workload survives restart and erasure | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | ER-05 | F-16c, F-16b, F-02c, F-03 | measure | S2 foundations (serial: Alembic) | - | Restart, erasure and replay on the embedded profile; not a server release | **Not started** | ER-05 |
| EX-1 | Phase 1 exit (server milestone): raw-only read non-inferior; golden; replay NO CHANGE | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | ER-05 | F-16f, EX-1a, P0-03 | measure | S2 foundations (serial: Alembic) | - | ROADMAP Phase 1 exit gate | **Not started** |  |

## Phase 2: write path and models

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| W2-01 | Source envelope intake, receipt, idempotency, re-ingest | L0 L1 | D K_C | W-01, W-02, W-03, W-24 | ER-06 | EX-1a | behaviour | write | - | Write p95 at most 250 ms; idempotent retries; receipt never claims an unavailable capability; crash tests at every commit/projector boundary | **Not started** |  |
| W2-02 | Stamping: three clocks, time zone, speaker/addressee/participants, provenance class | L0 L1 L4 scope | D R | W-04, W-05, W-08, M-08 | - | W2-01 | behaviour | write | - | Focus slice non-inferior | **Not started** |  |
| W2-03 | Role policy, echo guard (marker + last-read overlap) and routing | L0 L1 L5 ctx | D | W-06, W-09 | ER-17 | W2-02 | behaviour | write | - | Focus slice non-inferior | **Not started** |  |
| W2-04 | Firewall at three gates; base plus extended signals | L0 L1 scope | D | W-07, G-06 | - | W2-03, F-16a | behaviour | write | - | False-positive screen on the behavioural suite | **Not started** | Q8 chat-preset exemption |
| W2-05 | Segmentation: adaptive window, time-gap cut, token batching | L0 | D | W-11 | - | W2-01 | behaviour | write | - | Calls per turn down, grounding inside band | **Not started** |  |
| W2-07 | Prompt layout rules: no examples, stable prefix, same language | L0 L1 L3 ctx | D C | W-18, P-07, P-08, P-10 | ER-16 | P0-09, EX-1a | behaviour | write | - | English output unchanged on focus slice (per partial) | **Not started** |  |
| W2-06a | ItemV1 to AssertionEnvelope compile, validation, repair re-ask, dead letters | L0 L1 L3 | D | W-14, W-15, P-05, P-06 | ER-11, ER-15, ER-32 | W2-05, F-16a | behaviour | S4 extraction (serial) | - | Unit tests; items dropped counted | **Not started** |  |
| W2-06b | Prompt v2 and schema-constrained decoding | L0 L1 L3 | D | W-16, P-04, P-09, P-11 | ER-12, ER-15 | W2-06a, W2-07 | behaviour | S4 extraction (serial) | - | Window arm: items dropped down; at most 1 call per window | **Not started** | G-W49 |
| W2-06c | Online extraction via the outbox with a stage hook list | L1 L3 | D R | W-12 | - | W2-06b, F-04 | behaviour | S4 extraction (serial) | - | No-model template: zero calls; window arm screen | **Not started** |  |
| W2-14 | Temporal extraction by every means: anchor ladder, basis and precision, relations without dates | L0 L1 L3 L5 | D R | W-04, M-10, R-07 | ER-18 | W2-06c | behaviour | S4 extraction (serial) | - | Temporal category up; no invented exact dates (precision audit) | **Not started** | Owner I-2 |
| W2-15 | Coverage check and targeted re-extraction (screened) | L1 L3 | D | W-17, W-15 | ER-12 | W2-08 | behaviour | S4 extraction (serial) | - | Omitted-fact recall and unsupported additions measured separately on unseen domains; extra calls counted | **Not started** | ER-12 |
| W2-08 | Grounding: spans per assertion, rule checks, assent | L1 L3 | D | W-17 | ER-11, ER-14 | W2-14, X-05 | behaviour | S4 extraction (serial) | - | Grounding at least 95% on the labelled set | **Not started** |  |
| W2-09 | Online entity resolution with pending_merge | L1 L3 | D K_C | W-19, M-15 | ER-13, ER-20 | W2-08 | behaviour | S4 extraction (serial) | - | Calibrated thresholds: no homonym merges above a stated rate | **Not started** |  |
| W2-10 | Integration ladder and cardinality registry | L1 L3 | D K_C | W-21, W-22, M-12 | ER-14, ER-17 | W2-09, F-01 | behaviour | S4 extraction (serial) | - | MAB-CR non-inferior; A-B-A tests; behavioural INV-ORDER-SWAP passes | **Not started** | Q7 coexist unless marked; P0-08 finding: reverse-order ingest leaves old and new value live (supersede follows ingest order, not event time) |
| W2-11 | Asset describer (ingest and read triggers, off by default) | L0 | D R | W-23 | ER-25 | W2-01 | behaviour | write | - | Off by default; on-arm screen | **Not started** |  |
| W2-12 | Cost ledger and write targets | L0 L1 L3 L4 L5 | D K_C K_P | W-26, P-14, P-22 | ER-08, ER-15, ER-16, ER-37, ER-39 | EX-1a | infra | write | - | Targets as regression tests | **Not started** |  |
| W2-13 | Small local pre-pass (GLiNER2) as hints | L1 L3 | D | P-23 | - | W2-10 | behaviour | S4 extraction (serial) | - | Calls down, grounded facts not down | **Not started** |  |
| MD-01 | Model tiers, placement, no-model fallbacks, device plan, thinking per role | L0 L1 L4 | D K_C R G | P-01, P-02, P-03, P-15, P-19 | - | EX-1a | behaviour | models | - | Worker-tier screen per role; no-model template zero calls | **Not started** |  |
| MD-02 | Default embedders (CPU multilingual, GPU profile) | L0 L1 | R | P-17, M-23 | - | EX-1a, F-03 | behaviour | models | - | English R@k within band; non-English probe | **Not started** |  |
| EX-2 | Phase 2 exit: window arm non-inferior, grounding 95%, write p95 | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | - | W2-10, W2-04, W2-13 | measure | exit | - | ROADMAP Phase 2 exit gate | **Not started** |  |

## Phase 3: read path

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R3-01 | Depth tiers and ReadRequest; evidence unit | L0 L1 L4 ctx | R C | R-01, R-02 | - | EX-1a | behaviour | S3 read (serial) | - | Fast tier zero generative calls; embed/rerank cost reported separately; p95 SLO | **Not started** |  |
| R3-02 | Typed query contract by rules; render header | ctx | R C | R-03, R-20 | ER-19 | P0-07, R3-01, F-11 | behaviour | S3 read (serial) | - | A03 full set and blind | **Not started** | Waits for P0-07 |
| R3-03 | Rule router reading the LayerRegistry; LLM rewrites only in deep tier | ctx | R | R-04 | - | R3-01, F-02b | behaviour | S3 read (serial) | - | Focus non-inferior | **Not started** |  |
| R3-04 | Core legs: vector, language-aware BM25, temporal on event intervals | L0 L1 L3 L5 | R | R-05, R-06, R-07 | - | R3-03 | behaviour | S3 read (serial) | - | Focus non-inferior; no-model template: L1 legs empty, not error | **Not started** |  |
| R3-05a | Entity leg with anti-hub damping | L1 L3 | R | R-08 | - | R3-04, W2-09 | behaviour | S3 read (serial) | - | Own focus and blind screen | **Not started** |  |
| R3-05b | Graph one-hop leg (only when L3 exists) | L3 | R | R-09 | ER-19 | R3-04, F-02c, F-16e | behaviour | S3 read (serial) | - | Own focus and blind screen | **Not started** |  |
| R3-05c | Episode leg lifted to turns (only when L2 exists) | L0 L4 | R | R-10 | - | R3-04, O4-02 | behaviour | S3 read (serial) | - | L2-enabled template arm | **Not started** |  |
| R3-06 | Family fusion, protected pool, calibrated floors | L0 L1 | R | R-11, R-12, R-13 | - | R3-04 | behaviour | S3 read (serial) | - | Source-family pool non-inferior to per-leg pool | **Not started** |  |
| R3-07 | Jina rerank on every read (pools capped at 64); licence fallback; bounded boosts | L0 L1 L4 ctx | R | R-14, R-15, R-16, P-18 | ER-21 | R3-08, X-06 | behaviour | S3 read (serial) | - | Matches the 81.4% reference; boosts off until screened | **Not started** | Q5, Q6 |
| R3-08 | Lift derived hits to turns BEFORE rerank (via lineage) | L0 L1 L3 L4 | R | R-17 | ER-21, ER-22, POL-2 | R3-06, F-16a | behaviour | S3 read (serial) | - | Focus non-inferior | **Not started** |  |
| R3-09a | Packer: exact reader-tokenizer accounting incl. headers, citations, reserves; early-break fix | ctx | C | R-18 | ER-24 | F-11 | behaviour | S3 read (serial) | - | Exact-tokenizer, multilingual and long-token tests; estimated mode labelled | **Not started** | ER-24; no quality screen needed |
| R3-09b | Adaptive complete evidence bundles and block shares | ctx | C | R-18 | ER-24 | R3-09a, R3-07 | behaviour | S3 read (serial) | - | Equal-budget screen; adaptive bundle selection gets a separate quality screen (ER-24) | **Not started** |  |
| R3-10 | Neighbour windows after rerank | L0 | C | R-19 | ER-24 | R3-07 | behaviour | S3 read (serial) | - | Focus non-inferior | **Not started** |  |
| R3-11 | Store-calibrated relevance gate with named bypass | L1 ctx | R | R-21 | ER-23, POL-1 | R3-10 | behaviour | S3 read (serial) | - | OP-Bench up; no recall loss on MAB-CR, ConvoMem, LoCoMo | **Not started** |  |
| R3-12 | Deep tier: one sufficiency check and one rewrite | L0 ctx | R C | R-22 | ER-19, ER-23 | R3-11 | behaviour | S3 read (serial) | - | f-suff: net beyond band, no category loss, at most 2x wall time | **Not started** |  |
| R3-13 | External public knowledge (opt-in, separate block) | L0 | R | R-23 | ER-25 | R3-01 | behaviour | S3 read (serial) | - | Separate arm; never mixed in headline | **Not started** |  |
| R3-14 | Time view and two-axis as-of reads | L0 L1 | R | R-24, M-11 | ER-18 | F-02b, R3-04 | behaviour | S3 read (serial) | - | Temporal and MAB-CR non-inferior | **Not started** |  |
| R3-16 | Evidence obligations for joins, durations and lists (screened) | L0 ctx | R C | R-03, R-22 | ER-19 | R3-12, W2-09 | behaviour | S3 read (serial) | - | Unseen intersections, durations and enumerations improve at fixed reader/token budgets; no more unsupported answers | **Not started** | ER-19 |
| R3-15 | Parallel legs, latency SLOs (declared hardware/concurrency), caches keyed by scope/version (off until ER-31 tests pass), prefix layout | L0 L1 ctx | D R C | R-25, R-26, P-20, P-24 | ER-09, ER-31, ER-39 | R3-12 | infra | S3 read (serial) | - | p95 SLO tests in CI; caching (result/context caches disabled until ER-31 keys and use-time admission tests pass) | **Not started** | Last in the read lane |
| EX-3 | Phase 3 exit: beats or matches 81.4% beyond band; blind sets hold | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | - | R3-15, R3-02 | measure | exit | - | ROADMAP Phase 3 exit gate | **Not started** |  |

## Phase 4: offline memory

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| O4-01a | Non-blocking sleep handles, triggers ((N AND M) OR context C, gap K), namespace jobs, leases, watermarks, daily maintenance | L0 L1 L3 L4 L5 ctx | K_C K_P | O-01, O-02, O-03 | ER-07, ER-26 | EX-1a, F-15 | behaviour | offline | - | Restart and lock fault tests; off by default | **Not started** |  |
| O4-01b | Background budget and freshness (covers_seq; read-side freshness) | L0 L1 L3 L4 ctx | K_C K_P R | O-05, O-06 | ER-09, ER-26 | O4-01a, F-16a | behaviour | offline | - | Deferred work counted, never dropped | **Not started** |  |
| O4-02 | Call 2 at session close: summary, stitched episode, profile ops (L2, opt-in) | L0 L1 ctx | K_C K_P | O-07, O-08, O-09, M-14 | ER-26 | O4-01a, F-16a | behaviour | offline | - | G-C9-style screen; golden unchanged with flag off | **Not started** | f-gc9 +15 before the thinking fix |
| O4-03 | Reconcile backlog, staged entity resolution, contradictions | L1 L3 | K_C | O-10, O-11, O-12 | - | O4-01a, W2-09, X-05 | behaviour | offline | - | Contradiction-recall set; MAB-CR non-inferior | **Not started** |  |
| O4-04 | Near-duplicate merge and value histories | L1 L4 | K_C | O-13, O-14, M-13 | ER-17 | O4-03, W2-10 | behaviour | offline | - | Zero wrong numeric merges; MAB-CR token check | **Not started** |  |
| O4-05 | Pattern observations (opt-in L4) | L1 L4 | K_C | O-15, M-13 | ER-28 | O4-04, F-16a | behaviour | offline | - | OP-Bench, PrefEval, MAB-CR non-inferior; sleep calls within budget | **Not started** | Q3 |
| O4-06 | Entity summaries and communities (opt-in) | L1 L3 L4 | K_C | O-16, O-17, M-19 | - | O4-03, F-16a | behaviour | offline | - | Global-question workload | **Not started** |  |
| O4-07 | Profile card, guarded operations, reflection (opt-in) | L1 L4 ctx | K_P | O-18, O-19, O-20, M-20 | ER-28 | O4-02, F-16a | behaviour | offline | - | OP-Bench and PrefEval up; hand audit at least 90% | **Not started** |  |
| O4-08 | Standing answers (opt-in) | L1 L4 | K_P | O-21 | ER-28 | O4-04, F-16a | behaviour | offline | - | Refresh on watermark; cited sources | **Not started** |  |
| O4-09 | Decay tiers, compression, multi-resolution text (K_C, opt-in) | L0 L1 ctx | K_C C | O-22, M-22, P-16 | - | O4-01b | behaviour | offline | - | Context tokens down at no QA loss | **Not started** |  |
| O4-10 | Prospective and procedural memory (advisory, opt-in) | L5 | K_C | O-23, O-24 | - | O4-01a | behaviour | offline | - | Off by default | **Not started** |  |
| O4-11 | Explicit feedback only; observer views (multi-agent) | L1 L4 L5 scope | K_C K_P R | O-25, O-26, M-24 | - | O4-03 | behaviour | offline | - | Multi-agent template tests | **Not started** |  |
| EX-4 | Phase 4 exit: OP-Bench and PrefEval up, no LoCoMo loss, sleep at most 0.2 calls per turn | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | - | O4-07, O4-05, O4-06, O4-09 | measure | exit | - | ROADMAP Phase 4 exit gate | **Not started** |  |

## Phase 5: trust, API, operations

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| T5-01 | Trust horizon on grants | scope | D R | G-07 | ER-29, POL-2 | F-16c, W2-04 | behaviour | trust | - | Read cost of the bound; N1 tests | **Not started** |  |
| T5-02 | Low-trust rendering in context | L0 L1 ctx | C | G-09 | ER-22 | R3-02 | behaviour | trust | - | Count wrapped blocks; QA non-inferior | **Not started** |  |
| T5-03 | Privacy tiers, consent and purpose scopes | L0 L1 L4 | D K_P R C | G-10, G-11 | ER-31 | EX-1a | behaviour | trust | - | Remote-tier gate tests | **Not started** |  |
| T5-04 | Erasure index across caches and projections (contract re-run per layer) | L0 L1 L3 L4 L5 ctx scope | D K_C | G-13, M-25 | ER-30, ER-31, POL-1 | F-16f | behaviour | trust | - | Forget probes on both profiles; every layer registered | **Not started** |  |
| T5-05 | Retention and audit per profile | L0 L1 L4 L5 ctx scope | D K_C R | G-14, G-15 | ER-27 | EX-1a | infra | trust | - | Profile tests | **Not started** |  |
| T5-06 | Observability: per-stage ledger, optional OTel, no content in spans | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-20 | - | W2-12 | infra | trust | - | Tests | **Not started** |  |
| T5-07 | N1 attack and utility run with enforcement on | L0 L1 scope | D R | G-07, G-06 | - | T5-01, W2-04 | measure | trust | - | Attack/utility frontier | **Not started** |  |
| EX-5 | Phase 5 exit: behavioural suite, N1, isolation and erasure on both profiles | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01 | - | T5-01, T5-02, T5-03, T5-04, T5-05, T5-06, T5-07 | measure | exit | - | ROADMAP Phase 5 exit gate | **Not started** |  |

## Phase 6: release and paper

| Id | Work package | Layers | Ops | Decisions | Review | Depends on | Kind | Agent lane | Owns | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R6-01 | Freeze configuration; sealed blind runs; loop metrics | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-05, G-26 | ER-02, ER-35, ER-36 | EX-1, EX-2, EX-3, EX-4, EX-5, T5-04, P0-02, P0-03, P0-08, MOD-01, X-01 | measure | release | - | One sealed blind run per set; loop metrics on scripted episodes; erasure contract re-run | **Not started** |  |
| R6-02 | Release memspine 2.0 with notes of removed APIs; retired arms cleaned | L0 L1 L4 L5 ctx scope | D K_C | G-23 | - | R6-01 | infra | release | - | Release checklist | **Not started** | No compatibility layer |

## Ownership

Files each package may write (glob patterns, repo-relative; includes removal and edit targets). Two packages that can run at the same time must not own overlapping files (checked by `build_plan.py`; allowed exceptions are listed in `ALLOWED_SHARED`).

| Id | Owns |
|---|---|
| P0-01 | `evals/eval_blind.py`<br>`evals/eval_focus.py`<br>`evals/eval_screen.py`<br>`evals/eval_paired.py`<br>`evals/memspine_evals/metrics.py`<br>`evals/memspine_evals/paired_gate.py`<br>`evals/memspine_evals/seal*.py`<br>`evals/tests/test_paired_gate.py`<br>`evals/tests/test_eval_gate_wiring.py`<br>`evals/tests/test_seal*.py`<br>`evals/memspine_evals/runner.py`<br>`evals/memspine_evals/results.py`<br>`evals/memspine_evals/cli.py`<br>`evals/tests/test_runner_identity.py`<br>`evals/tests/test_blind_validation.py`<br>`evals/README.md` |
| P0-02 | `evals/pinned_run.sh`<br>`evals/memspine_evals/provenance.py`<br>`evals/memspine_evals/paired_gate.py`<br>`evals/README.md` |
| P0-03 | `evals/pinned_run.sh`<br>`evals/memspine_evals/provenance.py`<br>`evals/memspine_evals/paired_gate.py`<br>`evals/README.md` |
| P0-04 | `.importlinter`<br>`tests/golden/**`<br>`tests/unit/test_import_contracts.py`<br>`docs/v2/data/engine_method_map*`<br>`scripts/engine_method_map.py` |
| P0-08 | `evals/suite/**`<br>`evals/memspine_evals/behavioural*.py`<br>`evals/tests/test_suite*.py`<br>`evals/tests/test_behavioural*.py` |
| P0-09 | `tests/unit/test_prompt_gold_lint.py`<br>`src/memspine/prompts/lint.py` |
| X-01 | `docs/v2/data/build_plan.py`<br>`docs/v2/data/packages.py`<br>`docs/v2/briefs/**` |
| X-02 | `.github/workflows/**`<br>`scripts/ci_*.py`<br>`tests/unit/test_flag_evidence.py`<br>`tests/unit/test_alembic_single_head.py`<br>`pyproject.toml`<br>`uv.lock`<br>`.importlinter`<br>`tests/unit/test_import_contracts.py` |
| X-03 | `tests/golden/**` |
| X-04 | `docs/adr/ADR-067-*`<br>`docs/v2/areas/*.md`<br>`docs/v2/pipelines/*.md` |
| X-05 | `evals/labelled/**`<br>`evals/tests/test_labelled_sets.py` |
| MOD-01 | `src/memspine/schemas/**`<br>`docs/v2/schemas/durable/**`<br>`docs/v2/schemas/public/**`<br>`docs/v2/schemas/internal/**`<br>`tests/unit/schemas/**` |
| X-06 | `docs/v2/data/reranker_licences_*.md` |
| F-05 | `src/memspine/store/records.py`<br>`src/memspine/store/events.py`<br>`src/memspine/store/log.py`<br>`src/memspine/store/scope.py`<br>`src/memspine/store/lineage.py`<br>`src/memspine/trust/**`<br>`src/memspine/core/records.py`<br>`src/memspine/core/events.py`<br>`src/memspine/core/replay.py`<br>`src/memspine/core/projector.py`<br>`src/memspine/core/namespace.py`<br>`src/memspine/core/visibility.py`<br>`src/memspine/core/firewall.py`<br>`src/memspine/core/audit.py`<br>`src/memspine/core/erasure.py`<br>`src/memspine/core/privacy.py`<br>`src/memspine/core/redaction.py`<br>`src/memspine/store/{replay,visibility}.py`<br>`src/memspine/store/projections/**`<br>`src/memspine/store/__init__.py`<br>`scripts/codemod_move.py`<br>`scripts/moves/F-05.toml`<br>`src/memspine/{ingest,derive,read,compose,models,runtime,api,memory}/__init__.py`<br>`tests/golden/harness.py` |
| F-06 | `src/memspine/read/**`<br>`src/memspine/compose/**`<br>`src/memspine/models/**`<br>`src/memspine/core/query_*`<br>`src/memspine/core/temporal_*`<br>`src/memspine/core/perspective.py`<br>`src/memspine/core/lead.py`<br>`src/memspine/core/policies/assembly.py`<br>`src/memspine/core/policies/compression.py`<br>`src/memspine/core/answer*`<br>`src/memspine/services/llm*`<br>`src/memspine/services/embedding*`<br>`src/memspine/services/rerank*`<br>`src/memspine/services/query_encoder*`<br>`src/memspine/services/decision*`<br>`src/memspine/services/cache*`<br>`src/memspine/services/llm/**`<br>`src/memspine/services/embedding/**`<br>`src/memspine/services/rerank/**`<br>`src/memspine/services/query_encoder/**`<br>`src/memspine/services/decision/**`<br>`src/memspine/services/cache/**`<br>`scripts/codemod_move.py`<br>`scripts/moves/F-06.toml`<br>`tests/golden/harness.py` |
| MOD-02 | `src/memspine/*/registry.py` |
| F-07 | `src/memspine/config/schema/**`<br>`src/memspine/config/flags.py`<br>`src/memspine/config/schema.py` |
| F-08 | `src/memspine/runtime/container.py`<br>`src/memspine/engine.py` |
| F-09 | `src/memspine/ingest/**`<br>`src/memspine/engine.py` |
| F-10a | `src/memspine/read/**`<br>`src/memspine/engine.py` |
| F-10b | `src/memspine/read/**`<br>`src/memspine/engine.py` |
| F-11 | `src/memspine/compose/**`<br>`src/memspine/engine.py` |
| F-12 | `src/memspine/trust/**`<br>`src/memspine/memory/**`<br>`src/memspine/engine.py` |
| F-13a | `src/memspine/api/**`<br>`src/memspine/protocols/**`<br>`src/memspine/engine.py`<br>`evals/memspine_evals/systems/memspine_system.py` |
| F-13b | `src/memspine/api/**`<br>`src/memspine/protocols/**`<br>`src/memspine/engine.py`<br>`evals/memspine_evals/systems/memspine_system.py` |
| F-13c | `src/memspine/api/**`<br>`src/memspine/protocols/**`<br>`src/memspine/engine.py`<br>`evals/memspine_evals/systems/memspine_system.py` |
| F-15 | `src/memspine/derive/stages/**`<br>`src/memspine/workers/pipelines.py` |
| F-01 | `src/memspine/store/keys.py`<br>`src/memspine/derive/graph.py` |
| F-02a | `src/memspine/store/envelope.py`<br>`src/memspine/store/layers.py`<br>`src/memspine/derive/graph.py`<br>`src/memspine/store/records.py`<br>`src/memspine/store/backends/**/alembic/versions/*` |
| F-02b | `src/memspine/store/envelope.py`<br>`src/memspine/store/layers.py`<br>`src/memspine/derive/graph.py`<br>`src/memspine/store/records.py`<br>`src/memspine/store/backends/**/alembic/versions/*` |
| F-02c | `src/memspine/store/envelope.py`<br>`src/memspine/store/layers.py`<br>`src/memspine/derive/graph.py`<br>`src/memspine/store/records.py`<br>`src/memspine/store/backends/**/alembic/versions/*` |
| F-04 | `src/memspine/runtime/**`<br>`src/memspine/ingest/commit.py`<br>`src/memspine/workers/inline.py`<br>`src/memspine/workers/runner.py`<br>`src/memspine/workers/scheduler.py`<br>`src/memspine/workers/schedule.py`<br>`src/memspine/workers/dbos_runner.py`<br>`src/memspine/workers/taskiq_runner.py` |
| RT-01 | `src/memspine/runtime/**`<br>`src/memspine/ingest/commit.py`<br>`src/memspine/workers/inline.py`<br>`src/memspine/workers/runner.py`<br>`src/memspine/workers/scheduler.py`<br>`src/memspine/workers/schedule.py`<br>`src/memspine/workers/dbos_runner.py`<br>`src/memspine/workers/taskiq_runner.py` |
| RT-02 | `src/memspine/runtime/**`<br>`src/memspine/ingest/commit.py`<br>`src/memspine/workers/inline.py`<br>`src/memspine/workers/runner.py`<br>`src/memspine/workers/scheduler.py`<br>`src/memspine/workers/schedule.py`<br>`src/memspine/workers/dbos_runner.py`<br>`src/memspine/workers/taskiq_runner.py` |
| F-16a | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |
| F-16b | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |
| F-16c | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |
| F-16d | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |
| F-16e | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |
| F-16f | `src/memspine/store/lineage.py`<br>`src/memspine/store/migrate.py`<br>`src/memspine/store/profiles.py`<br>`src/memspine/store/backends/postgres/**`<br>`src/memspine/store/backends/neo4j/**`<br>`src/memspine/derive/write.py`<br>`src/memspine/services/graph/neo4j.py` |

No ownership yet (cannot be briefed): 70 packages.
