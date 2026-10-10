# Gap-to-solution plan (2026-10-11)

One authoritative plan. It merges: the gap register (`evals/analysis/GAP_REGISTER.md`, sections A-J, stable failures, R2, progress log, new section K), the Codex deep audit of 2026-10-11 (25 solution families M01..E06, crosswalk of the 37 original gaps, layered hypotheses, all-scope coverage; local data under `evals/runs/_analysis/deep_audit_2026-10-11/`, not committed, only ids, counts and solution text are used here), the full-run forensics (`evals/analysis/FULL_RUN_FORENSICS_2026-10-10.md`), `docs/PERSPECTIVE_COMPARISON.md`, `docs/FRAMEWORK_TOOLCALL_SURVEY.md`, `docs/DEFERRED_INGEST_STUDY.md` and every screen result recorded so far.

User goal: a complete gap-to-solution plan, built for any memory workload, not for this scenario. Every row below therefore states the data shape the solution needs and why it is not LoCoMo or OP-Bench specific.

Conventions. Numbers are questions out of the 1,540 LoCoMo cat 1-4 questions unless a benchmark is named. "Estimate" always means an assumed capture rate on a measured class size, never a measurement. No dataset text is quoted anywhere in this file (OP-Bench has no licence; LoCoMo text stays local).

## Decisions taken (user, 2026-10-10 and 2026-10-11)

- Scope of runs: LoCoMo cat 1-4 (1,540 q, all 10 conversations) + OP-Bench (859 probes, 10 personas). Cat 5 is out of the headline; it may still run as an optional guard slice for anything that can over-abstain. LongMemEval is excluded.
- The held-out set is exposed (user decision, 2026-10-10 20:00): all 10 LoCoMo conversations and all OP-Bench personas are now one development pool.
- Fresh blind check (user, 2026-10-11): **MemoryAgentBench Conflict_Resolution + ConvoMem**. Blind validation only, never used for tuning. Wiring is in progress (other agents).
- **E03 (public knowledge) and E04 (dataset images + OCR/vision) are APPROVED** behind opt-in flags (section 5, wave 3). E03 searches generic public terms only, never private conversation text; external evidence is cited separately and never proves a private fact. E04 downloads only the dataset's own referenced image URLs, once, caches by content hash, and fails explicitly when the asset is unavailable.
- On hold (user): I70 framework adapters, I71 deferred ingest. A1 second judge is deferred until after the fixes (final-claims gate, wave 4).

## 1. Status snapshot

### 1.1 Baselines (full runs, logged, `--trace-full`)

| Run | Config | Result |
|---|---|---|
| `full-persp-loc` (22b97b9) | BEST + perspective `subject_weight` | LoCoMo 1,540: **80.4%** (1,238 correct); single 88.7, multi 66.3, temporal 81.0, open 46.9; dev 82.7 / held-out 79.0 |
| `full-persp-opb` | same, no relevance gate | OP-Bench 859: **23.5** (irrelevant 14.1, baiting 8.2, syc fact/value/memory 76.4/74.9/20.1, repetition 23.9; dev 20.6, held-out 25.5); injection rate 1.0 |
| dev screen r3d (304 LoCoMo q + OP-Bench dev 331) | relevance gate (decider) + named bypass (I74) | OP-Bench dev **43.2** vs 21.2 (+22.0); LoCoMo 80.3 vs 81.6 (-4 net, inside band). Our no-memory BASE is 62.8 (I34) |
| `r7-protect-full` (round 7, arm 1) | leg-protected pool, `read.pool_protect_per_leg: 3` (I75a) | LoCoMo 1,540: **81.4 vs 80.4 (+15 net, noise band +-12.9, every category up)** at about 30% more rerank cost. The OP-Bench half is pending |

Reader ceiling (forensics): with every gold turn in the context (1,301 q) accuracy is **87.1%** (88.4% without the 23 errata rows, 89.5% if the 10-11 judge errors were also credited). With incomplete evidence (235 q) accuracy is 43.4%. Perfect retrieval alone therefore gives about 87; 90 needs the reader/judge ceiling to rise too (section 6).

Stage table of the 302 wrong answers (80.4% run): recall 69, cuts 61 (56 at the fused pool, 5 at `rerank_keep`), reader 138 (detail 47, list 22, count 8, date/duration 25, refusal 11, inference 23, distractor 1, speaker 1), judge 11, errata 23, write path 0.

OP-Bench against our own no-memory BASE (62.8): baseline 21.2 on dev = -66%; with gate + bypass 43.2 = -31%.

### 1.2 Every screen verdict so far

| Screen / change | Verdict |
|---|---|
| B1 list mode + fixed reranker (BEST) | ADOPTED: 84.1 vs 82.9 on 584 dev q (+12/-5) |
| B9 `rerank_floor: skip`, pool 2 | in BEST (84.5 vs 84.1, evidence coverage up) |
| B4 `lexical_dates` | rejected (-3.1 temporal); `temporal_relative` neutral |
| B5 `lexical_strip_names` | rejected (+3/-6); B6 english analyzer neutral |
| B7 wide pool + tier, balanced | not adopted (churn +30/-23, trades single for multi-hop) |
| B8 `session_leg` | neutral, not adopted |
| B15 word vectors in place of BM25 | negative (-12 net, 77.6 vs 81.6) |
| C1/C2/C5 prompt and annotator variants, C3 yes/no rule, C8 `grounded_v3` | neutral or rejected: prompt-only reader fixes are exhausted on this reader |
| A9 thinking mode | rejected (63.2 vs 88.8; 43 answers hit the reasoning budget) |
| R2-1 bridge hop / R2-1b gated | not adopted (+5/-7; gated +1/-2 out of sample); R2-2 inert, R2-3 neutral, R2-4 rejected |
| I1 neutral refusal retry | adopted as default; fired 22 on the full run, accepted on none |
| I3 `grounded_generic` (r3-generic) | REJECT (77.6 vs 81.6, open-domain 15.4 vs 61.5); `routed_generic` (r3c) never run |
| I4 intent list trigger (r3-intent) | NEUTRAL, safe on both (81.9 vs 81.6; OP-Bench +0.2); kept as generality candidate |
| I10 query instruction | neutral on OP-Bench, not adopted |
| I17 latest-wins annotate (r3-latest) | NEUTRAL, not adopted (value sycophancy -13.7 on OP-Bench) |
| I29 relevance gate, decider alone (r3-relgate) | REJECT as built (OP-Bench +22.7, LoCoMo -22.4: 119/304 contexts emptied) |
| I29+I74 gate + named bypass (r3d) | ADOPT-CANDIDATE (+10.35 macro); confound: `decider_tasks` default let the decider veto list mode; clean arm r3e not run (queue stopped 2026-10-10 20:00) |
| I31 dedupe (r3-dedupe) | INERT on both (byte-identical contexts) |
| I32 no-record hint (r3-norecord) | NEUTRAL, safe (memory sycophancy +2.0); detector fired on 6/80 probes |
| I39 perspective, `subject_weight` (r3-persp) | NEUTRAL by rule (+0.52 macro); multi-hop +11.6 on 304 q; full run 80.4 (+4 net vs eq06-fix, single-hop -18, multi +10, temporal +14) |
| I75a leg-protected pool (round 7 arm 1) | +15 net on 1,540, every category up (see 1.1); OP-Bench half pending; adopt-candidate once OP-Bench shows no loss |
| Offline only (no GPU screen yet) | I58 judge conventions: dev baseline 79.7 -> 80.2 (+4 credits); I76 duration solver 3 fixed / 0 broken; I79 annotator reaches 1 of 61 temporal failures; I19 leakage probe: no leak in 12 configs |
| Built, never screened | I56, I57, I59, I60, I63, I64, I67, I48, I52, I53, I75b, I76, I79, I9, I6, I20 |

