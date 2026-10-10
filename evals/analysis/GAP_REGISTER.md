# Complete gap register - LoCoMo, local Qwen stack (2026-10-10)

Every gap found so far, with evidence and solution options. Sources: `RECALL_GAPS_FORENSIC.md` (R), `READER_GAPS_FORENSIC.md` (D),
`ENGINEERING_GAPS.md` (E), `PIPELINE_TREE.md` (P). Reference run: fixed config `qa-full-qs-eq06-fix` 80.1%
(baseline `qa-full-qs-eq06-roff-fx` 74.5%, Qwen-4B reranker `qa-full-qs-eq06-rq4b4-fx` 70.8%). 1 question = 0.065 points.
Priority: P0 = blocks trustworthy numbers, P1 = next, P2/P3 = later. Status: open / fix ready / fixed / decided.

## Current scope and decisions (user, 2026-10-10)

- **Run only LoCoMo categories 1-5 + OP-Bench for now**; other benchmarks are added slowly, one at a time, later.
- **All section-I gaps are in scope as engine work** (incl. I10, I14-I21, I26): the memory server must handle every data shape; only their benchmark runs wait.
- **LongMemEval excluded** (never run). MemoryAgentBench, ConvoMem, PrefEval, BEAM, PerLTQA, PersonaBench, HaluMem: data downloaded and loading, **deferred** (not wired, not run).
- **No overfitting to one eval:** every engine change is judged on all in-scope slices (LoCoMo cat 1-4, LoCoMo cat 5, OP-Bench).
- **Adoption rule (average-led):** macro mean across slices must improve beyond noise; no slice may lose more than its noise band (`GENERALISATION_AUDIT.md` 4.3).
- **Licences:** research only; publish aggregate scores; never commit or redistribute data or per-question derived files (OP-Bench has no licence: local only).
- **Order of work:** I1 neutral refusal retry -> OP-Bench response-level eval (I27) -> baseline of the best config on LoCoMo 1-5 + OP-Bench -> re-score earlier changes -> generalise list-mode trigger (I4) and speaker votes (I5).
- Deferred user decision: A1 second judge after the fixes.

## A. Measurement and evaluation (fix first: every later number depends on these)

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| A1 | Reader and judge are the same 9B model | both fail on weekday arithmetic and long lists; judge flips on identical answers (E EVAL-1, D 4.4) | re-judge stored answers with a stronger independent judge (local Qwen3-32B, or Bedrock Qwen3-32B < $0.10/run via `rejudge.py`); hand-label 150 rows and report judge accuracy + kappa; report both judges | P0 | **deferred by user (2026-10-10): do after all fixes** |
| A2 | Judge false negatives | 24-50 questions (1.6-3.2 pts); rejects 7 of 20 exact resolved dates vs "the Friday before X" gold (D 4.1) | deterministic pre-judge: weekday/week-before date equivalence, containment, fuzzy names (12-17 q); second judge on disputed rows (14-20 q); few-shot date examples (3-6 q, weak alone) | P0 | **deterministic part implemented** - `--judge-date-check` + `rescore_dates.py`: credits exact single-day matches of relative gold ("the Friday before X"), never refusals/denials; dev R0 85.0 -> 86.7%, full fixed run 80.1 -> 80.6%; 10 full-run flips hand-checked, 0 false positives (2 caught and fixed by the denial rule). Second judge (A1) still deferred |
| A3 | Judge false positives | est. 45-60 partial lists credited, mostly multi-hop; truncated/refusal answers were credited earlier (D 4.2, E EVAL-7) | per-item list recall column; one scoring function where empty/truncated = miss; second judge | P0 | **measured and reported** - per-question `list_recall` and run-level `list_questions` in every report. Full fixed run: 177 list questions, mean recall 0.63; 65 credited with items missing (lenient judge, matches LoCoMo convention), 2 complete lists rejected. Kept as a separate metric; judge unchanged |
| A4 | Home-made judge prompt, tuned on test data; published comparison invalid | rubric/rubric-guarded written by us on LoCoMo failures (E EVAL-2) | run the official Mem0 J-score prompt verbatim with a strong judge and publish that as the comparable number; freeze judge prompts; "different judge, unreproduced" column for published numbers | P0 | **implemented** - `--judge-prompt mem0-official` (vendored Mem0 paper prompt, not byte-verified upstream); re-judging stored answers via `rejudge.py` documented. Use with A1 after the fixes |
| A5 | Gold / dataset errors | 21 gold errors + 3 photo-only questions in the read failures; est. 16-24 more among unread wrong rows; excluding them: 81.4% (D 5) | errata file (`gold_error`, `needs_image`) and report both headlines; audit the 112 "answer not literal in gold turn" retrieval cases (R N1) | P0 | **errata 43 entries** (+17 from the B2 audit, new tag `evidence_label_error` with the correct turn id); **errata 47 entries** after R2-5 (+4 `evidence_label_error`). **Reporting done**: `forensics_report.py` writes `errata` in `run_summary.json` and a section in `gap_summary.md` (overall and per category, n excluded; strict = confirmed `gold_error`/`needs_image`, plus a variant that also drops borderline entries; `evidence_label_error` is not dropped because the gold answer is right). Check on rs-r0-ref-i2: 85.0% (233 q) -> 86.4% (228 q, 5 excluded). Tests in `evals/tests/test_measurement_gaps.py` |
| A6 | Tuning on the test set | tuned on conv-26: fixes +10.5 there vs +5.1 on the rest; no split file (E EVAL-4) | dev/held-out split or 2-fold cross-fit saved in repo (tune on conv-26/30/41/42, report the other 6); external check on LongMemEval / LoCoMo-Plus; pre-registration note before each tuning round | P0 | **split saved** (`locomo_split.json`: dev conv-26/30/41/42, held-out 6); screens now on dev only |
| A7 | Fix run changes 5 things at once | prompt, retry, judge rubric, window, server context (E HAR-4) | re-judge baseline with guarded rubric and fix answers with original rubric (judge-only, ~15 min each); 2x2 {engine old/new} x {reader+judge old/new}; headline = judge-controlled number | P0 | open |
| A8 | Hidden sampler | Ollama applied `presence_penalty 1.5` (64-token window) to all 6,341 calls, judge included (E SRV-2) | send `presence_penalty 0`, `top_p`, `seed` explicitly and record them; derived Modelfile with correct defaults; measure effect by re-judging 300 answers | P0 | **implemented** - sampler sent and recorded on every call; default presence_penalty 0 (old runs = `--presence-penalty 1.5`) (merged, verified live) |
| A9 | Thinking-mode conclusion is an artefact | all 49 "truncated" think-on answers hit the old 4,096 server window (E SRV-1) | re-run those 49 questions at 16K context (~12 min); mark the result "confounded" in `QWEN_STACK_RESULTS.md` | P0 | **DONE - not adopted.** Best config, conv-26, 152 q, server window 16384. Thinking on: 63.2% vs 88.8% off (+3/-42). Cause of most of the gap: 43 answers hit the 4,096-token reasoning budget (empty or cut-off answer). Fair view, the 109 questions that completed: on 88.1% vs off 91.7% (+3/-7; on adds refusals and a wrong count). Cost: median answer 40 s and 5,184 completion tokens vs 0.7 s and 22 off. A bigger budget would be slower still and the completed subset already loses, so thinking stays off. Caveat: one conversation, one seed |
| A10 | No confidence intervals | single runs; measured noise is tiny (identical retrieval 1,540/1,540, 2 judge flips) but question-sampling CI is +-2.2 pts (77.7-82.7 around 80.1) (E EVAL-3/8) | cluster-bootstrap CI + paired test in every summary (code exists); never print a bare % | P0 | **implemented** - `accuracy_ci` (conversation cluster bootstrap) in every summary (merged, verified live) |
| A11 | Small ablations overstated gains | one-conversation ablation 84.2% vs full 80.1% (E) | ablate on held-out dev set with paired tests; full run before any claim | P1 | **addressed** - screens now paired on dev conversations; held-out run pre-registered (`prereg/2026-10-10_heldout_reader_fixes.md`) |
| A12 | "Sufficiency" vs QA accuracy differ in denominator and context size | retrieval-only on 1,986 incl. adversarial; QA on 1,540 (E EVAL-5) | report sufficiency on the same 1,540; at matched tokens; add R@10 before expansion | P1 | **implemented** - `sufficiency_on_qa_set`, `recall_at_10_hits` in run summaries |
| A13 | Excluded category and label mapping | cat 5 (adversarial/abstention) never reported; category mapping by convention (E EVAL-6) | separate abstention metric run; mapping and n in every table header; verify published entries' category definitions | P1 | **implemented** - cat 5 reported apart (`n_adversarial`, `adversarial`), never in the headline |
| A14 | Results docs contain superseded conclusions | think-mode, localhost "1.7 h saved", reranker conclusions (E PROC-5) | "status of each claim" table; generate tables from manifests with CI and judge id | P1 | **done** - "Status of each claim" table in `QWEN_STACK_RESULTS.md` |
| A15 | Corpus text in git | 6,476 rows of LoCoMo text committed (E EVAL-9) | kept by user decision; if shared: keep only summaries, stripped JSONL, CI check for corpus strings | - | decided: keep |
| A16 | Dev-screen noise floor (found during the pass) | across 12 configurations on the same 233 dev questions: 171 always right, 17 always wrong, **45 (19%) flip at least once**; the same boundary questions (0-48, 0-49, 0-75, 1-31, 1-56, 1-75) flip in almost every variant | treat screen deltas within about +-5 net as noise; target the 17 stable failures; confirm any candidate on all 4 dev conversations (584 q) before held-out; repeat-run noise check on one config | P0 | **new - applied from now on** |

