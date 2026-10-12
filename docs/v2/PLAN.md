# MemSpine restructure plan: work packages and status

Generated from `data/build_plan.py` (the source of truth for rows); status is also kept on the live tracker page and synced back here.
74 packages cover all 155 decisions. Kinds: **move** = pure restructure (tests and CPU golden), **behaviour** = needs a GPU screen through the paired gate, **measure** = a run, **infra** = tooling.

Layers follow the decided stack (Q1): L0 raw, L1 facts, L2 episodes and summaries, L3 graph, L4 observations and profile, L5 standing answers, procedures and plans; ctx = working context. Ops are the loop operators (D deposit, K_C consolidate, K_P personalise, R retrieve, C compose, G generate). Each package is one task for one agent (Sonnet); packages in a `serial` lane run one at a time, different lanes may run in parallel once their dependencies are Done.

| Status | Count |
|---|---|
| Not started | 62 |
| Ready | 4 |
| In progress | 3 |
| Blocked | 2 |
| Needs measurement | 2 |
| Done | 1 |
| Dropped | 0 |

## Phase 0: settle and prepare

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| P0-01 | Paired adoption gate (focus, blind, full set) | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-01, G-03 | - | infra | infra | Replay: identical arm NO CHANGE; missing set INCOMPLETE | **In progress** | Built 2026-10-12 (d0ab058, 5bf5643); open: sealed one-shot blind run per adoption |
| P0-02 | Environment pinning per run | - | R G | G-24 | - | infra | infra | A fresh per-run venv reproduces clean-jina-full | **In progress** | Lock fixed (1d15194); per-run freeze recorded (7ce1d55); open: separate venv per pinned run |
| P0-03 | Run manifest and comparability key | - | D R C G | G-25 | P0-02 | infra | infra | Manifest records commit, env hash, flags, prompts, models, data hashes | **Not started** |  |
| P0-04 | CPU golden and import-boundary test (restructure guard) | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | - | infra | infra | Golden identical on two runs at HEAD | **Ready** | S0 of RESTRUCTURE |
| P0-05 | Engine LLM fixes: hard per-call bound, think=False on Ollama | L1 L4 | G | P-15 | - | behaviour | infra | Regression tests; re-screens below | **Done** | 2d05ac7, 798c4b5 |
| P0-06 | Re-screen engine-LLM arms on the thinking fix (f-gc9, f-gc3, f-win, f-gc2) | L0 L1 L3 ctx | D K_C K_P R | O-07, O-16, W-12 | P0-05 | measure | infra | Paired gate vs f-jref2-i2 on the focus slice | **Needs measurement** | GPU stopped by the owner; f-gc9 was net +15 before the fix |
| P0-07 | A03 query contract: full set, then blind sets | ctx | R | R-03 | P0-01 | measure | infra | PROMOTE on 1,540 vs clean-jina-full, then CONFIRMS on MAB-CR and ConvoMem | **Needs measurement** | Focus +15/-7 net +8 HOLD; full run stopped partway |
| P0-08 | Benchmark suite and behavioural controls | L0 L1 L4 L5 ctx scope | K_P R C G | G-02, G-04 | P0-01 | infra | infra | Dev: LoCoMo (+cat 5), OP-Bench; blind: MAB-CR, ConvoMem, PrefEval; behavioural suite blocks adoption | **Not started** | LongMemEval stays out |
| P0-09 | Prompt gold-lint over single words, partials and harness prompts | L1 L3 | D | P-13, W-18 | - | infra | infra | Lint fails on any gold token in any prompt | **In progress** | Single words and harness prompts done (11853ed); open: partials glob, rendered-prompt warnings |