### 1.3 What the exposed held-out set means

Every number above is a development number. Dev-vs-held-out differences (82.7 vs 79.0) are no longer evidence of generalisation. A claim ("this change generalises") needs the fresh blind check of section 3 (MAB Conflict_Resolution + ConvoMem). Until it has run, every adopted change is reported as "adopted on the development pool".

## 2. Layered plan L0-L6

Layers follow the Codex audit (L0 Measurement, L1 Query/answer contract, L2 Atomic evidence, L3 Retrieval/selection, L4 Tool loop, L5 Synthesis/verification) plus **L6 Serving / engine ops**. Codex's L6 answer verification, L7 personalisation control and L8 validation are folded in: G01/P03/E06 sit in L5, P01/P02 in L3 (they decide what memory is selected for the reader), V01/V02 in L0.

Legend for the columns.
- **Validation** = S (matched isolated screen: the one change vs the frozen reference, same stack, on ALL in-scope slices: LoCoMo 1,540 and OP-Bench 859, paired gains/losses per slice) then X (2x2 interaction with the named partner) then F (fresh blind check, section 3). Where a row names only S, X and F follow the protocol default (3.5).
- **Decision** = rule (deterministic) / decider (OpenDecider port, calibrated probability, fixed 0.5 cut) / calibrated gate (per-store self-calibration, I37) / LLM step / code.
- **Status** = built-opt-in / screened-verdict / not-built / on-hold (plus "in progress" where another agent is working on it now, and "done" for closed infrastructure).
- **Cost** = extra cost per query over the frozen reference (model calls, GPU/CPU time).
- Gain = estimate, in questions of 1,540 (LoCoMo) or OP-Bench points; class sizes are the Codex rule-based links (upper bounds, not causal) or the forensics classes. Gains do not add (section 6).
- Codex ids in brackets, our ids first. Crosswalk in 2.8.

### 2.1 L0 Measurement and evidence validity

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **M01** [I27] | Explorer reads provisional repetition scores; positive score can coexist with a truncated answer. 863 linked q, 755 baseline failures are OP-Bench-side | Use finalised per_probe scores; keep provisional values and their basis; separate retrieval-only, missing, pending, failed states; official macro apart from pass@0.5 | Any benchmark with provisional vs final scoring or capped generations needs explicit completion states | code | **in progress** (other agent) | none (reporting) | 0 | rebuild from unchanged files: official macro identical, provisional/final differences visible | any official-score change caused by display repair | 0 (measurement) |
| **M02** [I62, I14, C3, I58, I61, I77] | 8 raw evidence references malformed (e.g. 42:3-58, 42:3-88, 43:4-18, 47:6-38, 49:8-31/38/46, 50:9-69); labels conflict with dates/speakers; 76 linked q. Errata today: 47 + 5 (I62) | Reversible evidence-normalisation view: raw id, resolved id, error, adjudication status, exact source span; official labels immutable; report official score and a versioned adjudicated diagnostic | Per-benchmark errata files via `memspine_evals/errata.py` (`errata/v1`); needs a stable turn-id scheme per loader | code + hand review | built-opt-in (errata tool, I62 entries); normalisation view not-built | `--errata`, `--errata-extended` | 0 | every repaired ref resolves inside the same conversation; independent review of each adjudication | repairs silently rewrite labels, merge people, or excuse unsupported additions | 0 (denominator hygiene; headline 81.4% excluding errata on the old run) |
| **V01** [A7, A11, A16, I15, I16, I36, I37] | Aggregates hide regressions; flips 19% on a 233-q set (A16); subset/partial runs not interchangeable | Frozen baseline + run manifests; paired gains/losses per category and conversation; one change at a time then chosen 2x2; predeclared tolerances for quality, support, latency, tokens; cluster bootstrap; behavioural (metamorphic) control suite; fresh blind data | Dataset-independent harness; needs a split file (`make_split.py`) and a declared `DataShape` per loader | code + protocol | built-opt-in (CIs, splits, noise estimator); **MAB/ConvoMem wiring in progress** | `evals/run.sh`, `noise_floor.py`, `make_split.py` | 0 GPU for tooling; 2 repeat runs of the reference per slice for the noise floor | see section 3 | promotion from partial runs, best-per-question selection, exposed holdouts | 0 (enabler) |
| **V02** [I78, I73, H8, I24, I30, I39, I75] | Some gate decisions, bypasses, replay expansion and per-leg floors never logged; 56 fusion cuts had no record before I78 | Log query contract, route, gate reason, per-leg cap and ranks, pool/rerank cuts, expansion provenance, prompt version, token budget, retries, score basis; mark each field observed / reconstructed / absent | Logging is data-independent; text redacted or kept local per dataset licence | code | built-opt-in (I78 pool_cut, I73 timers, H8 explorer); per-leg floors and query-contract fields not-built | `--trace-full`, `observability.write_timers` | trace storage only | replay manifest without model calls reconciles every context id and token count | missing/capped logs presented as proof of a mechanism | 0 (enabler) |
| I77 | 7 of 11 judge errors uncredited by I58 (containment 2, relative-year 1, range 2, synonym 1, list under non-list gold 1) | `contains_single`, `date_range`, `relative_year` conventions; synonyms only on a disputed row via second judge | String-level rules, no dataset vocabulary; second column only, LLM verdict never changed | rule | not-built | `--judge-conventions` (extend) | 0 | calibration set + zero flips on OP-Bench and cat 5; list credited items read by hand | flips above calibration base rate, or any change to cat 5 / OP-Bench scoring | +5 on the conventions column only |
| A1, A4, I13, I35 | Reader = judge model; home-made judge; no kappa outside LoCoMo; published numbers use other judges | Second independent judge on stored answers (`rejudge.py`, `second_view.py` built); Mem0-official prompt as comparable view; 150-row hand-labelled set with kappa | Judge-agnostic re-scoring of stored answers | LLM step (offline) | built-opt-in (tools), **deferred by user to the end** | `--judge-prompt mem0-official`, `second_view` | one GPU re-judge pass | kappa >= 0.6 on hand set; both judges reported | reporting one judge only | 0 (measurement) |
| I16, A16 | Noise floors are estimates (0.328 sqrt n) | Two same-config repeat runs per slice; replace the estimates | Slice-agnostic | code | built-opt-in (estimator); runs not done | `noise_floor.py` | 2 runs per slice | band in questions and points per slice | n/a | 0 |
| I24, I12 | Missing metrics: session-level R@k, stale-rank, adherence, per-benchmark latency/cost; date check per benchmark | Add to run summaries behind the schema; report with and without date check | Per-benchmark reporting | code | partly built | summary schema | 0 | schema tests | n/a | 0 |