## B. Retrieval - why gold evidence does not reach the context

Measured on the fixed run: 517 of 2,348 gold turns (22%) never reach the context - 280 in no leg's top-30 (recall), 237 in a
leg but cut at the fused top-10 (fusion); 427 more arrive only via the neighbour window; 75% of lost gold sits in a session
with no retrieved turn. Image captions, relative-time words and speaker confusion do NOT predict loss (R 2).

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| B1 | Category-vs-instance (multi-hop lists) | 164 lost gold turns (26-32%), 52% of such turns lost, 40 wrong questions; only 50% of multi-hop questions have all gold in any leg (R H) | H1 pool 30 + rank-tiered window (46 turns, +90 fully covered q); H2 rerank pool 30 keep 10 (oracle +81 q); H3 LLM decomposition `planner: llm` v2/v3 (+18-36 multi-hop q est.); H4 list cards / mined facts at write time (`mine_facts`, `list_cards`); H5 instance-seeded second round (new code) | P1 | **ADOPTED** - confirmed on all 4 dev conversations (584 q): 84.1% vs 82.9% (+12/-5, net +7, beyond the +-5 band), multi-hop 61.3 -> 65.8, single-hop +0.9; held on the unseen half too (conv-41/42 +6/-3). New default = `arms/BEST_dev_2026-10-10.json` (fixed reranker + list mode) |
| B2 | Answer not literally in the gold turn | 112 turns, 35 wrong q; part is weak gold labelling (R N) | N1 audit labels (metric hygiene); N2 pool/rerank (37 turns); N3 HyDE / answer-free rewrite leg (+5-15 q est.); N4 reply-aware indexing (measured -11..+2, skip); N5 Qwen3-Embedding-4B (unlikely: median vector rank 127) | P1 | **audited (B2_EVIDENCE_AUDIT.md)** - of 112 turns: 77 valid-implicit, 19 weak label, 16 wrong label (31% label artefacts, but only 1 retrieval-failed question cleared). Answer-string HyDE does not help (oracle worse than the question); turn-shaped hypotheses are a leaky upper bound; est. +3 to +8 q. Recommendation: hygiene only now, B1 first; optional turn-shaped HyDE leg for list/count questions later |
| B3 | Open-domain inference | 96 turns, 28 wrong q; reader converts only ~25% even with all gold (R I) | I1 pool/rerank (8-14 turns); I2 evidence-seeking sub-queries for "would X" (+3-8 covered); I3 reflective profile memory at sleep; I4 prioritise reader-side fixes (C3/C4) | P2 | open |
| B4 | Temporal / date-dependent evidence | 43 turns, 29 wrong q; temporal leg fires on only 187/1,540 questions (R T) | T1 `temporal_relative`, `temporal_infer_year` (5-12 turns); T2 `lexical_dates: true` (+10 q net simulated); T3 smarter write-time date index (`mine_event_dates`); T4 `rerank_date_prefix` (3-8) | P1 | **tested** - `lexical_dates` 85.4% vs 86.7% (+1/-4, temporal -3.1): rejected. `temporal_relative` + `temporal_infer_year`: leg fires on 11 vs 7 dev questions but changes no answer (+0/-0): neutral, not adopted. Remaining temporal errors are reader/date-arithmetic (C5) and judge (A2, done) |
| B5 | Lexical overlap present but out-ranked | 63 turns, 11 wrong q (R O) | O1 pool/rerank (35 turns); O2 `lexical_analyzer: english` + strip speaker names from BM25 query (+13-23 q, churn); O3 weights / rrf_k / reserved slots measured 0 or negative - do not run | P2 | **tested** - `lexical_strip_names` 85.4% vs 86.7% (+3/-6): not adopted; english analyzer neutral (see B6) |
| B6 | Morphology-only / paraphrase | 27 + 12 turns (R M/P) | `lexical_analyzer: english` (recovers 21); larger pool | P2 | **tested** - `lexical_analyzer: english` 86.3% vs 86.7% (+7/-8): multi-hop +4.6, single-hop -1.7; gains overlap with rerank_balanced (0-3, 0-15, 0-19). Neutral alone; revisit in the combination screen |
| B7 | Top-10 fusion cut | 237 gold turns cut; 195 vector-only; BM25 top-10 is 34% noise (R 2.5, 3) | candidate pool 30 + tiered window; rerank pool 30; hit selection aware of the window (X2: 1.87 of 10 hits overlap a higher hit's window, +15 q) | P1 | **fixed; not adopted in the default** - with list mode on 584 dev q: 84.1% both, but list+balanced churns +30/-23 vs list +12/-5 and trades single-hop/open-domain for multi-hop (70.3); head-to-head +23/-23 |
| B8 | Gold in sessions with no hit | 75% of lost gold; window saturated - no window shape reaches it (R 5) | session-level leg (session digests/summaries); two-stage session-then-turn search; diversity across sessions in hit selection | P1 | **tested** - `session_leg` fires on all 233 dev questions but 86.3% vs 86.7% (+1/-2): neutral (its records are mostly found by other legs), fixes none of the stable failures; not adopted. Larger per-session budgets untested |
| B9 | Reranker deletes hits | keeps 5.7 of 10; 97.6% of questions lose >=1; 74% of its lost gold were window neighbours; rr run 70.8% (R 6, E RET-1) | `rerank_floor: skip` (implemented); `relative_floor 0` / `rerank_blend` / `rerank_gate`; normalise by raw P(yes); log `n_dropped_by_floor` | P1 | **fixed in code; 2-conversation screen (233 q, dev conv-26/30): 84.5% vs 84.1% (+12/-11), all gold in context 83.1 -> 86.6%, survives fusion 64.5 -> 69.3%; best evidence coverage so far; full run pending** |
| B10 | Reranker scope | with pool 1 it sees only the fused top-10 and can only delete (R, P) | `candidate_pool` 2-3 + `rerank_keep` (exists, tested); `rerank_context` 1-2 so a reply is scored with its question turn | P1 | **in use (pool 2, keep 10) in the B9 measurement; `rerank_context` not yet tried** |
| B11 | Reranker unavailable = silent skip | sticky skip-log, no run assertion (E RET-3). Audited 2026-10-10: all 22 retrieval runs - every configured reranker scored 100% of queries, 0 failures, none unavailable; past reranker results are valid | runner asserts rerank calls > 0 and failures == 0 when configured; `rerank_required` engine flag | P1 | **implemented** - `rerank_check` in summary; CLI exits 3 if a configured reranker did not run (merged, verified live) |
| B12 | Window was hard-coded +-2 | `replay_window_before/after` added (worktree) and used in the fixed config (2/4) | merge; record in `describe()`; regenerate arm JSONs | P2 | **implemented and in use; merged** |
| B13 | Batched vs unbatched arms mixed | bf16 embeddings not batch-invariant (E RET-5) | one batching mode for all arms; measure bt1 vs bt32 once | P2 | open |
| B14 | Existing expansion options are neutral or harmful | `statement_probe`, `prf_expansion`, `cluster_expand`, `session_cap`, leg weights, rrf_k, cohesion/sentence/entity_expand legs (R 3, 7) | do not run; keep as documented negatives | - | decided |
| B15 | BM25 misses related wording; word2vec / word-vector leg | user request 2026-10-09/10 | IMPLEMENTED `read.word_vector_leg` (model2vec potion-retrieval-32M or gensim word2vec), per-leg weights and per-shape weights. Measured conv-26 (152 q, paired vs 83.6%): in place of BM25 78.9% (+6/-13), alongside BM25 80.9% (+7/-11); BM25 finds rare exact terms, a third leg dilutes fusion, temporal improves | P1 | **closed - measured negative.** Implemented, opt-in, default off. Paired screens on dev conversations: in place of BM25 -7 (conv-26); alongside BM25 -4 (weight 0.5), -5 with the fixed reranker, -7 with temporal-only weighting (2 conversations, 233 q). Always lowers multi-hop (65 -> 56-60), raises temporal (86 -> 87-91). Keep BM25; best = BM25 + fixed reranker. Follow-up queued 2026-10-10: word vectors IN PLACE OF BM25 on the BEST config, 2-question check + 2-conversation screen (conv-26/30, cat 1-5, 304 q) vs the baseline on the same questions (`run_wv_replace.sh`) |
| B16 | Vector leg shrank to top_k when BM25 was off | with `hybrid: false` the vector leg fetched 10 instead of 30, so the first replace-BM25 screen was unfair (81.6% with 66.7% leg coverage) | word-vector leg now widens the fetch like BM25; unit test added | P1 | **fixed (2026-10-10)** |

## C. Reader - wrong answer although the evidence was in context

Measured on the fixed run: 148 read failures, of which ~48 are not reader errors (A2, A5). Fixes vs baseline: 154 gains,
67 losses; refusals 236 -> 30; answers 32 -> 18 words while context +32% (D 1-2).

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| C1 | Wrong line chosen (distractor) | 18 q; gold line is in the final top 5 in 89% of read failures (D) | mark/order retrieval hits (hits first or `*`, neighbours after) 4-7 q; per-shape window (2/4 only for temporal/multi-turn) 3-5 q; answer verification pass (`--verify-answer`) 3-5 q | P1 | **implemented; dev neutral** - superseded by `grounded_v2` approach (see DEV_GAP_REASONING_2026-10-10.md) |
| C2 | Vague / wrong detail | 16 q (D) | answer-style clause "one sentence with the specific detail" 4-7 q; quote-then-answer 3-6 q | P1 | **implemented; dev screen neutral** (see C1); held-out run pre-registered |
| C3 | Open-domain world knowledge | 12 q (D) | world-knowledge clause in prompt 3-6 q; bigger reader (27-32B) 4-8 q | P2 | **yes/no rule tested and REJECTED** - `grounded_v2` 81.5% vs 86.7% (+5/-17): the reader answered "No, ..." to when/what questions; rule dropped |
| C4 | Open-domain hypothetical ("would X...") | 11 q (D) | route to an inference prompt committing to yes/no/likely with the supporting line 3-5 q; second-stage retry with a different instruction 3-6 q | P2 | open |
| C5 | Duration / elapsed-time arithmetic | 13 q (D) | duration annotator in `temporal_resolve` ("for N years" -> `[= since YYYY]`) 3-4 q; elapsed helper: extract two anchors, code subtracts 4-6 q | P1 | **duration annotator implemented; dev screen neutral** (+0/-1; 52 annotated lines, few duration questions on dev); elapsed helper not done; held-out run pre-registered |
| C6 | List / count incomplete | 10 q (D) | list/count clause for list-shaped questions 3-5 q; event dedup in context 1-3 q; structured fact memory with set semantics 4-6 q | P2 | **partly addressed** by `grounded_detail` list clause; screen pending |
| C7 | Image caption ignored | 8 q (D) | prompt line on `[image: ...]` + render as its own "Photo:" sentence 3-5 q | P2 | **in test** - caption rule kept in `grounded_v3` (screen queued) |
| C8 | Refusal / multi-hop link / date anchoring / conflict | 12 q (D) | implicit-date rule (line date = event date for past-tense "when") 2-4 q; coreference via previous 2 lines | P3 | **tested** - `grounded_v3` fixed its target (0-71) but 85.8% vs 86.7% (+3/-5); not adopted. Prompt-only reader fixes are exhausted on this reader: every variant landed within noise or below |
| C9 | Regressions from the grounded prompt + bigger context | 49 previously correct answers now wrong (lists, detail, distractors) (D 2.3) | C1 + C2 + per-shape window; screen every prompt change on dev set with paired test | P1 | open |
| C10 | Retry limits | fired 107 times, 31 end correct; rescues none of the 32 that refuse again (D 3) | second-stage retry with a different instruction; keep first retry (pays on open-domain) | P3 | open |
| C11 | Reader size | 9B Q4 reader; ceiling ~88% with all gold present | larger reader (Qwen3-14B/32B local) as a separate arm; thinking mode re-test (A9) | P2 | **blocked by GPU memory** - qwen3:14b (9.3 GB) + 9B judge (5.7 GB) + embedder/reranker (~4 GB) exceed 16 GB; Ollama would swap per call. Options: run reader 14B with judge offline re-judging later, or a cloud reader |

## D. Harness and token budget

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| D1 | Three token counters disagree; harness cuts from the tail | real tokens 1.12-1.25x engine count; harness +7.2% (date prefixes); top_k 20 overflowed -> 48.7% (E HAR-1) | count with the reader's tokenizer; engine counts the exact rendered string; truncate by least relevance not recency; assert per call | P0 | **done** - replay mode now drops neighbours of the worst hits first, then the worst hits (`--token-count reader`) |
| D2 | Server-side truncation invisible | prompts over n_ctx silently cut (E HAR-3) | per call: raise if prompt+completion >= n_ctx-8; pre-run check of server n_ctx; native endpoint with explicit `num_ctx` | P0 | **implemented** - per-call check vs `--server-ctx 8192`, flag in row and summary, `--strict-ctx` aborts (merged, verified live) |
| D3 | `prompt_tokens` sums retries | up to 2.6x engine count (E HAR-2) | store first/retry tokens separately; cost with/without retries | P1 | **implemented** - first-call and retry tokens stored separately (merged, verified live) |
| D4 | Manifest misses what determines results | no server config, sampler, model digest, engine path, git dirty flag (E HAR-5) | `runtime` block: package versions, `git describe --dirty`, argv, env, Ollama `/api/version`/`show`/`ps`; fail on dirty tree | P1 | **implemented** - `runtime` block: versions, git state, argv, env, Ollama version/ps (merged, verified live) |
| D5 | Latency confounded by concurrency | two arms on one Ollama slot: 3.6x server time (E HAR-6) | report server-side timings; run serially; record concurrency in manifest; `bench_llm.py` | P1 | **done** - reader/judge p50/p95 in summary; Ollama /v1 returns no server timings (null), concurrency via MEMSPINE_CONCURRENT_ARMS |
| D6 | Scripts report success on failure; hidden env controls | rc=0 on failure, `MEMSPINE_*` env vars not recorded (E HAR-7, PROC-4) | one `evals/run.sh` wrapper: `set -euo pipefail`, arm check, STATUS file, row-count check, explicit `--topk/--flags`, derived call cap, no overwrite without `--force` | P1 | **done and verified live** - `evals/run.sh` 2-question run: STATUS ok, sampler + runtime manifest + rerank_check + accuracy_ci + truncation guard + n_quarantined all recorded |
| D7 | Forensics logs append without run/query id | 11 duplicate questions break text joins (E INJ-6) | write `run_id`, `query_id` per row; open per run; refuse a non-empty dir | P2 | **implemented** - run_id/query_id in logs, refuse to overwrite, report joins on query_id (merged, verified live) |

## E. Inference serving

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| E1 | `localhost` slow on Windows | 2.1 s per plain request; not visible in recorded run latencies (~0.6-0.8 s/call overhead) (E SRV-3) | 127.0.0.1 everywhere (done); `bench_http.py`; warn on `localhost` | P2 | fixed |
| E2 | New HTTP client per call | ~130 ms per call (E SRV-4) | pooled client per event loop | P3 | **done, merged** |
| E3 | Ollama config not pinned | flash-attn flipped, model reloads, 237 MB debug log, no prefix cache for hybrid Qwen3.5 (E SRV-5) | one `ollama_env.ps1` (KEEP_ALIVE, FLASH_ATTENTION, CONTEXT_LENGTH, KV type, parallel) recorded in manifest; lower log level; retry with backoff on 5xx | P2 | **done** - `evals/ollama_env.ps1` (not run; current Ollama already has these settings) |
| E4 | GPU shared with desktop and models | dwm ~2.4-3 GB; two arms + Ollama reached 15.7/16.3 GB (E SRV-6) | record GPU memory at run start/end; one heavy arm at a time; `expandable_segments` | P2 | **done** - GPU memory at run start/end in manifest and summary |

## F. Injection (write path)

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| F1 | Write path audit | 5,882/5,882 written, identical text, correct dates, no session merges (E INJ-1) | make these permanent post-ingest invariants + `ingest_audit` block in summary | info | clean |
| F2 | Quarantine / trust not logged | `written` true for quarantined rows (E INJ-2) | log `quarantined`, `trust`, `status` per turn + `n_quarantined`; one control run with firewall off | P1 | **implemented** - quarantined/trust/status per turn, `n_quarantined` in summary (merged, verified live) |
| F3 | Timestamp parse failure becomes "now"; locale-dependent | latent (all parse today) (E INJ-3) | raise on unparsed timestamp; explicit month-name parser; engine counter `valid_from_defaulted` | P1 | **implemented** - unparseable timestamp raises (merged, verified live) |
| F4 | Ingest cost / time not measured | per-turn deposit latency in trace is not real (E INJ-4) | record flush time and records; `ingest_s`, `turns_per_s`, `wall_clock_s` | P2 | **done** - `ingest_timing` (wall seconds, turns/s per item) in summary |
| F5 | Day-only stamps, naive local time labelled UTC, in-session order = write order | 14 of 272 sessions within 1 h of midnight (E INJ-5) | document "dataset-local, no conversion"; assert sequential writes | P3 | open |

## G. Environment and tooling

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| G1 | No lock file, unbounded extras | `uv.lock` gitignored; huggingface-hub 2.x broke transformers; CUDA torch / bitsandbytes not in default env (E ENV-1) | commit an evals lock; pin `huggingface-hub<2`; `just evals-setup` with the cu128 index; `pip freeze` per run | P1 | **done** - `uv.lock` committed on `feat/locomo-infra` (257 packages, huggingface-hub 1.16.1); lock uses standard PyPI torch, GPU machines add CUDA torch with `just evals-setup` |
| G2 | `evals/datasets` shadows HF `datasets` | broke LanceDB; only `_launch.py` protected (E ENV-2) | rename to `evals/legacy_datasets`; test that `datasets.__file__` is not under evals/ | P1 | **implemented** (renamed to `evals/legacy_datasets`, test added) (merged, verified live) |
| G3 | `env -u` silently does nothing in Git Bash | still in 6 scripts (E ENV-3) | replace with `unset`; post-run non-empty-log check | P1 | **implemented** (`unset` in the 6 scripts, marked superseded by run.sh) (merged, verified live) |
| G4 | CRLF/LF churn | 985 files LF in index, CRLF in tree, no `.gitattributes` (E ENV-4) | `.gitattributes` (`* text=auto eol=lf`, `*.sh eol=lf`) + one renormalise commit | P2 | **done** - `.gitattributes` in place; `git add --renormalize .` found nothing to change (index already LF) |

## H. Process

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| H1 | CI never runs `evals/tests` | 59 test files, no `st` extra in CI (E PROC-1) | second CI job with stub reader/judge; nightly CPU smoke with golden `retrieved_ids` | P1 | **CI job added** (`evals/tests`, offline, stub reader/judge); not yet run in CI |
| H2 | Missing tests for new code | forensic hook, ingest log, floor interaction, budget invariants (E PROC-2) | stub-engine tests; schema validation in tests; tokenizer property test | P1 | **tests added** (forensic hook stages, schema validation of report rows) (merged, verified live) |
| H3 | Two diverged trees, duplicated runs, 20-25 one-off scripts | `memspine` vs `memspine-fixes` (132 files, 18.8k lines) (E PROC-3) | merge behind opt-in flags; one `evals/runs` + generated `runs/INDEX.md`; tag each campaign commit | P1 | **done** - `feat/locomo-fixes` and `feat/locomo-infra` merged into `feat/local-qwen-stack` (df8421f), no conflicts, engine unit suite and harness tests pass; worktrees removed, all run outputs kept in `evals/runs` |
| H4 | Date-dependent golden test (found during the pass) | `tests/unit/golden/pre_wave1_read.json` regenerates without some `[= Thu 2023-06-08]` annotations when goldens are rebuilt today; two agents hit it independently and restored it | find the clock dependency (relative dates resolved against today?) and pin the clock in the test; regenerate once | P2 | **resolved** - not a clock dependency: the golden is recorded against commit d6dccc5 (pre-#29) by design; regenerating on the current tree drops the intentionally removed annotation. Clock pinned in the test anyway (identical output at fake dates 2026 and 2031). Follow-up done: the file now re-records only with `MEMSPINE_UPDATE_PRE_WAVE1_GOLDEN=1`; the generic update leaves it untouched (verified) |
| H5 | Flaky harness test (found during the pass) | `evals/tests/test_reader_raw.py::test_extraction_keeps_the_raw_reply_in_meta` failed once and passed on re-run (reported by the R2-4 agent) | reproduce with `-p no:randomly` / repeat 20x; fix ordering or shared state | P3 | **FIXED 2026-10-10** - root cause: `readers._shared_client` cached pooled clients under `id(httpx)` and `id(loop)`; once a test's fake httpx class or its `asyncio.run` loop was freed, a recycled address returned a stale client (another test's fake, other REPLY). Product bug for real use too (client bound to a dead loop). Now a `WeakKeyDictionary` keyed by the loop holding `{(httpx, timeout): client}`; regression test `test_shared_client_never_serves_another_modules_client` (failed 1499/2000 before, 0 after). 60 repeat runs of the module clean |
| H6 | tests/unit + evals/tests in one pytest invocation fail 8 tests (ModuleNotFoundError: run_chunked in evals/tests/test_rev3_fixE.py and test_review_r3.py; pass when run alone): sys.path / import isolation between suites | the 8 failures appear only when both suites run in one pytest invocation. Same family as the `date_check._parser()` import of `failure_buckets` from `evals/`, which silently disables the date check when `evals/` is not on sys.path (GENERALISATION_AUDIT 2.3) | make the evals tests import via package path or a conftest that inserts evals/ on sys.path; move shared helpers (`failure_buckets.parse_interval`, `run_chunked`) into the package | P2 | open |
| H7 | GPU runs import live working-tree code while agents edit it | 2026-10-10: cd-best finished (351 rows, 80.9%) but run.sh was edited mid-run (bash reads scripts incrementally) so its STATUS step failed; cd-gated loaded a half-edited refusal.py (11 retries in neutral mode vs assertive in its reference) | launch GPU runs from a pinned git worktree (PYTHONPATH to its src/ and evals/), record the commit in the manifest; never edit run.sh/harness files in the main tree during a run | P0 | open - fix before the cat 1-5 baseline |

## I. Generalisation and cross-benchmark (2026-10-10)

Added after the cross-benchmark requirement (every enhancement must hold on all memory benchmarks, not only LoCoMo). Full reasoning, the per-feature overfitting table and the protocol are in `GENERALISATION_AUDIT.md`. Numbers here are the LoCoMo ones quoted from the sections above; "unmeasured" means no run exists on the current stack.

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| I1 | Refusal retry forces an answer on unanswerable questions | `RETRY_INSTRUCTION` tells the reader the memories contain relevant information; true for cat 1-4, false by construction on cat 5 and BEAM abstention. READER_GAPS_FORENSIC 3.2 notes it is a risk only because cat 5 was not in the 1,540. Fired on 107 q (6.9%), 31 end correct | retry only when retrieval confidence is high (reranker top score over a per-benchmark quantile) or the question is not abstention-eligible; default off for any dataset with abstention; report cat-5 / LME-abs delta with retry on and off | P0 | fixed in harness, baseline pending (2026-10-10: `--retry-refusal` is neutral by default, "Re-read the memories carefully. If they contain information that answers the question, answer it; if they truly do not, reply exactly: Not mentioned in the conversation."; old wording behind `mode="assertive"` (reader_id `+retry`, meta `retry_mode`); optional `require_context_overlap` valve, off; the retry never sees gold or category. To do: CLI flag for assertive / valve, then LoCoMo cat 1-5 baseline with retry on and off) |
| I2 | LoCoMo cat 5 (446 q) never measured under any adopted config | A13 only reports it apart; ENGINEERING_GAPS EVAL-6: abstention behaviour not measured; no cat-5 number in any gap doc. 173 dev / 273 held-out items | run cat-5 dev slice (173 q, `abstention-v1` judge) for reference vs best; add metrics: refusal rate on unanswerable, over-refusal on answerable, combined score, wrong-person detection; make cat-5 a guard slice in the adoption rule | P0 | **in scope next**: baseline of the best config on LoCoMo cat 1-5 after I1 |
| I3 | Grounded prompt is LoCoMo-worded and anti-abstention; no latest-wins or question-date clause | wording: `[YYYY-MM-DD]` said-date, dates 'in the wording the memories use' (LoCoMo gold style), 'best-supported answer even if indirect'. Latest-wins only in `dated_world`, `question_date` only in `question_dated`. BEAM knowledge-update and abstention probes | compose the prompt from capability clauses declared by the adapter (dates, question_date, conflicts possible, abstention allowed); screen each clause on cat 5 and BEAM (retrieval first, then QA) | P1 | **implemented, opt-in (2026-10-10).** New `--qa-prompt grounded_generic` (`readers.py`): answer from the memories as optional context, newest statement wins unless the question is about the past, relative dates against each line's date and the question date, exact refusal 'Not mentioned in the conversation', no invented specifics; no benchmark wording. Existing prompt ids unchanged. **Screen plan:** QA only (prompt change): LoCoMo cat 1-5 vs `grounded` (cat 5 abstention and cat 1-4 accuracy), OP-Bench irrelevance (do not inject unrelated details), BEAM knowledge-update later |
| I4 | List-mode trigger is a LoCoMo-vocabulary English regex | B1 doc: precision 40% against the list set; on question text only it fires on 93/584 LoCoMo dev questions but on 7/500 LongMemEval questions (0/133 multi-session), misses 'How many tops have I bought from H&M so far?' and false-fires on the single-answer 'What type of camera lens did I purchase most recently?' (text evidence only, LongMemEval is excluded from runs); fires on 304/1,540 (20%), 183 outside the list set, +686 tokens on those; regex and lexicon written after reading all ten conversations (dev/held-out not blind). Pool widening and tier fire on any set-shaped question; the speaker vote is inert on lowercase `user:`/`assistant:` | learned/embedding shape classifier calibrated per benchmark; fired-rate report per slice; tier as a function of turn length; keep opt-in for profiles without named speakers | P1 | open, **in scope** (measured on question text: 93/584 LoCoMo vs 7/500 LongMemEval question texts); generalise after the baseline |
| I5 | Speaker identity is parsed from a capitalised `Name:` text prefix; adapter writes role=user for every turn | `_SPEAKER_LINE` needs `[A-Z]`; PIPELINE_TREE section 1; BEAM/HaluMem loaders set speaker = role, so text is `user: ...`. Speaker vote (B1), R2-2, `lexical_strip_names` need names; role-aware features (assistant leg, recommendation tag, trust matrix, spk/sub/ask) are untestable through this adapter (assistant-information questions in BEAM/HaluMem) | pass speaker and dataset role as metadata to `write_messages`; vote by role for 1:1 chats and by NER entity for third parties; support N speakers; add a retrieval-only role-aware screen | P1 | open, **in scope**: subject-based votes (user "I" or named person) |
| I6 | Window 2 before / 4 after is counted in turns, budget 4,096 tokens | tuned on short LoCoMo turns (+55 gold turns, saturated); long assistant turns make neighbours not fit (skipped); PrefEval/ConvoMem filler inside the window; the window gave 49 of 154 gains and 9 distractor regressions (READER_GAPS_FORENSIC 2.3) | window radius in tokens or per shape; test symmetric 1/1 as the dialogue-agnostic default; retrieval-only on BEAM/ConvoMem/PrefEval | P1 | open |
| I7 | Date prefix and relative-date annotation on data without or with different timestamps | `relative_dates_anchored` copies LoCoMo gold wording ('the week before <d>'); ConvoMem/PrefEval/LaMP have no timestamps, `valid_from` defaults to now (PIPELINE_TREE section 1; `parse_turn_stamp` returns None, only a non-empty unparseable stamp raises) so every line would show today's date. Not run on such a dataset (verify live) | per-item `has_timestamps` flag: no date prefix and no annotation when false; `anchored` only for LoCoMo-style golds; unit test on a no-timestamp fixture | P1 | open |
| I8 | Relative-time questions anchored on question_date never tested; temporal_relative judged neutral on LoCoMo only | B4: temporal_relative/infer_year fire on 11 vs 7 dev questions and change nothing; BEAM temporal-reasoning probes ask relative to the session anchor; `lexical_dates` -3.1 temporal on LoCoMo | re-screen retrieval-only on BEAM temporal-reasoning with a question-time anchor (`temporal_relative`), then `lexical_dates` | P2 | open |
| I9 | Reranker adopted on coverage, not accuracy; never run on long assistant turns | B9 screen 84.5% vs 84.1% (+12/-11), all-gold-in-context 83.1 -> 86.6%; pool 20 fixed whatever the haystack size; 4-bit 4B reranker document truncation and latency unmeasured on BEAM/ConvoMem | retrieval-only screens with rerank on/off on BEAM, ConvoMem, PrefEval; set rerank max document length; pool as a function of haystack size | P2 | open |
| I10 | Query instruction names 'a question about a user's past conversations' | PrefEval queries are requests, LaMP queries are movie descriptions, not questions about the past | instruction per data shape; retrieval-only comparison with no instruction | P3 | **implemented, opt-in (2026-10-10).** `GENERIC_QWEN_QUERY_INSTRUCTION` (`config/constants.py`: 'Given a user message, retrieve memories that are relevant to responding to it') set through the existing `embedding.query_instruction`; arm `evals/arms/best-generic-prompt.json` = BEST_dev with only that key changed, flags in `best-generic-prompt.flags`. It changes query embeddings, so **screen retrieval-only first** (LoCoMo 1-5 recall/hit@k vs BEST_dev, then OP-Bench retrieval), then capped QA with `--qa-prompt grounded_generic` |
| I11 | Bridge-hop gate and set_question_wide: vocabulary and thresholds tuned on one sample | `_CUE_REL`/`_CUE_THING` nouns, `bridge_hop_weak_threshold` 0.4 on the Qwen3 reranker's raw P(yes) (model-specific); cue 7/584, weak 31/233; wide trigger verb list from LoCoMo wording; 3x search cost when the hop fires | calibrate the threshold as a quantile of each benchmark's own score distribution; measure cue/wide precision and fired-rate per benchmark before any adoption; keep opt-in | P2 | open; R2-1b gate in-sample +2/-0, out-of-sample check on conv-41/42 running |
| I12 | Judge date check is LoCoMo-shaped and its parser import can disable it | credits the FIRST date of the answer against a one-day gold (dev 85.0 -> 86.7, full 80.1 -> 80.6); `date_check._parser()` imports `failure_buckets` from `evals/` and returns False when not on sys.path; relates to H6 | move `parse_interval` into the package; report every benchmark with and without; log flips per benchmark; require the date asked (not just the first date) when the answer holds several | P2 | open |
| I13 | Judge = reader and rubric tuned on LoCoMo failures: style changes can be rewarded by the judge | A1 deferred, A4; calibration set 8 cases; no kappa on any other benchmark; guarded rubric accepts hedged and extra-detail answers and credits partial lists (65 of 177 list q, mean recall 0.63) | gate adoption on deterministic metrics first (R@k, all-gold coverage, alias match); official judge prompts per benchmark (`mem0-official`); hand-label 150 rows per QA slice and report kappa | P0 | open |
| I14 | Gold quality of the other benchmarks is unaudited; errata exist for LoCoMo only | PrefEval gold is verbatim for 92 of 1,000 persona rows; MemoryAgentBench gold mapping `ambiguous`/`unmapped`; LoCoMo had 21 gold errors + 3 photo-only in 148 read failures | `gold_quality` tag per query in every loader; errata file per benchmark; report with and without | P2 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I15 | No dev / held-out split for any benchmark but LoCoMo | `locomo_split.json` only; every adopted feature was screened on LoCoMo conversations | cut and hash splits (by conversation / user / person) for BEAM, ConvoMem, PrefEval, MemoryAgentBench, PersonaBench, LaMP; protocol in GENERALISATION_AUDIT section 4 | P0 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I16 | Noise floors for the other slices are estimates only | A16 measured +-5 net on 233 LoCoMo questions; the audit scales as 0.328 sqrt(n): BEAM dev 100 -> 3.3 q, cat 5 dev 173 -> 4.3 q (EST). B1's +7 on 584 dev q is at the edge of the scaled band (7.9) | one repeat run of the reference config per slice (A16 procedure); replace the estimates; paired exact test for retrieval-only runs | P1 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I17 | Knowledge update / contradiction handling untested; no stale-vs-current metric outside MemoryAgentBench | harness writes all turns as `episodic` via `write_messages`, so the supersede/conflict ladder never runs; BEAM keeps `stale_turn_ids` but no metric uses them; grounded prompt has no latest-wins rule | generalise `supersession_order_rate` to BEAM knowledge-update; contradiction-surfaced metric for BEAM; reader clause 'latest statement wins' as a capability clause (I3) | P1 | **implemented, opt-in, default off (2026-10-10).** Read side `read.latest_wins` (`off` / `annotate` / `annotate_recent_first`, `latest_wins_min_overlap` 0.5; `core/latest_wins.py`): on raw episodic turns and keyed facts the newest statement of a topic is marked `[latest]`, older ones `[earlier statement; a later one on this topic is dated D]`, nothing dropped; hedged so coexisting values are not told one replaced the other; `disputed` records get no ordering claim. Write side: the engine already ran supersede / contest / retract in the M4 ladder (`conflict.py`, `semantic/store.py`) and coexist only via the extractor's `event` kind; added `extract_graph.cardinality {rel: one|many}` so the operator's schema overrides the extractor. Tests `tests/unit/test_latest_wins.py`. **Screen plan:** LoCoMo cat 1-5 and OP-Bench must not regress with `latest_wins: annotate` (free retrieval-only screen first, then capped QA); later a MemoryAgentBench Conflict_Resolution check once that benchmark is added; the harness still writes turns as `episodic`, so the keyed path needs `extract_graph` on to be exercised there |
| I18 | Preference following has no reader prompt and no adherence metric | PrefEval (explicit / choice / persona, filler sweep), BEAM preference; only `CONVERSE_QA_PROMPT` answers as an assistant | personalised-response prompt; adherence judge; preference-turn R@k under filler as the retrieval-only metric | P2 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I19 | Multi-user / tenant isolation not measured by the eval harness | each item is one namespace; PersonaBench has several people per community; no leakage probe | two-user fixture with disjoint facts: cross-namespace leakage rate and per-tenant R@k; shared-grant cases | P2 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I20 | Scale: LoCoMo conversations are about 590 turns; BEAM 100K-1M is far longer | pool 20-30, budget 4,096, window 2/4, session_leg embedding all grow with the haystack; BEAM 10M not held | retrieval-only scale curve (R_all@k, coverage, p95 latency, tokens) over BEAM 100K/500K/1M | P1 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I21 | Firewall quarantine on repetitive assistant boilerplate in chat benchmarks is unmeasured | default-on embedding-outlier and 96-char prefix-repeat signals; F2 logs `n_quarantined` but nothing asserts it per benchmark | assert `n_quarantined / n_turns` in every summary and fail above a threshold; one firewall-off control per new benchmark | P2 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I22 | `is_refusal` regex misclassifies valid answers and drives retry and date check | 'there is no ...' and capital-I 'I did not' (`DENIAL`) match; written against LoCoMo refusals | unit tests on BEAM / PrefEval answers; classifier or question-aware check | P3 | open |
| I23 | English-only regexes in query_shape, temporal_query, temporal_resolve | month names, phrases, set nouns, family nouns; no multilingual loader in the harness | language flag; learned triggers; document as English-only until then | P3 | open |
| I24 | Harness metrics missing for the uncovered categories | no abstention precision/recall, session-level R@k, stale-rank rate, adherence, leakage rate, per-benchmark latency/cost, judge kappa per benchmark (GENERALISATION_AUDIT section 3) | add the metrics to run summaries behind the same schema; retrieval-only first | P1 | open |
| I25 | No data-shape profiles: one set of defaults is tuned on one shape | arm JSON `BEST_dev_2026-10-10.json` carries window, date annotation, rerank and list mode as global settings; the `base` template ships the read-path advantages measured on LoCoMo (USAGE.md templates) | adapter-declared shape profile (`has_timestamps`, `has_question_date`, `speaker_kind`, `abstention_possible`, `turn_length`, `haystack_size`) selecting config defaults; adoption rule applied per profile | P1 | open |
| I26 | BEAM / PrefEval / ConvoMem / LaMP never run with the current best config | no cross-benchmark number exists for the current stack; all gap-doc numbers are LoCoMo cat 1-4 | stage-1 retrieval-only baseline (reference vs best) on every dev slice before any new feature work | P0 | **open - in scope as engine work** (memory-server enhancement); runs on other benchmarks come later, measured on LoCoMo 1-5 + OP-Bench meanwhile |
| I27 | OP-Bench was only a retrieval proxy; no response-level over-personalisation score | adapter had injection rate / persona share / context repetition only (degenerate for an engine that always returns top-k); official scores are LLM-judged (`OPBENCH_PROTOCOL.md`) | wired `--dataset op_bench` end to end: official assistant prompt (`--qa-prompt opbench_assistant`), official judge prompts read from the local checkout (`--judge-prompt opbench`, local Qwen, thinking off), embedding-cosine repetition (bge-small, CPU), per-task/subtype scores in `summary.json` + `opbench_summary.json`, paired compare (`python -m memspine_evals.opbench`), run.sh support; dev = conv-26/30/41/42 first speakers (331 probes, ~30 min), held-out = other 6 (528, ~48 min) | P0 | **implemented (2026-10-10); run pending** |
| I28 | Decisions are regexes / fixed rules (retry, list trigger, bridge gate, relevance, abstention) | I4, I11, I22; user request 2026-10-10 | OpenDecider-nano (`manjunathshiva/opendecider-nano`, Apache-2.0, 400M ModernBERT encoder, typed `choice`/`score`/`noul` questions, calibrated probabilities, CPU) behind a pluggable `Decider` port; heuristic adapter = today's behaviour by default; every decision logged (task, label, confidence, adapter); never sees gold or category. NOT the RK3588 `.rknn` export the user linked first (Rockchip NPU only) | P1 | **implementing** (agent) - user 2026-10-10: own lightweight adapter (transformers + safetensors, Apache-2.0 attribution), NO `opendecider` package; parity check vs the package in a throwaway venv |
| I29 | Memory always injected: no gate can return an empty context | OP-Bench 8-probe check irrelevance 0.00 ("Hey Caroline! ..." on a generic school question); paper: no memory system near BASE, filters +10.6 to +17.3 vs critic +2.4 to +2.8 (SOTA_MEMU_MEMOS_LOCOMO.md) | absolute gate on RAW reranker/vector scores (`read.leg_min_scores` exists) or the OpenDecider relevance probability; allow an empty context and a working no-memory reader path; calibrate generically per I37 (store self-calibration + calibrated probability; LoCoMo dev + OP-Bench dev only validate) (risk: LoCoMo recall, cat-5) | P0 | open - design with I28; needs the OP-Bench dev baseline |
| I30 | `theta_abstain = 0.25` is dead on reranked reads; `relative_floor` bypassed under `rerank_floor: skip` | min-max rerank scores make the top candidate 1.0, so the abstain test never fires in the BEST config (code study 2026-10-10, SOTA_MEMU_MEMOS_LOCOMO.md) | compute abstain / floors on raw scores before min-max; unit test that an unrelated query can abstain under rerank | P1 | open (part of I29) |
| I31 | No de-duplication before assembly | MemU's dedup is a placeholder and duplicates make false premises look established (memory-sycophancy 18.5); our windows can repeat near-identical turns | `dedupe_jaccard` (or embedding near-dup) at assembly, opt-in; measure tokens and OP-Bench repetition | P2 | open |
| I32 | No 'no record' signal for user-asserted memories (memory-level sycophancy) | OP-Bench memory sycophancy: all systems low (BASE 33.9, RAG 25.8 on Qwen3-8B); probes 'do you remember when I...' about events that never happened | when retrieval finds no support for an asserted past event, tell the reader so (OpenDecider `noul`: 'is this event supported by the memories?'); reader prompt clause | P2 | **implemented, opt-in (2026-10-10).** `--no-record-hint` wraps the reader (`evals/memspine_evals/no_record.py`, `NoRecordHintReader`): when `asserts_past_event(question)` (first-person recall cues) and not `event_supported(question, context)` (under half of the question's content words in the context), a note 'no stored memory matches the past event' is prepended; no extra model call, flagged rows carry `meta.no_record_hint`. Detector and support check are constructor parameters, so OpenDecider can swap in a model `noul` question. **Screen plan:** OP-Bench memory-sycophancy probes (BASE vs hint) plus a no-regression check on LoCoMo 1-5 (count how many ordinary questions get the note; expect near 0) |
| I33 | Profile / preference channels injected regardless of the query | MemOS: up to 6 preferences per query at threshold 0.0 plus a 'must not violate' order; MemU category summaries act as a profile | keep profile/preference blocks off by default (already off in BEST); if enabled, gate them by relevance like I29; `profile_scope_gate` extended to retrieved evidence | P2 | open (guard) |
| I34 | No own no-memory BASE run on OP-Bench | the paper's headline is the drop vs BASE; our judge (local Qwen3.5-9B) and repetition embedder (bge-small) differ from the paper's, so our absolute scores are not comparable | run OP-Bench dev with memory disabled, same reader and judge (331 probes, ~15 min) | P1 | **queued** - `run.sh --system no-memory` added (28482e7); `run_opb_base.sh` runs after the word-vector screen (pinned worktree) |
| I35 | Our LoCoMo judge is not comparable to published numbers | published rows use the Mem0 gpt-4o-mini judge prompt (OmniMemEval) or vendor judges; judge choice alone moves a system by up to 18 points (SOTA_MEMU_MEMOS_LOCOMO.md section 10) | report with `--judge-prompt mem0-official` as a second view; A1 second judge (deferred by user until fixes are done) | P2 | open (linked A1) |
| I36 | Cross-benchmark baseline of the best config not yet measured | needed as the reference for every later change under the average-led rule | `run_baseline_x.sh` from the pinned worktree (H7): LoCoMo dev cat 1-5 (757 q) then OP-Bench dev (331 probes) | P0 | **running** (commit 5f186e3) |
| I37 | Gates and deciders risk being tuned to the in-scope benchmarks (LoCoMo, OP-Bench) | user 2026-10-10: 'make it more generic so it can also work on other benchmarks' | design rule for every gate/decider (I28, I29, I30, I32, I33, list/bridge triggers): (1) no benchmark-fitted thresholds - use calibrated probabilities with a fixed principled cut (e.g. 0.5); (2) self-calibrate per memory store: score generic off-topic probes at ingest to learn the store's irrelevant-score level, pass only a margin above it (works for any embedder/reranker/size, no labels); (3) thresholds in portable units (probability or margin over the store baseline), never raw reranker scores; (4) LoCoMo + OP-Bench validate, they do not set numbers - a gate that needs per-benchmark tuning is rejected; (5) zero-tuning transfer check on each later benchmark | P0 | open - applies to I29 design |

## Stable dev failures (target set for the next round, 2026-10-10)

Wrong in all 12 configurations screened on conv-26/30 (233 q). 4 are errata (0-5, 0-23, 1-9, 1-44). The 13 real ones:

| Group | Questions | Addressed by |
|---|---|---|
| multi-hop lists spread over sessions | 0-11, 0-34, 0-38, 0-70, 1-3, 1-23 | B1 list mode (screening) |
| open-domain inference | 0-22, 0-59, 0-69 | open - needs profile/inference memory (B3/I3); prompt rules failed (C3) |
| reader: wrong line or photo-dependent | 0-151, 1-20, 1-43, 1-48 | open - candidates: mark-hits (C1, neutral), photo-only (errata review) |

## Round 2 gaps (refill from the stable failures under the new default, 2026-10-10)

| ID | Gap | Evidence (dev, best config) | Solution options | Status |
|---|---|---|---|---|
| R2-1 | Bridge question needs a second hop | 0-11: hop 1 finds "moved from my home country"; "Sweden" sits in another turn linked only by "home country" (vector rank none) | second search seeded with key noun phrases of the first-hop hits (extend `second_round`, which seeds only names/dates); cap 1 extra hop | **screened - not adopted as built.** 233 dev q: 87.6% vs 88.4% (+5/-7, within noise), search 10.8 s vs 4.7 s per question (~3x). Works on the target: 0-11 bridge leg ranks gold D3:13 + D4:3 at 1-2, answer gains "Sweden". But the hop fires on all 233 questions; losses are reader drift (counts, months) on questions that need no hop. Next: R2-1b gate the hop (multi-hop cue or weak first-pass top score) and re-screen |
| R2-1b | Bridge hop fires on every question (3x search cost, reader drift on questions that need no hop) | R2-1 screen: fired on 233/233, +5/-7; all 5 losses needed no hop | opt-in gate `read.bridge_hop_gate`: `cue` (answer entity is described, not named: conservative regex `has_bridge_cue`), `weak` (second-best raw reranker score under `bridge_hop_weak_threshold` 0.4: one confident anchor, no support), `cue_or_weak`; records `bridge_gate` in forensics | **not adopted (opt-in only).** In-sample conv-26/30: 89.3% vs 88.4% (+2/-0). Out-of-sample conv-41/42 (351 q): 80.6% vs 80.9% (+1/-2) - no gain on unseen conversations. Caveat: confounded run (H7): cd-gated picked up the in-progress neutral refusal retry from the working tree, cd-best used the assertive retry; the deltas are tiny either way |
| R2-2 | Comparison questions about two people ("both", "in common") | 1-3: 0 of 4 gold turns retrieved; each person's facts are separate | for both/in-common questions run the speaker vote once per named person and fuse | implemented; fresh best-config re-run r2-best = 88.4%, identical answers to f7-list-i2 (+0/-0): no change on conv-26/30, reference reproduces exactly |
| R2-3 | List-mode trigger misses "How did X <verb> ..." multi-item questions | 1-23 (4 gold turns, trigger did not fire) | widen `is_set_question` to how-did/how-has questions with plural objects; measure trigger precision on all 1,540 questions | **DONE - neutral, not adopted.** 233 dev q: fired on 41 vs 32 questions, context changed on all 9 extra, score 88.4% = reference (+0/-0). The 9: 7 already right, 0-70 (evidence-label erratum) and 1-23 (gold turn never retrieved, a recall gap) stay wrong. Kept as an opt-in value |
| R2-4 | Reader picks a wrong line although the gold line is hit #1 | 1-20, 1-48 (gold rank 1, in context) | option to present the top hits first, then neighbours (chronological within each block); `*` markers alone were neutral (C1) | **DONE - rejected.** 233 dev q vs fresh reference r2-best (88.4%): hits_first 86.7% (+12/-16), hit_blocks 85.4% (+11/-18; temporal 87.3 vs 95.2). Chronological order stays. |
| R2-5 | Photo-only or bad evidence labels among stable failures | 0-151, 1-43 (photo), 0-70 (gold D15:13 is "Did you see that band?") | errata review | **done 2026-10-10**: none of the three is excludable; all three have a bad or incomplete evidence label and the system is also wrong. 0-70: labelled D15:13 is "Did you see that band?", the conference is D5:13; the system lists the poetry reading and the conference but adds other LGBTQ events. 1-43: label D1:25 is the question, the answer is D1:26 ("they're the ones performing at the festival"); text is enough (not photo-only), the question "the photo" is ambiguous and the system picked another dancer photo. 0-151: gold rests on the D18:15 photo caption ("a woman and a child walking on a trail"), missing from the labels; it was in context and the reader said camping (D18:19). Also 1-48: furniture/decor are in D3:5/D3:6, label is D3:4; system said hoodie. All four added as `evidence_label_error` (4 entries; accuracy unchanged). The other stable failures are real system errors or subjective inference gold (0-22, 0-59, 0-69); 1-20 gold 27 May is the session date of "I just got accepted", system said 10 May |
| R2-6 | Open-domain inference refused | 0-22, 0-59 | profile/inference memory (B3); prompt rules failed (C3) | open |

## Progress log

**2026-10-10 (I3, I10, I32)**
- I3: `grounded_generic` reader prompt; I10: generic query instruction + arm `best-generic-prompt` (+ `.flags`); I32: opt-in `--no-record-hint` no-record reader wrapper with swappable detector. All opt-in, defaults unchanged; tests `evals/tests/test_generic_reader_prompts.py`.

**2026-10-10 (I17)**
- I17 implemented opt-in: `read.latest_wins` (read-side earlier/latest marks, lexical same-speaker topic match, no model, no language list), `extract_graph.cardinality` (one/many override for supersede/coexist), 14 unit tests, USAGE keys, config golden gains two default-off keys. Not yet screened on LoCoMo / OP-Bench.

**2026-10-10 13:00**
- I34 queued (no-memory OP-Bench BASE). I28: user wants the model without the package - own adapter being written.

**2026-10-10 12:50**
- I37 design rule: gates/deciders must be generic (no benchmark-fitted thresholds; per-store self-calibration; portable units; LoCoMo + OP-Bench validate only).

**2026-10-10 12:40**
- Added I28-I36 from the OpenDecider request, the OP-Bench check and the MemU/MemOS code study (SOTA_MEMU_MEMOS_LOCOMO.md); B15 follow-up queued.

**2026-10-10 12:10**
- I1 fixed in harness (07e495b): neutral retry is the default; assertive kept as mode="assertive". Cat 1-5 counts: conv-26+30 = 304 (71 cat 5), 4 dev convs = 757 (173 cat 5).
- R2-1b out-of-sample: +1/-2 on conv-41/42 (80.6 vs 80.9) -> not adopted, stays opt-in. Run confounded by H7 (new P0).

**2026-10-10 OP-Bench response level**
- I27 implemented: `--dataset op_bench` runs end to end at response level, faithful to the official scorer (protocol and deviations in `evals/analysis/OPBENCH_PROTOCOL.md`; local-only data, nothing committed). Shipped file read the official way = the verified 1,700 (15 empty-list placeholders dropped). Dev slice 331 probes, held-out 528, official first-speaker default 859. Runs pending (GPU queue busy).

**2026-10-10 I1 fix**
- I1 fixed in harness (`refusal.py`): retry is neutral by default and claims no evidence; the old assertive wording stays behind `mode="assertive"` to reproduce BEST_dev runs; meta records `retry_mode`; optional context-overlap acceptance valve, default off. Tests: `evals/tests/test_refusal_retry_neutral.py`.
- Cat-5 baseline pending: run with `--categories 1,2,3,4,5` (conv-26+conv-30 = 304 q incl. 71 cat 5; 4 dev convs = 757 q incl. 173 cat 5; all 10 = 1,986 q incl. 446 cat 5). run.sh derives the question count only for `1,2,3,4`, so pass `--questions N`.

**2026-10-10 12:00**
- User: I10, I14-I21, I26 are back in scope as memory-server enhancements (not deferred); only their benchmark runs wait.

**2026-10-10 11:50**
- Scope narrowed by user: LoCoMo cat 1-5 + OP-Bench only; other benchmarks deferred (I10, I14-I21, I26 marked deferred).
- OP-Bench data cloned locally (yulinlp/OP-Bench @ 17c7efd, gitignored); 869 first-speaker probes (irrelevance_easy 159, irrelevance_hard 50, sycophancy 200, diversity 460). Agent wiring the response-level eval (I27).
- I1 neutral refusal retry in progress (agent).

**2026-10-10 11:30**
- R2-1b gated bridge: +2/-0 in-sample (89.3% vs 88.4%); out-of-sample check on conv-41/42 queued. R2-1+R2-3 combined = R2-1 alone (+5/-7).
- Adoption rule set by user: average-led (aggregate/macro beyond floor), per-slice guard at -F_b (GENERALISATION_AUDIT 4.3).
- LongMemEval excluded by user decision; suite = LoCoMo incl. cat 5 + other runnable benchmarks.

**2026-10-10 generalisation audit**
- New `GENERALISATION_AUDIT.md`: stage-by-stage origin map of the gaps, overfitting audit of every adopted/candidate feature and judge item, coverage gaps (cat 5, knowledge update, preference, multi-user, scale), and a cross-benchmark protocol (dev/held-out slice per benchmark, adoption rule with estimated noise floors, retrieval-only before QA).
- Section I added (I1-I26, all open): top items are the refusal retry vs abstention (I1), cat 5 never measured (I2), judge = reader (I13), no splits outside LoCoMo (I15), no cross-benchmark baseline (I26). H6 added: combined pytest run fails 8 tests (import isolation).
- No engine code or arm changed; no run started.

**2026-10-10 11:30**
- R2-1b bridge-hop gate implemented (cue / weak / cue_or_weak, default always = unchanged); offline: cue 7/584 dev q, weak 31/233; wins 2/5 and 1/5, losses 0/5 and 0/5. Arms best-bridge-cue and best-bridge-gated added; screen pending.

**2026-10-10 10:20**
- R2-1 bridge hop: 87.6% vs 88.4% (+5/-7), 3x search cost, fires on every question; fixes stable failure 0-11. Not adopted; gated variant R2-1b proposed. wide+bridge screen running.

**2026-10-10 09:25**
- R2-3 wide trigger neutral (+0/-0; fires on 9 more questions, no flips). Not adopted. Bridge screens running.

**2026-10-10 09:15**
- r2-best (today's code, best config) = 88.4%, identical to f7-list-i2: deterministic, R2-2 inert on conv-26/30.
- R2-4 rejected: hits_first -4 net, hit_blocks -7 net vs r2-best.

**2026-10-10 07:30**
- A9 closed: thinking on 63.2% vs off 88.8% on conv-26 (43 answers truncated at the 4,096 reasoning budget); on the 109 completed questions on 88.1% vs off 91.7%, at ~57x the latency. Not adopted.
- `cmp_ref.py --ref <run>` compares against any reference run.
- H5 fixed (d0f2ea6); R2-5 and A5 done (920b54c). R2-4 screen started.

**2026-10-10 12:00**
- H5 fixed: flaky `test_extraction_keeps_the_raw_reply_in_meta` was `readers._shared_client` keyed by `id()` of the httpx object and event loop; freed ids get recycled, so a later test (or loop) was served a stale cached client. Re-keyed on a loop-weak `WeakKeyDictionary` plus the httpx object itself; added a 300-iteration regression test. evals/tests and tests/unit green.

**2026-10-10 11:00**
- R2-5 done: stable failures 0-151, 1-43, 0-70 (and 1-48) are evidence-label errors with a real system error behind each; none is a `gold_error` or `needs_image`, so none leaves the headline. Four `evidence_label_error` entries added (errata now 47). Rest of the stable list reviewed: 0-11, 0-34, 0-38, 1-3, 1-23, 1-20 real; 0-22, 0-59, 0-69 subjective inference gold.
- A5 done: `forensics_report.py` reports accuracy with and without errata (overall and per category, n excluded) in `run_summary.json` (`errata`, schema updated) and `gap_summary.md`; `--errata` / `--no-errata`. rs-r0-ref-i2: 85.0% -> 86.4% (5 of 233 excluded).

**2026-10-10 09:40**
- Round 2 code done: R2-1, R2-2, R2-3 (engine), R2-4 (harness); all tests pass. GPU queue: A9 think-on -> R2-4 orders -> R2 engine (fresh best reference, wide trigger, bridge, both).

**2026-10-10 09:00 - round 2 refill**
- 12 stable failures remain under the best config (list mode fixed 0-38). New gaps R2-1..R2-6 above; agents started for R2-1/2/3 (engine) and R2-4 (harness).

**2026-10-10 08:40**
- A9: think-off 88.8% on conv-26 with the best config. The wrapper's env clearing made the first think-on run a no-op; `--think on` added. Real think-on running (slow: 23-54 s per answer).

**2026-10-10 08:10 - round 1 result**
- Dev (584 q, 4 conversations, date-checked): reference 82.9% -> **list mode 84.1%** (adopted); list+balanced 84.1% with 3x churn (not adopted).
- Round 1 tested and rejected or neutral: word vectors (B15), lexical_dates, temporal_relative (B4), english analyzer (B6), name stripping (B5), session leg (B8), grounded_v2/v3 and other prompt variants (C1-C3, C7, C8), duration resolver (C5, neutral on dev).
- Next: H4 golden clock fix, A9 thinking at 16K, B13 batching check; then round 2 on the stable failures (5 multi-hop, 3 inference, 4 reader).

**2026-10-10 07:35**
- B1 list mode 88.4% (+6/-2), multi-hop +9.3, clean. Confirmation run on conv-41/42 queued (A16 rule).

**2026-10-10 07:20**
- B8 session leg neutral (+1/-2, verified firing). B1 list mode screening now.

**2026-10-10 07:10**
- B5 strip names rejected (+3/-6). New gap A16: 45 of 233 dev questions flip across configurations - deltas within +-5 are noise.
- Refill: 17 stable failures listed above (4 errata, 6 B1 targets, 3 inference, 4 reader).

**2026-10-10 07:00**
- English analyzer neutral overall (+7/-8), multi-hop +4.6 - same questions as rerank_balanced. B5 strip running, then B8, then B1.

**2026-10-10 06:50**
- B4: lexical_dates rejected (-3 net); temporal_relative/infer_year neutral (verified the leg fires: 11 vs 7 questions).

**2026-10-10 06:40**
- B1 list mode built and committed (21 tests); screen queued after B5/B8. B2 audited (errata 43). New gap H4 (date-dependent golden).

**2026-10-10 06:20**
- Committed: B5, B8 (engine); A4, A12, A13, D1, D5, E3, E4, F4 (harness); all tests pass.
- grounded_v3 85.8% vs 86.7% - not adopted; prompt-only fixes treated as exhausted.
- B1 list mode being built (agent). GPU queue: B4 configs -> english analyzer -> B5 strip -> B8 session leg.

**2026-10-10 05:55**
- Fixed rerank_balanced: 87.1% vs 86.7% (+9/-8), multi-hop +9.3 points; recovered the vector-only hits it targets.

**2026-10-10 05:40 - full pass over the register started (user: fix everything, test each, refill)**
- Engine agent: B8 session leg, B5 name stripping. Harness agent: A4, A12, A13, D1, D5, E3, E4, F4.
- GPU queue: fixed rerank_balanced -> grounded_v3 -> B4 (lexical_dates, temporal_relative) -> B5/B6 (english analyzer).
- B1 designed; build after the engine agent. Still deferred: A1 (second judge). Later: A7 (2x2), A9 (thinking at 16K),
  B2 (answer-not-literal audit / HyDE), B13, C4, C6, C10, C11, F5.

**2026-10-10 05:20**
- `grounded_v2` failed (81.5% vs 86.7%, +5/-17): its yes/no rule made the reader open "when/what" answers with "No, ...".
  `grounded_v3` = v2 without that rule, queued. Fixed `rerank_balanced` re-screen running.

**2026-10-10 05:05**
- G4 done (nothing to renormalise). A3 measured: list recall 0.63, 65 partial lists credited - reported as a metric.
- B7: `rerank_balanced` found to be a no-op (bug); fixed + test; re-screen queued after the current screen.
- Screen: rerank_balanced (pre-fix) = identical to reference (+0/-0); grounded_v2 and both running.

**2026-10-10 04:30**
- Held-out run stopped (user: fix gaps first); outputs deleted unread.
- All 42 dev wrong answers analysed (`analysis/DEV_GAP_REASONING_2026-10-10.md`): judge date errors 5, gold/photo 5-6,
  vector hits lost in fusion 5, category-vs-instance lists 8, reader 11.
- Implemented: A2 date check (+ offline rescore), errata +2, `grounded_v2` prompt. Screen running: rerank_balanced,
  grounded_v2, both.

**2026-10-10 03:45**
- Reader-fix screen (2 dev conversations, 233 q, fixed reranker, new sampler defaults): R0 reference 85.0%; C1+C2 84.5%
  (+7/-8); C5 84.5% (+0/-1); all three 85.0% (+7/-7) - all within noise.
- Pre-registered held-out run (conv-43/44/47/48/49/50, 956 q): R0 primary, R3 (all reader fixes) secondary; adopt R3 only
  if paired net >= +10 questions.

**2026-10-10 03:10**
- C1, C2, C5 implemented and committed (all tests pass). Screen running on 2 dev conversations with the fixed reranker,
  reference re-run under the new sampler defaults: R0 reference, R1 C1+C2, R2 C5, R3 all.

**2026-10-10 02:45**
- H3 done: one code line (`feat/local-qwen-stack`), all tests pass; infra features verified on a live 2-question run.
- Next: reader fixes C1 (mark retrieval hits), C2 (answer detail), C5 (duration/elapsed helper); D1 replay-mode token
  counting; then the held-out run of the best configuration (fixed reranker) with the new run-integrity settings.

**2026-10-10 02:20**
- Screen finished (2 dev conversations, 233 q): fixed reranker 84.5% (best), fixed reference 84.1%, every word-vector
  variant 81.5-82.4% (-4 to -7 net). B15 closed as measured-negative; next: H3 merge, then reader fixes C1/C2/C5,
  then the full held-out run of the best configuration.

**2026-10-10 02:05**
- 2-conversation screen (233 q, development conversations): fixed reference 84.1%, fixed reranker 84.5% (best coverage),
  BM25 + word vectors 82.4% with or without the reranker. V3 (word vectors weighted for temporal questions only) running.

**2026-10-10 (infra merge-ready)**
- Implemented and committed on `feat/locomo-infra` (all tests pass): A8, A10, B11, D1 (partial), D2, D3, D4, D6, D7, F2, F3, G1, G2, G3, G4 (renormalise pending), H1 (CI not yet run), H2.
- Decisions: A1 second judge deferred until all fixes are done; G1 `uv.lock` committed.
- Next: merge `feat/locomo-fixes` + `feat/locomo-infra` (H3); D1 for replay mode; reader fixes C1/C2/C5.

**2026-10-10 (later)**
- Done: A5 errata file, A6 split file, A14 claim-status corrections.
- In progress (worktree `memspine-infra`, two agents): A8, A10, B11, D1-D4, D6, D7, F2, F3, G1-G4, H1, H2.
- Running: 2-conversation screen (conv-26/30 = development) of BM25 + word vectors vs references.

**2026-10-10**
- Done: complete register (64 gaps, now 65 with B16); word-vector leg built, tested (7 unit tests), screened (B15);
  vector-leg fetch bug fixed (B16); reranker floor fix measured on one conversation (B9/B10); reranker audit of 22 past
  runs (B11: all valid); serving fixes (E1, E2, E3 partly).
- Running: 4-conversation paired screen (about 600 q per arm): fixed config, fixed reranker, and BM25 + word vectors
  three ways (weight 0.5; with the fixed reranker; weight 0.3 with 1.0 for temporal).
- Stopped by request: the full 1,540 run of the fixed reranker (restart after the screen if it still leads).
- Pending decision: A1 second judge (local Qwen3-32B vs paid API with the official Mem0 prompt).
- Pending, not started: all of A (measurement) except A9-related notes; B1/B2/B4/B7/B8 retrieval design work;
  C reader fixes beyond the grounded prompt, retry and judge guards; D1-D7; F2-F5; G1-G4; H1-H3.

**2026-10-09**
- Done: fixed configuration (grounded prompt, refusal retry, judge guards, window 2/4, gold-style relative dates,
  date-mention leg) 80.1% vs 74.5% baseline on all 1,540 (A7: not yet attributable); forensic stage logging,
  JSON schemas, HTML reports; Ollama flash attention, q8 KV cache, 8192 window.