## Phase 1: foundations (restructure in place)

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| F-01 | Graph key identity: kind, occurrence, A-B-A, old-key aliases | L0 L1 L3 | D | M-17, W-20 | P0-04 | behaviour | foundations | A-B-A returns to A; same-day events stay two; replay idempotent; GC arm screen | **Ready** | S1; G-W50 |
| F-02 | Record envelope: layer field, type tag, tri-temporal times, provenance class, axes | L0 L1 L3 L4 L5 ctx scope | D K_C R | M-02, M-03, M-06, M-07, M-08, M-10, M-16 | P0-04 | move | foundations | Replay byte-stable; upcaster round-trips; golden unchanged | **Ready** | S2; Q1 two layers on |
| F-03 | Derivation fingerprint on derived records and caches | - | K_C | P-12, P-25 | F-02 | move | serial: engine split | Cache misses once; outputs identical | **Not started** | S3 |
| F-04 | Durable outbox and inline runner, read barrier | L0 L1 L3 ctx | D K_C | W-02, W-10, W-13, W-25, G-19, P-21 | F-02 | behaviour | serial: engine split | Fault-injection tests; golden unchanged with barrier; window arm screen | **Not started** | S4; Q2; model calls outside namespace locks |
| F-05 | Package skeleton; move store and trust leaves | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | P0-04 | move | serial: engine split | Both suites and golden green; no old import paths | **Not started** | S5 |
| F-06 | Move read, compose and models leaves | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | F-05 | move | serial: engine split | Same as F-05 | **Not started** | S6 |
| F-07 | Config split and flag registry | L0 L1 L4 L5 ctx scope | D R C | G-21, G-22 | F-05 | move | serial: engine split | Same YAML keys; every default flag has evidence | **Not started** | S7 |
| F-08 | Wiring out of the engine (Services container) | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16 | F-06 | move | serial: engine split | Golden | **Not started** | S8 |
| F-09 | Write path out of the engine | L0 L1 L5 | D | W-01, W-09 | F-08 | move | serial: engine split | Golden | **Not started** | S9 |
| F-10 | Read path out of the engine | L0 L1 L3 | R | R-05 | F-08 | move | serial: engine split | Golden byte-identical hit order | **Not started** | S10 |
| F-11 | Compose out of the engine | ctx | C | R-20 | F-10 | move | serial: engine split | Golden context hash identical | **Not started** | S11 |
| F-12 | Trust, admin and memory-type mechanisms out | L4 ctx scope | K_C R C | M-21, M-24 | F-08 | move | serial: engine split | Golden | **Not started** | S12 |
| F-13 | Scoped handle API; engine reduced to lifecycle and admin | L0 L1 L4 L5 ctx scope | D K_C K_P R C | G-16, G-17, G-18 | F-09, F-10, F-11, F-12 | move | serial: engine split | Harness on the handle; paired-gate replay NO CHANGE vs f-jref2-i2 | **Not started** | S13; no compatibility aliases |
| F-14 | Retire measured-negative and dead code | L0 L1 L4 L5 ctx scope | D R C | G-22 | F-13 | move | serial: engine split | Golden unchanged; retired flags raise ConfigError | **Blocked** | Needs owner on unscreened candidates (event ledger, maxsim, causal walk, bridge hop, prefilter, novelty exclusions, agentic query) |
| F-15 | Split the pipelines module into derive stages | L0 L1 L3 L4 L5 ctx | K_C K_P | O-04 | F-08 | move | serial: engine split | Golden; stage order unchanged | **Not started** | S15 part 1 |
| F-16 | Storage profiles, lineage table, Neo4j adapter, migration by replay | L0 L1 L3 L4 L5 ctx scope | D K_C R | M-01, M-04, M-05, M-09, M-18, M-25, M-26, G-12, G-23 | F-02, F-05 | behaviour | serial: engine split | Isolation, erasure, rebuild tests on embedded and server profiles; v1 log replays byte-stably | **Not started** | S16; Q4 Neo4j |