### 2.2 L1 Query and answer contract

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **A03** [B1, C1, C2, C3, I4] | Answers of the wrong type (country for a city, date for a title); 111 baseline failures linked, 156 candidates | Typed query contract: target subject, relation, answer type, time scope, cardinality, recall vs invited inference; retrieve against it; verify the final claim matches (E06) | Contract fields are language- and dataset-neutral; uncertain classification falls back to the general evidence-grounded path | rules first; decider (typed question) for ambiguous cases; LLM only as a fallback | **in progress** (with E06) | proposed `read.query_contract: off\|rules\|decider` | rules 0; decider about 25 ms CPU | S on LoCoMo + OP-Bench; X with E06; F; controls: city/country, author/composer, visit/residence, winner/participant, literal vs hypothetical | ambiguous questions forcibly misclassified, useful qualified answers rejected | +6 to +10 q [estimate] of the 47 detail + 23 inference + list classes it touches |
| I4 | List-mode trigger was a LoCoMo-vocabulary regex; fires 20% | Intent trigger (`is_intent_list`) or decider | Question-shape cue, fired-rate reported per slice vs base rate | rule / decider | screened-verdict: NEUTRAL, safe (81.9 vs 81.6; OP +0.2) | `read.list_trigger: intent` | 0 | done; keep for the combined config | fired-rate far above the data shape's base rate | 0 to +3 |
| I3, A08 [A08], C4, B3, R2-6 | Open-domain 46.9% (96 q); inference refused or wrong 23, recall 22; 60 linked failures; `grounded_generic` rejected | Capability clauses composed from adapter-declared data shape; route by question shape (would/likely/might) to `grounded_generic_infer` / `routed_generic`; label recall vs grounded inference vs recommendation; hypothesis stated with uncertainty, never stored as a fact | Route chosen by shape, not dataset; unsupported personal attributes stay unknown | rule / decider | built-opt-in, r3c never run | `--qa-prompt routed_generic`, `grounded_generic_infer` | 0 extra calls | S (cat 1-4 + OP); controls: paired factual/hypothetical prompts, unsupported-attribute probes | plausible inference becomes asserted ownership/diagnosis/religion; OP-Bench sycophancy loss | +5 q [estimate] (30% of 23 + 7) |
| I61 | Premise-tolerant answering: one event matches all but a date/year | Answer it and name the mismatch; abstain when the subject mismatches | Rule: one event matches all but one attribute | LLM step / rule | not-built (judgement call, trades against cat 5) | proposed `read.premise_tolerant` | 0 | S + cat-5 guard slice | cat-5 abstention recall drops | +2 q [estimate] |
| I1, I22, C10 | Retry fired 22, accepted 0 on the full run; `is_refusal` regex | Neutral retry (done); whole-answer refusal match (done); retry cap (done); later replaced by G01 targeted retry | Retry never claims evidence exists | rule | done (default) | `--retry-refusal`, `--retry-guard` | up to +1 call on refusals | closed | n/a | 0 |
| I23, I25, I10, I8, I28 | English-only regexes; one default set tuned on one shape; decisions are regexes | `language_guard`; data-shape profiles (`DataShape`, presets); `Decider` port with heuristic default and OpenDecider adapter; as-of anchor for relative time | Infrastructure for every row that needs a per-shape default | rule / decider | built-opt-in (I10: screened neutral, not adopted) | `read.language_guard`, `data_profile`, `read.decider` | decider 25 ms CPU | per-profile adoption rule | profile defaults chosen from benchmark scores | 0 (enabler) |

### 2.3 L2 Atomic evidence and source links

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **A01** [C1, I33, I39, I47, I54, I59, I63] | Wrong-person attribution; 784 baseline failures linked (mostly OP-Bench), 892 candidates | Speaker / addressee / subject / assertion source / modality as separate fields; short replies via a bounded dialogue edge; uncertainty over guessing | Fields come from the store's own speaker set and roles; works for 1:1, N-party and user/assistant chats | rule tags; decider optional | built-opt-in: I39-I47, I49-I55 (`perspective`, `subject_weight` screened NEUTRAL by rule, full run done); I59 owner check and I63 header unscreened | `memories.episodic.policies.perspective`, `read.perspective_mode`, `read.owner_check`, `read.user_header` | 0 model calls | S for `owner_check: both` and `user_header: on`; controls: name swap, speaker-order swap, third-party questions, same-name users | speaker filter removes valid evidence about another person; coreference guesses create persistent false facts | +4 q LoCoMo, plus OP-Bench subject-confusion points [estimate] |
| I40, I41, I43, I44, I46 | Plans, hypotheses, negations, hearsay, hedges counted as facts | `mod`, `pol`, `rep`, `cert`, `scope` tags and read axes | Per-sentence rules; decider tasks as second opinion | rule / decider | built-opt-in, unscreened | `read.perspective_axes` family | 0 | S on OP-Bench (value/memory sycophancy) and LoCoMo | cat 1-4 loses beyond its band | feeds A01 / P03 |
| I47, I48, I49, I50, I51, I53, I54, I55 | Agent vs user subject, inferred provenance, plan windows, subject card, namespace context, visibility, kin resolution, subject-aware conflict key | See `docs/PERSPECTIVE_COMPARISON.md` X1-X9 | Each is a store-level field; defaults byte-identical | rule | built-opt-in (X1 ack promotion, X2 `write.inferred`, X3 absolute windows, X4 card, X5 context, X7 `write.participants`, X8, X9 `perspective_key`) | see register rows | 0 | S per axis only where a benchmark exercises it (BEAM / HaluMem assistant-information when wired) | n/a | 0 on current benchmarks |
| I52, I45 | Flat English sensitivity tag, no grade | Graded `sens:` tags and a read bar | Fixed category-to-grade table | rule / decider | built-opt-in, screen pending | `write.sensitivity`, `read.sensitivity_gate` | 0 | S on OP-Bench irrelevance | gate empties needed contexts | 0 to small |
| **A02** [I3, I32, I56, I61] | Plans, completed events, repeated mentions, updates conflated; 93 baseline failures linked, 370 candidates | Source-backed event tuples: actor, action, object, location, event-time interval, mention time, status, provenance; identity link only when enough evidence agrees; keep history | Schema property (cardinality one/many); no dataset words | rule + LLM extraction | not-built (pieces: I17 latest-wins, I42 as-of, I55) | proposed `write.event_ledger` | extraction LLM calls at sleep, 0 on the hot path | S; controls: planned->cancelled->completed, two same-day events, one event two days, residence vs visit | different events merged, or history lost under latest-only | feeds A06/E05 |
| **E01** [R01, A01, A02, A03] | Related facts stay isolated in raw turns; 224 baseline failures linked, 740 candidates | Atomic subject-relation-object facts with exact source spans, modality, time; join only through resolved entities and evidenced relations; read-time extraction first, then a write-time projection as a separate arm; raw turns stay authoritative | Rebuildable projection; deletion propagates (I72 liveness check exists); entity-agnostic | LLM step (extraction) | not-built; pieces exist: `mine_facts`, list cards, semantic facts | proposed `read.fact_path`; existing `mine_facts` | read-time: 1 call on set/relation questions; write-time: sleep-cycle calls | S (read-time arm), then S (write-time arm), X with R02; controls: origin/employer/author/medication/project examples; deletion propagation test | derived facts invent relations, lose qualifiers, survive parent deletion, cross namespaces | +6 q [estimate] (overlaps R01/E02) |
| A07 [B8, I54] | Pronoun / image references need adjacent context; 32 failures linked | Explicit reply/reference edges and bounded neighbour context; record whether an asset was available | Reply edge is a chat property | rule | not-built (window exists) | proposed `read.reference_edges` | 0 | S; controls: shuffled neighbours, ambiguous photos | wrong event bound | +2 q [estimate] |
| I5, I7, I21, I19, I72 | Speaker parsing, defaulted dates, false quarantine on boilerplate, tenant leakage, forget/miner race | Role metadata, `skip_defaulted_dates`, `chat_roles` preset, leakage probe, parent-liveness recheck | Data-shape properties | rule | done / built-opt-in; leakage probe: no leak; I72 fixed | `read.skip_defaulted_dates`, `firewall.signals.*_exempt_roles` | 0 | per-benchmark `n_quarantined / n_turns` assert open | quarantine rate above threshold on a new benchmark | 0 |