## Phase 2: write path and models

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| W2-01 | Source envelope intake, receipt, idempotency, re-ingest | L0 L1 | D K_C | W-01, W-02, W-03, W-24 | F-04, F-09 | behaviour | after F-13 (W2) | Write p95 at most 250 ms; idempotent retries | **Not started** |  |
| W2-02 | Stamping: three clocks, time zone, speaker/addressee/participants, provenance class | L0 L1 L4 scope | D R | W-04, W-05, W-08, M-08 | F-02, W2-01 | behaviour | after F-13 (W2) | Focus slice non-inferior | **Not started** |  |
| W2-03 | Role policy, echo guard and routing | L0 L1 L5 ctx | D | W-06, W-09 | W2-01 | behaviour | after F-13 (W2) | Focus slice non-inferior | **Not started** |  |
| W2-04 | Firewall at three gates; base plus extended signals | L0 L1 scope | D | W-07, G-06 | W2-01 | behaviour | after F-13 (W2) | False-positive screen on the behavioural suite | **Not started** | Q8 chat-preset exemption |
| W2-05 | Segmentation: adaptive window, time-gap cut, token batching | L0 | D | W-11 | F-04 | behaviour | write | Calls per turn down, grounding inside band | **Not started** |  |
| W2-06 | Online extraction call: ItemV1, prompt v2, constrained output, repair, dead letters | L0 L1 L3 | D R | W-12, W-14, W-15, W-16, P-04, P-05, P-06, P-09, P-11 | F-04, W2-05 | behaviour | after F-13 (W2) | Window arm vs reference; items dropped down; at most 1 call per window | **Not started** | G-W49 |
| W2-07 | Prompt layout rules: no examples, stable prefix, same language | L0 L1 L3 ctx | D C | W-18, P-07, P-08, P-10 | P0-09 | behaviour | write | English output unchanged on focus slice (per partial) | **Not started** |  |
| W2-08 | Grounding: spans per assertion, rule checks, assent | L1 L3 | D | W-17 | W2-06 | behaviour | write | Grounding at least 95% on a labelled cross-domain set | **Not started** |  |
| W2-09 | Online entity resolution with pending_merge | L1 L3 | D K_C | W-19, M-15 | W2-06, F-01 | behaviour | write | Calibrated thresholds: no homonym merges above a stated rate | **Not started** |  |
| W2-10 | Integration ladder and cardinality registry | L1 L3 | D K_C | W-21, W-22, M-12 | F-01, W2-06 | behaviour | write | MAB-CR non-inferior; A-B-A tests | **Not started** | Q7 coexist unless marked |
| W2-11 | Asset describer (ingest and read triggers, off by default) | L0 | D R | W-23 | W2-01 | behaviour | after F-13 (W2) | Off by default; on-arm screen | **Not started** |  |
| W2-12 | Cost ledger and write targets | L0 L1 L3 L4 L5 | D K_C K_P | W-26, P-14, P-22 | F-03 | infra | after F-13 (W2) | Targets as regression tests | **Not started** |  |
| W2-13 | Small local pre-pass (GLiNER2) as hints | L1 L3 | D | P-23 | W2-06 | behaviour | write | Calls down, grounded facts not down | **Not started** |  |
| MD-01 | Model tiers, placement, no-model fallbacks, device plan, thinking per role | L0 L1 L4 | D K_C R G | P-01, P-02, P-03, P-15, P-19 | F-06 | behaviour | models | Worker-tier screen per role | **Not started** |  |
| MD-02 | Default embedders (CPU multilingual, GPU profile) | L0 L1 | R | P-17, M-23 | F-06 | behaviour | models | English R@k within band; non-English probe | **Not started** |  |

## Phase 3: read path

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| R3-01 | Depth tiers and ReadRequest; evidence unit | L0 L1 L4 ctx | R C | R-01, R-02 | F-10 | behaviour | after F-13 (R3) | Fast tier zero model calls; p95 SLO | **Not started** |  |
| R3-02 | Typed query contract by rules; render header | ctx | R C | R-03, R-20 | P0-07 | behaviour | after F-13 (R3) | A03 full set and blind | **Blocked** | Waits for P0-07 |
| R3-03 | Rule router; LLM rewrites only in deep tier | ctx | R | R-04 | R3-01 | behaviour | after F-13 (R3) | Focus non-inferior | **Not started** |  |
| R3-04 | Core legs: vector, language-aware BM25, temporal on event intervals | L0 L1 L3 L5 | R | R-05, R-06, R-07 | F-10, F-02 | behaviour | after F-13 (R3) | Focus non-inferior; temporal category up | **Not started** |  |
| R3-05 | Routed legs: entity anti-hub, graph one hop, episode lift | L0 L1 L3 L4 | R | R-08, R-09, R-10 | R3-04 | behaviour | after F-13 (R3) | Each leg its own focus and blind screen | **Not started** | Q1: legs run only when their layer exists |
| R3-06 | Family fusion, protected pool, calibrated floors | L0 L1 | R | R-11, R-12, R-13 | R3-04 | behaviour | after F-13 (R3) | Source-family pool non-inferior to per-leg pool | **Not started** |  |
| R3-07 | Jina rerank on every read; licence fallback; bounded boosts | L0 L1 L4 ctx | R | R-14, R-15, R-16, P-18 | R3-06 | behaviour | after F-13 (R3) | Matches the 81.4% reference; boosts off until screened | **Not started** | Q5, Q6 |
| R3-08 | Lift derived hits to turns under one score | L0 L1 L3 L4 | R | R-17 | R3-07 | behaviour | after F-13 (R3) | Focus non-inferior | **Not started** |  |
| R3-09 | Packer early-break fix; block shares | ctx | C | R-18 | F-11 | behaviour | after F-13 (R3) | Fix: tests only; shares: equal-budget screen | **Ready** | Early-break fix needs no screen |
| R3-10 | Neighbour windows after rerank | L0 | C | R-19 | R3-07 | behaviour | after F-13 (R3) | Focus non-inferior | **Not started** |  |
| R3-11 | Store-calibrated relevance gate with named bypass | L1 ctx | R | R-21 | R3-07 | behaviour | after F-13 (R3) | OP-Bench up; no recall loss on MAB-CR, ConvoMem, LoCoMo | **Not started** |  |
| R3-12 | Deep tier: one sufficiency check and one rewrite | L0 ctx | R C | R-22 | R3-03 | behaviour | after F-13 (R3) | f-suff: net beyond band, no category loss, at most 2x wall time | **Not started** |  |
| R3-13 | External public knowledge (opt-in, separate block) | L0 | R | R-23 | R3-01 | behaviour | after F-13 (R3) | Separate arm; never mixed in headline | **Not started** |  |
| R3-14 | Time view and two-axis as-of reads | L0 L1 | R | R-24, M-11 | F-02, R3-04 | behaviour | after F-13 (R3) | Temporal and MAB-CR non-inferior | **Not started** |  |
| R3-15 | Parallel legs, latency SLOs, caching, prefix layout | L0 L1 ctx | D R C | R-25, R-26, P-20, P-24 | R3-06 | infra | after F-13 (R3) | p95 SLO tests in CI | **Not started** |  |