### 2.4 L3 Retrieval and evidence selection

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **R02** [I75, B7, B10, I9, I78] | 56 fusion cuts (23 with the lost gold inside one leg's top 10, 16 within top 3); perspective leg is a correlated voter; BM25-only gold pool 11 -> 3 on the dev control | Reserve slots for independent source families, de-duplicate correlated votes (v2: source-family pool); perspective as multiplier; hard namespace filters before admission | Protects any retriever whose legs disagree; no dataset word | code | I75a **screened round 7 arm 1: +15 net (81.4 vs 80.4), every category up, about 30% more rerank cost; OP-Bench half pending**; I75b / multiplier arms queued; **source-family pool v2 in progress** (other agent) | `read.pool_protect_per_leg: 3`, `read.perspective_leg: false` | +30% rerank (about +1 s GPU) [round 7 measured as relative cost] | S on OP-Bench half; X with relevance gate (P01) and with `rerank_context`; F | noise displaces better evidence; correlated legs double-voted; gains need unbounded pool growth | +15 observed (band +-12.9); v2 +3 to +6 more [estimate] |
| **R01** [B1, B8, I4, I67] | Instance absent from candidates for lists, category-to-instance, multi-hop; recall 69 q, 133 linked failures, 320 candidates | Bounded second retrieval only for missing slots; independent lexical + semantic paths; entity aliases with provenance; one-hop relation expansion; fixed cost cap | Slots come from the query contract (A03); same budget for all arms | rule / decider trigger | built-opt-in as I67 (`read.agentic`), unscreened; refined by E02 in wave 2 | `read.agentic: true`, `agentic_trigger: multi_hop` | about 0.45 extra LLM calls per question when `multi_hop` fires (28% of questions) | S at equal total call budget vs single pass; controls: entity-swapped, irrelevant-memory, unanswerable | adds unrelated facts, raises unsupported claims | +14 q (assumed 20% of 69) [estimate] |
| **R03** [B10, I31, I6, I20] | Hit survives but loses its antecedent / source in window assembly; overlapping windows waste tokens; 61 failures linked | Minimal evidence bundles (claim, speaker, antecedent, time anchor); merge overlapping windows; explicit prompt budget incl. headers and answer reserve; log drops | Token-based window (`replay_window_unit`), budget scaling | rule | built-opt-in, unscreened (I6, I20); I31 dedupe screened INERT | `read.replay_window_unit: tokens`, `read.replay_budget_scaling`, `rerank_context`, `read.dedupe` | `rerank_context` about +1 s GPU | S (`r4-rctx1/2` arms exist); vary distractor count and position | extra neighbours cause attribution errors; compression drops dates/negation | +7 q [estimate] |
| rerank, pool | 33 fusion cuts at leg rank 11-60, 5 `rerank_keep` cuts at order 13-15 | Pool 3, `rerank_keep` 15, chunked rerank (I9) | Pool as a function of haystack size (I20) | code | built-opt-in, unscreened | `candidate_pool`, `rerank_keep`, `read.rerank_chunk_chars` | rerank cost grows with pool | S, X with R02 (same pool budget) | pool growth without paired gain | +11 q combined with R03 [estimate] |
| **P01** [I27, I29, I30, I33, I63, I74] | Memory injected whatever the request; OP-Bench irrelevant 14.1, baiting 8.2; 859 candidates | Estimate whether memory can supply a required slot; filter by subject and intent; allow an empty context; named-reference bypass is a feature, not a universal rule | Per-store self-calibration (probe scores learned at first read), fixed margin in sigma units, fixed 0.5 decider cut (I37) | calibrated gate + decider | screened-verdict: r3d ADOPT-CANDIDATE (OP +22, LoCoMo -4 net inside band); clean r3e (decider_tasks: []) and r3c store-calibrated never run | `read.relevance_gate: decider\|store_calibrated`, `read.relevance_gate_bypass: named`, `read.abstain_on_raw` | decider about 25 ms CPU; gate 0 model calls; an emptied context saves about 2k tokens | S: r3e then `store_calibrated`; X with R02; F (MAB/ConvoMem: no recall loss when everything is on-topic); report fired-rate per slice | global gate suppresses multi-hop evidence, or a name bypass admits irrelevant facts | OP-Bench +15 to +22 points [r3d measured], LoCoMo -4 to 0 |
| I59, I60, I63 | Wrong-owner answers; absent-name matched to nearest entity; reader does not know which speaker is the user | `owner_check: both`, `entity_check: note`, `user_header: on` | Names and roles from the store's own speaker set | rule | built-opt-in, unscreened | `read.owner_check`, `read.entity_check`, `read.user_header` | 0 | S (cat 1-4 may not lose its band; OP-Bench subject confusion); optional cat-5 guard | abstention on cat 1-4 questions whose evidence is stated by the other speaker | +3 q LoCoMo, OP-Bench baiting up [estimate] |
| **P02** [I64] | Same persona themes re-injected; repetition 23.9; 450 failures linked | Session-scoped usage counter, small penalty on recently injected records, a task-specific reason to reuse | Counter is store state, reset per session; essential repeated constraints kept | rule | built-opt-in, unscreened | `read.reinjection_penalty`, `read.reinjection_window` | 0 | S on OP-Bench; control: recurring relevant constraint retained (allergy-type) | invents novelty, drops a relevant recurring constraint | OP-Bench repetition +5 points [estimate] |
| I33 | Profile / preference blocks injected regardless of the query | `profile_relevance_gate` | Relevance gate on profile channels | rule | built-opt-in; profile off in BEST | `read.profile_relevance_gate` | 0 | only when profiles are on | n/a | 0 |
| I11 | Bridge gate vocabulary and threshold tuned on one sample | Decider task `bridge_hop`, threshold as quantile | Self-calibrated | decider | screened-verdict: R2-1b not adopted | `read.bridge_hop_gate` | 3x search cost when hop fires | closed unless re-opened by R01/E02 | n/a | 0 |

### 2.5 L4 Conditional tool loop

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **E02** [R01, G01, V02; I67, I4, I30, I75] | Single retrieval or blind retry does not diagnose the missing evidence; 174 failures linked, 397 candidates | After each evidence assessment pick ONE action naming the missing slot: memory search, relation expansion, neighbour lookup, asset inspection, public-knowledge lookup, deterministic calculation, answer, qualified stop; stop on sufficiency, budget, repeat, no new evidence, contradiction; builds on `read.agentic` | Action set is a port; tools optional; same total caps for every arm | decider + LLM step (structured JSON, I69 repair) | not-built as slot-driven (I67 built-opt-in, unscreened) | `read.agentic` (extend) | 1-2 calls when fired, capped | S: single pass vs current agentic vs slot-driven at equal total caps; controls: failed tools, duplicates, misleading observations, answerable-without-tools | retries only change wording, favourable judge without new support, over budget | +7 q [estimate] (overlaps R01) |
| **E03** [A08, A03, P01] | Reader confuses unmentioned personal fact with a negative answer, or lacks the public relation for a qualified inference; 52 failures linked | **Approved.** For likely / recommendation questions, retrieve the personal premise, identify the missing public relation, query a trusted cached source or web provider with GENERIC public terms only (never conversation text); cite external evidence separately; return a qualified inference; never persist as a personal fact | Optional external-evidence port at the orchestration boundary; works on any store; web arm is reported apart from memory-only | LLM step + provider port | not-built; **approved, to build (wave 3)** | proposed `read.external_evidence: off\|cache\|web` | 1 query + 1 call when fired; network latency | separate web-enabled arm; paired likely-enjoy vs actually-listened pairs; negative / unknown preference controls | web evidence used to prove a private event or possession; personal data in a query; stereotype inference of sensitive attributes | +3 q [estimate] (open-domain inference class 23) |
| **E04** [A07, A05, M02; I54, I57, I58, I76, I77, I79] | Captions omit the title / object an original attachment carries; 177 failures linked | **Approved.** Preserve attachment identity, source turn, original URI, hash, availability; download ONLY the dataset's own referenced image URLs, once, cache by content hash; OCR / vision through a controlled adapter; validate title/object; join to the turn's event and date; failure explicit | Attachment preservation is a loader property; text-only and multimodal arms kept apart | LLM step (vision) | not-built; **approved, to build (wave 3)** | proposed `read.asset_evidence: off\|cached` | vision call only on image-dependent questions | separate multimodal arm; missing / expired URL, MIME mismatch, misleading captions, low-confidence OCR, ambiguous titles | web lookalike substituted, dataset search hint used as truth, title guessed from a generic caption, silent failure | +2 q [estimate] (photo-only errata excluded) |
| I68 | OP-Bench irrelevance / baiting: model cannot decline memory | `read.mode: agent_gated` (model decides to call `memory_search`) | Documented ablation vs P01 | LLM step | not-built (needs I65, done) | proposed `read.mode: agent_gated` | +1 turn when memory needed | only if P01 fails to close the gap | small Qwen under-calls tools | 0 (hypothesis) |

### 2.6 L5 Structured synthesis and verification

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| **E06** [A03, A04, G01; I58, I61, I77, I79] | Answer omits supported specificity, contradicts its explanation, or lists unsupported items; 1,053 baseline failures linked (all categories), 1,370 candidates | Claim ledger per answer slot: value, source, certainty, temporal precision, alternatives; verify type, list completeness and self-consistency; retry only the diagnosed defect | Verifier is rule + one bounded LLM check; reader-independent where possible | rule + LLM step | **in progress** (with A03) | proposed `reader.verify: off\|ledger` | +1 call when a defect is diagnosed | S; X with A03; controls: coarse dates, explicit negative vs non-mention, wrong final with right explanation, partial lists | correlated verifier rubber-stamps; overwrites justified uncertainty; approves unsupported list items | +6 q [estimate] (overlaps A03/A04/G01) |
| **E05** [A02, A05, A06; I56, I57, I76, I79] | Reader picks the wrong milestone or computes a duration before validating endpoints; 69 failures linked, 25 date/duration reader errors | Extract start / transition / end milestones with actor, predicate, status, source date, interval, exact span; search again only for missing operands; validate, then compute by code with a stated elapsed-time convention | Calendar arithmetic is language-independent; any number of milestones | LLM step (extraction) + code | **in progress** (other agent); I76 solver built | `--duration-solve [rewrite\|hint]`, `--date-repair`, `read.relative_dates_weekdays/happened` | 1 extraction call on duration questions; 0 for the code-only built parts | S (temporal slice); controls: interview vs acceptance, rescheduling, approximate endpoints, leap/month boundaries | a wrong anchor yields confident precision | +8 q [estimate] (I76/I79/I57 offline: about +4 safely claimed) |
| **A05** [I3, I57, I76, I79] | Relative dates, durations, weekdays inconsistent; temporal held-out 76.4 vs dev 87.7; 67 failures linked; 21 of 61 temporal failures code-repairable | Source-bound date operands + fixed arithmetic; preserve approximate intervals; resolve weekday/date conflicts | See E05 | code | built-opt-in, unscreened (I57, I76, I79) | as E05 | 0 extra calls | S | as E05 | +4 to +8 q [estimate] |
| **A04** [B1, C6, I4, I58, I77] | Lists: gold item present but extras added, or a partial list credited; 99 failures linked, list 22 + count 8 reader errors | Evidence table, one row per candidate with supporting turn; de-duplicate by identity; return supported items; score item recall and unsupported rate separately | Set semantics; no dataset words | LLM step + code | built-opt-in: I56 `--count-verify`; list prompt `grounded_generic_list`; unscreened | `--count-verify two_call\|single` | two_call +1 call on count questions (3.3%) | S; controls: partial lists, supersets with irrelevant additions, similar names, other-speaker items | gold-string inclusion treated as gain; partial credit | +5 q [estimate] |
| **A06** [C6, I56] | Event identity, completion, predicate not established by lexical/date dedupe; 22 failures linked | Provenance-linked ledger per query; count only distinct predicate-matching entries; reconcile explicit cumulative counts; range if unresolved | Needs A02 event tuples | code + LLM | built-opt-in (I56), unscreened | `--count-verify` | as A04 | S; controls: same-day separate visits, planned vs completed, winners vs entries | same-day events over-merged, planned events counted | +3 q [estimate] |
| **G01** [C1, C11, C2, I1, I3, I61, I79, I9] | Evidence present but answer wrong, overlong, contradictory or unjustified refusal; 145 failures linked (136 reader errors with complete evidence) | Answer contract with source-linked claims, bounded check for unsupported claims / missing slots / operand-result consistency, retry only a diagnosed failure | Same verifier as E06 | rule + LLM step | not-built (follows E06) | `--retry-refusal` family | +1 call on diagnosed failures | S, retry cost reported | retry changes an evidence-based unknown into a guess | +4 q [estimate] |
| **P03** [I1, I32, I47, I61] | False remembered events accepted; memory sycophancy 20.1; 115 failures linked | Compare assertion with source-backed facts: confirmed / contradicted / unknown; correct only the premise; absence is not contradiction | Store-support check (word overlap, later decider `noul`) | decider | built-opt-in: I32 `--no-record-hint` (screened NEUTRAL, safe; detector fired on 6/80 probes); decider swap not-built | `--no-record-hint` | 0 | S on OP-Bench; controls: true / false / unknown assertions, user corrections | absence treated as contradiction; reflexive disagreement | OP-Bench memory sycophancy +5 points [estimate] |
| C1-C10 | Reader-side: distractor lines, detail, open-domain, hypothetical, duration, lists, image captions, regressions, retry, reader size | See G01/A04/A05/A08; prompt-only variants exhausted | n/a | n/a | screened-verdict (neutral / rejected) except C11 | n/a | n/a | closed | n/a | n/a |
| C11 | Reader is 9B Q4; complete-evidence ceiling 87.1% | Reader-ceiling options in section 6 | Reader is a swappable port | n/a | blocked by 16 GB VRAM | none | n/a | separate arm, judge-controlled | local/cloud mixed comparison | unmeasured |

### 2.7 L6 Serving and engine ops

| id(s) | Problem + evidence | Solution | Generic design note | Decision | Status | Flag | Cost | Validation | reject_if | Gain |
|---|---|---|---|---|---|---|---|---|---|---|
| I65 | No agent-facing memory tool-set or guard for agent writes | Neutral JSON-schema tool-set (search / write / update / forget / confirm), one dispatcher, writes on an `agent_tool` channel through the firewall, budgets, verdicts | Framework-neutral (OpenAI / Anthropic / neutral export) | code | done (opt-in) | `memspine.protocols.tools` | 0 on read path | tests (15); not measured: whether models call tools well | n/a | 0 (adoption) |
| I66 | No MCP server | Zero-dependency stdio JSON-RPC over the dispatcher; read-only profile; namespace fixed by server config | Transport-free `McpServer.handle` | code | done | `memspine mcp` | 0 | tests incl. real subprocess | n/a | 0 (adoption) |
| I69 | Structured output: no constrained decoding or retry-with-error | Measure parse failures; optional one retry with validation error / `json_schema` on retry only | Needed by E02 / E05 / E06 JSON steps | code | built-opt-in; step 1 (read `structured_stats()` on a Qwen run) open | `llm.structured.retry_on_error`, `constrained_retry` | 0 unless retry | stop if failure_rate < 1% | n/a | 0 (enabler) |
| I70 | Framework adapters (LangGraph, LlamaIndex, OpenAI Agents) | Optional extras on the dispatcher | n/a | n/a | **on-hold (user)** | n/a | n/a | n/a | n/a | 0 |
| I71 | Inline write enrichment; no queued write path | Study done: heavy LLM enrichment already background; per-namespace idle-debounced micro-sleep, `write.mode: sync` default; 12 open decisions in the study | n/a | n/a | **on-hold (user)** | proposed `write.mode` | n/a | prerequisites I72, I73 done | n/a | 0 |
| I72, I73 | Forget vs miner race (privacy); no per-step write timers | Parent-liveness recheck under the namespace lock; opt-in timers | n/a | code | done | `observability.write_timers` | 0 | tests | n/a | 0 |
| I38 | CPU work not tuned (decider, bge-small repetition embedder, tests starving the GPU) | Threads = physical cores, bucketed batching, bf16 option, pytest `-n` cap during GPU runs | n/a | code | partly done | `decider_threads`, `decider_dtype` | n/a | n/a | n/a | 0 |
| E1-E4, D1-D7, F, G, H | Serving, harness, injection, environment, process | See appendix | n/a | n/a | done | n/a | n/a | n/a | n/a | 0 |

### 2.8 Crosswalk: our ids <-> Codex ids (37 original gaps)

| Our id | Codex families | | Our id | Codex families |
|---|---|---|---|---|
| B1 | R01 A04 A03 | | I47 | A01 P03 |
| B7 | R02 | | I54 | A01 A07 |
| B8 | R01 A07 | | I56 | A06 A02 |
| B10 | R02 R03 | | I57 | A05 |
| C1 | A01 A03 G01 | | I58 | M02 A04 |
| C2 | A03 G01 | | I59 | A01 |
| C3 | A08 A03 M02 | | I6 | R03 |
| C6 | A04 A06 | | I61 | A02 G01 P03 M02 |
| C11 | G01 V01 | | I62 | M02 |
| I1 | G01 A08 P03 | | I63 | A01 P01 |
| I27 | M01 P01 V01 | | I64 | P02 |
| I29 | P01 | | I67 | R01 V01 |
| I3 | A02 A05 A08 G01 | | I75 | R02 V02 |
| I30 | P01 V02 | | I76 | A05 |
| I31 | R03 | | I77 | M02 A04 |
| I32 | P03 A02 | | I79 | A05 G01 |
| I33 | P01 A01 | | I9 | V01 R02 G01 |
| I37 | V01 | | I4 | A03 R01 A04 |
| I39 | A01 V02 | | | |

Codex-only (no original gap): E01 (refines R01, A01, A02, A03), E02 (refines R01, G01, V02), E03 (refines A08, A03, P01), E04 (refines A07, A05, M02), E05 (refines A02, A05, A06), E06 (refines A03, A04, G01). The audit states E01-E06 refine existing families, not independent gains.

Codex layers (L0-L8) to this plan: L0 -> L0; L1 -> L1 (A01 is also L2); L2 -> L2; L3 -> L3; L4 -> L4; L5 -> L5; Codex L6 answer verification (G01, P03, E06) -> L5; Codex L7 personalisation control (P01, P02) -> L3; Codex L8 validation and rollout (V01, V02) -> L0. New here: L6 serving.

Q-to-J mapping: J1/J2/J5/J6/J7 (OP-Bench subscores) -> P01, P02, P03, A01; J8 single-hop -> R02, R03, I77; J9 multi-hop -> R01, R02, A04, A06, E01, E02; J10 temporal -> A05, E05; J11 open-domain -> A08, E03; J12 cat 5 -> A01, P03 (guard only); J13-J18 -> aggregates.

## 3. Anti-overfitting protocol (strict)

The rules apply to every row of section 2. A violation voids the result, not just the claim.

1. **No thresholds fitted to the benchmarks (I37).** Every gate, trigger and decider uses a calibrated probability with a fixed principled cut (0.5), or a margin in sigma units over the store's own off-topic baseline, or a rule that reads question shape. A number that needs per-benchmark tuning is rejected. Any trigger must report its fired-rate per slice and compare it with the base rate of the question shape in that data.
2. **Per-store self-calibration.** Score a small FIXED set of generic off-topic probes through the same retrieval + rerank path on first read of each namespace (and again after 50% growth); keep mu / sigma; pass only a fixed margin above them. The probe set contains no benchmark vocabulary.
3. **Adopt only with an isolated, matched screen on ALL in-scope slices.** One change against the frozen reference (same code, same stack, same reader and judge, same sampler, pinned worktree per H7), on LoCoMo cat 1-4 (1,540) and OP-Bench (859) together, never on a hand-picked subset. Partial or capped runs are descriptive only and cannot promote.
4. **Average-led adoption rule.** Macro mean across slices improves beyond the noise band, AND no slice loses more than its own band (single-hop, multi-hop, temporal, open-domain, each OP-Bench subscore). Bands: LoCoMo 1,540 about +-12.9 net questions; dev-only screens +-5 net per 233-304 q (A16); replace with measured values from repeat runs (I16). Report paired gains/losses, exact sign test and cluster (conversation / persona) bootstrap, not net alone.
5. **No best-per-question selection**, no "oracle of arms", no tuning on a per-question basis, no choosing the best of several seeds without pre-declaring it. Flags are chosen per data-shape profile (I25), never per question id.
6. **Interactions tested 2x2.** Any two changes that touch the same stage (R02 x P01, R02 x rerank_context, A03 x E06, E01 x E02, E05 x A05, owner_check x relevance gate) run as a 2x2 before they are combined; the combination is adopted only if the 2x2 shows no negative interaction beyond the band. Gains that overlap are credited once (section 6).
7. **Fresh-data / different-shape check before any claim.** Every adopted change must pass a **blind** check on **MemoryAgentBench Conflict_Resolution + ConvoMem** (user decision 2026-10-11): the configuration is frozen first, the pre-registration note names the metric and the tolerance, the run happens once, and the result is never used to tune anything. Splits are cut by hash (`make_split.py`) with the sealed half read only after freezing. Other already-downloaded sets (PrefEval, BEAM 100K in `evals/data/`, PerLTQA, PersonaBench, HaluMem) stay available as additional, non-blocking shape checks and are deferred. LongMemEval is EXCLUDED. A change that helps LoCoMo and OP-Bench but harms the fresh sets beyond their band is not claimed (it can stay a documented, shape-gated opt-in).
8. **Behavioural / metamorphic controls (dataset-free).** For each family, controls that do not use any benchmark: name swap, speaker-order swap, entity renaming, paraphrase, irrelevant-filler insertion, paired generic vs personal requests, unanswerable questions, tenant-isolation pairs. A feature that changes answers under a renaming alone is rejected.
9. **Cost reported with quality.** Every result row carries: extra model calls per query (first call and retries separately), prompt and completion tokens, GPU seconds, CPU ms, p50 / p95 latency, rerank calls, network calls (E03/E04), ingest cost. A gain bought with a cost increase beyond its predeclared tolerance is a trade-off, not an adoption.
10. **Flag graduation.** opt-in -> screened (verdict recorded in the register) -> candidate (passes rules 3-6) -> default inside a data-shape profile (rule 7 passed) -> global default only if every profile benefits. Defaults stay byte-identical until a row reaches "candidate".
11. **Reporting honesty.** The development pool is reported as development. The first fresh-data numbers are reported with the configuration hash. Errata handling is reported both ways (A5, M02). Second judge (A1) and Mem0-official view are produced for the final claim (wave 4).
12. **Licence hygiene.** Aggregate scores only for OP-Bench (no licence) and any derived per-question file stays gitignored; this plan contains no dataset text.

## 4. Per-wave GPU units

One "unit" = one matched pair of full runs (LoCoMo 1,540 + OP-Bench 859). From the recorded latencies (reader p50 about 1 s on LoCoMo, 2.5 s on OP-Bench; judge about 0.27 s) a unit is roughly 3 to 4 hours on this machine [estimate, not measured here]; a dev-only screen (LoCoMo 304 + OP-Bench dev 331) is about 0.3 unit; CPU-only offline replays cost 0 GPU. A round 7 run is live on the GPU; nothing in this plan launches until it finishes.

## 5. Ordered execution roadmap

Dependencies are read left to right: a wave starts when its gate passes. Items "in progress" are being built by other agents on 2026-10-11 (this plan does not duplicate them).

### Wave 0 - measurement fixes (in progress)
- Items: **M01 (in progress)**, **M02**, **V02** (per-leg floor logging, query-contract log fields, observed / reconstructed / absent marking), V01 pieces: frozen baseline manifest hash, behavioural control suite, **MAB Conflict_Resolution + ConvoMem wiring (in progress)**, I77 (judge conventions v2), noise-floor repeat runs.
- Depends on: nothing (CPU work), plus the round 7 GPU run finishing for the repeat runs.
- GPU cost: about 2 units for the two repeat runs of the reference (one per slice pair) [estimate]; everything else CPU.
- Gate to move on: official OP-Bench macro identical after the M01 rebuild; every evidence id resolved or explicitly unresolved (M02); noise bands replaced with measured values; reference config frozen (hash) and runnable on MAB-CR / ConvoMem loaders (retrieval-only smoke only, nothing tuned).

### Wave 1 - evidence / answer contract (in progress)
- Items: **A03** typed query contract + **E06** claim-ledger verifier (in progress), **E05** milestone extraction + code computation with A05 (in progress), **R02** source-family pool = I75 v2 (in progress); read out of the round 7 arms already queued: I75a OP-Bench half, I75b / multiplier+protect, post-steps (I56 / I57 / I76 I79), owner check (I59 / I60 / I63).
- Depends on: wave 0 manifest + M01 final scores; I69 `structured_stats()` read so the JSON steps of A03 / E05 / E06 are not parse-limited.
- GPU cost: about 6 isolated units (I75 v2, A03, E06, E05, plus round 7 leftovers) + about 3 units for 2x2 (R02 x P01 gate, A03 x E06, E05 x A05) = about 9 units [estimate]. Run offline replays on the existing traces first (0 GPU).
- Decision gate: each item passes rules 3-6 of section 3; adopt-candidates = I75a (if OP-Bench half shows no loss), plus any item whose 2x2 is clean. The wave-1 combined config is frozen (hash) before wave 2.

### Wave 2 - atomic facts, slot loop, lists / counts, OP-Bench control
- Items: **E01** atomic facts (read-time arm, then write-time projection arm) and **E02** slot-driven loop (refines I67 `read.agentic`), **A04 / A06** lists and counts (I56, event ledger from A02), **P01 / P02 / P03** for OP-Bench (r3e clean gate arm, `store_calibrated`, owner check, `user_header`, reinjection penalty, no-record hint with decider `noul`), A01 owner-check screens, R03 evidence bundles, A08 / I3 routed inference.
- Depends on: wave 1 frozen config; A03 (slots feed E01 / E02); A02 (feeds A06); structured-output reliability (I69).
- GPU cost: about 10 isolated units + about 5 for 2x2 (E01 x E02, R02 x P01, P01 x owner_check, A04 x E06) = about 15 units [estimate]; E01 write-time arm adds sleep-cycle LLM calls (count separately).
- Decision gate: OP-Bench official overall and each subscore (irrelevant, baiting, three sycophancy, repetition) improve without LoCoMo loss beyond band; multi-hop and list / count classes improve at equal total call budget. Output: the frozen "wave-2 candidate" config.

### Wave 3 - external capabilities (APPROVED, to build)
- Items: **E03** public knowledge (generic public terms only; cited separately; never proves a private fact; web / cache arm reported apart from memory-only) and **E04** image assets (dataset's own referenced URLs only, downloaded once, cached by hash, explicit failure; OCR / vision arm reported apart from text-only), plus A07 reference edges.
- Depends on: E02 action loop (they are its tools); A08 / A03 contract (E03 trigger = invited inference); loader attachment preservation (E04).
- GPU cost: small relative (about 3 units) because the tools fire only on a few percent of questions; vision calls and network latency are counted separately [estimate].
- Decision gate: gains on the targeted classes (inference 23, image-dependent) with zero effect on text-only queries; no sensitive-attribute inference; failed downloads stay explicit. Reported as separate arms; never mixed into the memory-only headline.

### Wave 4 - fresh blind confirmation and final claims
- Items: run the frozen wave-2 / wave-3 candidate ONCE on MAB Conflict_Resolution and ConvoMem (blind), the A1 second judge and Mem0-official view on the final answers, the behavioural control suite, cost ledger.
- GPU cost: about 2 units (smaller datasets) + one judge pass [estimate].
- Gate: no protected metric below its predeclared tolerance on the fresh sets; otherwise the offending feature goes back to opt-in shape-gated.

Parallel, no waves: L6 items stay as they are; I70 and I71 on hold; I69 step 1 (read `structured_stats()` on the next Qwen run) is free.

## 6. Path to 90 (honest)

Targets: 90% of 1,540 = 1,386 correct. The 80.4% run has 1,238; round 7 arm 1 reaches 1,254 (81.4%). 132 more questions are needed from there (about 8.6 points).

### 6.1 Levers

| Lever | Class (q) | Source | Estimate (q) | Notes |
|---|---|---|---|---|
| Leg-protected pool I75a (R02) | fusion cuts with gold inside a leg's top 10: 23 | measured (round 7) | +15 observed vs +14 assumed | already in hand if OP-Bench holds |
| Source-family pool v2 (R02) | cuts at leg rank 11-60: 33 | assumed | +3 to +6 | overlaps wider pool |
| Duration / dates by code (A05, E05, I76, I79, I57) | 22 temporal | assumed 55% | +8 (offline-safe about +4) | held-out temporal is where the loss sits |
| Recall: R01 / E01 / E02 / I67 / I4 | recall 69 | assumed 20% | +14 | overlaps A03 and lists |
| Wider pool, chunked rerank, `rerank_context` (R03, I9) | 33 + 5 | assumed 30% | +11 | shares ground with R02 |
| Inference route, A08, E03, I61 | inference 23 + refusals 7 | assumed 30% | +9 | open-domain 46.9% |
| Lists and counts (A04, A06, I56) | list 22 + count 8 | assumed 27% | +8 | |
| Detail (R03, owner check, A03 contract, E06) | wrong detail 47 | assumed 15% | +7 | largest reader class |
| Judge conventions (I58, I77) | 11 | measurement column only | +9 on the column | not the headline |
| Errata | 23 | denominator only | 81.4% headline excluding errata on the old run | |
| Larger reader (C11) | 136 reader errors with complete evidence | unmeasured | 10 to 30 plausible | blocked locally |

The seven counted levers of the forensics sum to +75 (85.3%). With the round 7 measurement replacing lever 1 the counted sum is about +76 (85.4% from 80.4%; the measured arm itself is 81.4%).

### 6.2 Overlap caveats (gains do not add)

- A fixed recall can leave a reader failure behind: the 235 incomplete-evidence questions are answered at 43.4%, but fixing their evidence moves them to the 87.1% complete-evidence rate, not to 100%.
- A03 (contract), E06 (ledger), G01 (retry), A04 / A06 (lists / counts) and E05 / A05 (dates) all attack the same 138 reader-class failures; crediting each at its own capture counts a question several times. E01 / E02 / R01 / I67 overlap on the 69 recall failures. R02 / R03 / wider pool overlap on cuts.
- Reader churn: 4.7% of complete-evidence questions flip when only the other context lines change (noise +-12.9 net on 1,540). Any gain below that band cannot be seen in one run.
- The Codex solution families are hypotheses ("PROPOSED; not implemented or validated"); their linked counts are rule-based candidate links, not causal failure counts.

### 6.3 Scenarios (estimates)

| Scenario | Capture | Result |
|---|---|---|
| Conservative (half the assumed capture, plus I75a) | about 0.5x | about 83% |
| Assumed capture | 1.0x | about 85.4% (86.6% excluding errata, 87.1% with the conventions column) |
| Stretch: 1.5x capture plus 20 q from a larger reader | 1.5x + C11 | about 89.0% (90.3% excluding errata) |
| 90.0% on the 1,540 headline | needs 148 q from the 80.4% run | about 2.0x the counted sum, or 1.5x plus a larger reader |

Honest verdict: with the local 9B reader the realistic landing zone is 85 to 88%. The complete-evidence ceiling is 87.1% (88.4% without errata); 90% needs the reader / judge side to rise as well as the retrieval side. Fresh-data results can be lower than the development pool, and nothing on the development pool can establish 90 as a general claim.

### 6.4 Reader-ceiling options (C11 is blocked locally)

A 14B reader (9.3 GB) + 9B judge (5.7 GB) + embedder / reranker (about 4 GB) exceeds 16 GB; Ollama would swap per call. Options:
1. Two-pass evaluation: run the 14B reader over all questions with only the reader and retrieval models loaded, store answers, then load the 9B judge and re-judge offline (`rejudge.py`). No per-call swapping.
2. A smaller quantisation of the larger reader (lower-bit 14B) if its quality on the 87.1% ceiling holds; compare judge-controlled (A7 2x2).
3. Cloud reader as a separate, clearly labelled non-local arm (for example a hosted Qwen3-32B); report next to, never instead of, the local result.
4. Cut the reader's load instead: deterministic solvers (A05, A06, E05), claim-ledger verification (E06) and typed contracts (A03) move arithmetic, counting and type checks out of the 9B reader.
5. Reader ensemble or self-consistency on diagnosed failures only (G01), at bounded call cost; correlated-error risk is the reject_if.
6. Thinking mode was tested and rejected (A9); it is not an option on this reader.
7. Reader-side measurement: credit the judge errors with the conventions column (I58 + I77: up to about 89.5% ceiling) and report errata-excluded headline, always as second columns.

## Appendix: closed or decided gaps (completeness)

| Ids | Disposition |
|---|---|
| A2, A3, A5, A6, A8, A10, A11, A12, A13, A14, A15 | done or decided (deterministic date pre-judge, list recall column, errata 47+5, dev split, sampler explicit, CIs, claims table) |
| A1, A4, A7, A9 | A1 deferred; A4 tool done; A7 2x2 planned (`r4-a7-*`); A9 closed, thinking not adopted |
| A16 | noise floor rule applied; measured bands pending (I16) |
| B1, B9, B11, B12, B13 | adopted / implemented |
| B2 | audited, hygiene only |
| B3, B10 | open via A08 and R03 |
| B4, B5, B6, B7, B8, B14, B15, B16 | tested negative / neutral, documented |
| C1-C10 | screened neutral or rejected, folded into G01 / A04 / A05 / A08; C11 blocked |
| D1-D7 | done (token counting, ctx guard, retry tokens, manifest, latency, `run.sh`, run/query ids) |
| E1-E4 | done / fixed |
| F1-F5 | clean or implemented |
| G1-G4 | done |
| H1-H8 | done (CI job present, tests, merge, golden, flaky test, import isolation, pinned runs, explorer) |
| R2-1 .. R2-6 | R2-1/R2-1b not adopted, R2-2 inert, R2-3 neutral, R2-4 rejected, R2-5 done, R2-6 open via A08 |
| I2, I34, I36 | measured (cat 5 69.4% dev; OP-Bench BASE 62.8; baselines) |
| I9, I14, I15, I16, I20, I21 | tooling built, runs open (see L0 / L3 rows) |
| J1-J18 | subscore views, mapped to the families in 2.8 |