## Phase 4: offline memory

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| O4-01 | Scheduler, triggers, namespace jobs, leases, watermarks, budget, freshness | L0 L1 L3 L4 L5 ctx | K_C K_P R | O-01, O-02, O-03, O-05, O-06 | F-04, F-15 | behaviour | after F-13 (O4) | Restart and lock fault tests; evals deterministic (off by default) | **Not started** |  |
| O4-02 | Call 2 at session close: summary, stitched episode, profile ops | L0 L1 ctx | K_C K_P | O-07, O-08, O-09, M-14 | O4-01 | behaviour | after F-13 (O4) | G-C9-style screen | **Not started** | f-gc9 summaries +15 before the thinking fix |
| O4-03 | Reconcile backlog, staged entity resolution, contradictions | L1 L3 | K_C | O-10, O-11, O-12 | O4-01, W2-09 | behaviour | offline | Contradiction-recall set; MAB-CR non-inferior | **Not started** |  |
| O4-04 | Near-duplicate merge and value histories | L1 L4 | K_C | O-13, O-14, M-13 | O4-03 | behaviour | offline | Zero wrong numeric merges; MAB-CR token check | **Not started** |  |
| O4-05 | Pattern observations (opt-in L4) | L1 L4 | K_C | O-15, M-13 | O4-04 | behaviour | offline | OP-Bench, PrefEval, MAB-CR non-inferior; sleep calls within budget | **Not started** | Q3 |
| O4-06 | Entity summaries and communities (opt-in) | L1 L3 L4 | K_C | O-16, O-17, M-19 | O4-03 | behaviour | offline | Global-question workload | **Not started** |  |
| O4-07 | Profile card, guarded operations, reflection | L1 L4 ctx | K_P | O-18, O-19, O-20, M-20 | O4-02 | behaviour | after F-13 (O4) | OP-Bench and PrefEval up; hand audit at least 90% | **Not started** |  |
| O4-08 | Standing answers (opt-in) | L1 L4 | K_P | O-21 | O4-04 | behaviour | offline | Refresh on watermark; cited sources | **Not started** |  |
| O4-09 | Decay tiers, compression, multi-resolution text | L0 L1 ctx | K_C C | O-22, M-22, P-16 | O4-01 | behaviour | offline | Context tokens down at no QA loss | **Not started** |  |
| O4-10 | Prospective and procedural memory | L5 | K_C | O-23, O-24 | O4-01 | behaviour | after F-13 (O4) | Advisory only; off by default | **Not started** |  |
| O4-11 | Explicit feedback only; observer views (multi-agent) | L1 L4 L5 scope | K_C K_P R | O-25, O-26, M-24 | O4-03 | behaviour | after F-13 (O4) | Multi-agent template tests | **Not started** |  |

## Phase 5: trust, API, operations

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| T5-01 | Trust horizon on grants; lineage always recorded | L1 L4 scope | D K_C R | G-07, G-08 | F-16 | behaviour | trust | Read cost of the bound; N1 tests | **Not started** |  |
| T5-02 | Low-trust rendering in context | L0 L1 ctx | C | G-09 | F-11 | behaviour | trust | Count wrapped blocks; QA non-inferior | **Not started** |  |
| T5-03 | Privacy tiers, consent and purpose scopes | L0 L1 L4 | D K_P R C | G-10, G-11 | F-12 | behaviour | trust | Remote-tier gate tests | **Not started** |  |
| T5-04 | Erasure index across caches and projections | L0 L1 L3 L4 L5 ctx scope | D K_C | G-13, M-25 | F-16 | behaviour | trust | Forget probes on both profiles | **Not started** |  |
| T5-05 | Retention and audit per profile | L0 L1 L4 L5 ctx scope | D K_C R | G-14, G-15 | F-12 | infra | trust | Profile tests | **Not started** |  |
| T5-06 | Observability: per-stage ledger, optional OTel, no content in spans | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-20 | W2-12 | infra | trust | Tests | **Not started** |  |

## Phase 6: release and paper

| Id | Work package | Layers | Ops | Decisions | Depends on | Kind | Agent lane | Gate | Status | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| R6-01 | Freeze configuration; sealed blind runs; loop metrics | L0 L1 L4 L5 ctx scope | D K_C K_P R C G | G-05, G-26 | R3-07, O4-07 | measure | release | One sealed blind run per set; loop metrics on scripted episodes | **Not started** |  |
| R6-02 | Release memspine 2.0 with notes of removed APIs | L0 L1 L4 L5 ctx scope | D K_C | G-23 | R6-01 | infra | after F-13 (R6) | Release checklist | **Not started** | No compatibility layer |
