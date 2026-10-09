# Complete gap register - LoCoMo, local Qwen stack (2026-10-10)

Every gap found so far, with evidence and solution options. Sources: `RECALL_GAPS_FORENSIC.md` (R), `READER_GAPS_FORENSIC.md` (D),
`ENGINEERING_GAPS.md` (E), `PIPELINE_TREE.md` (P). Reference run: fixed config `qa-full-qs-eq06-fix` 80.1%
(baseline `qa-full-qs-eq06-roff-fx` 74.5%, Qwen-4B reranker `qa-full-qs-eq06-rq4b4-fx` 70.8%). 1 question = 0.065 points.
Priority: P0 = blocks trustworthy numbers, P1 = next, P2/P3 = later. Status: open / fix ready / fixed / decided.

## A. Measurement and evaluation (fix first: every later number depends on these)

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| A1 | Reader and judge are the same 9B model | both fail on weekday arithmetic and long lists; judge flips on identical answers (E EVAL-1, D 4.4) | re-judge stored answers with a stronger independent judge (local Qwen3-32B, or Bedrock Qwen3-32B < $0.10/run via `rejudge.py`); hand-label 150 rows and report judge accuracy + kappa; report both judges | P0 | **deferred by user (2026-10-10): do after all fixes** |
| A2 | Judge false negatives | 24-50 questions (1.6-3.2 pts); rejects 7 of 20 exact resolved dates vs "the Friday before X" gold (D 4.1) | deterministic pre-judge: weekday/week-before date equivalence, containment, fuzzy names (12-17 q); second judge on disputed rows (14-20 q); few-shot date examples (3-6 q, weak alone) | P0 | **deterministic part implemented** - `--judge-date-check` + `rescore_dates.py`: credits exact single-day matches of relative gold ("the Friday before X"), never refusals/denials; dev R0 85.0 -> 86.7%, full fixed run 80.1 -> 80.6%; 10 full-run flips hand-checked, 0 false positives (2 caught and fixed by the denial rule). Second judge (A1) still deferred |
| A3 | Judge false positives | est. 45-60 partial lists credited, mostly multi-hop; truncated/refusal answers were credited earlier (D 4.2, E EVAL-7) | per-item list recall column; one scoring function where empty/truncated = miss; second judge | P0 | **measured and reported** - per-question `list_recall` and run-level `list_questions` in every report. Full fixed run: 177 list questions, mean recall 0.63; 65 credited with items missing (lenient judge, matches LoCoMo convention), 2 complete lists rejected. Kept as a separate metric; judge unchanged |
| A4 | Home-made judge prompt, tuned on test data; published comparison invalid | rubric/rubric-guarded written by us on LoCoMo failures (E EVAL-2) | run the official Mem0 J-score prompt verbatim with a strong judge and publish that as the comparable number; freeze judge prompts; "different judge, unreproduced" column for published numbers | P0 | **in progress** - official Mem0 judge prompt verbatim (harness agent) |
| A5 | Gold / dataset errors | 21 gold errors + 3 photo-only questions in the read failures; est. 16-24 more among unread wrong rows; excluding them: 81.4% (D 5) | errata file (`gold_error`, `needs_image`) and report both headlines; audit the 112 "answer not literal in gold turn" retrieval cases (R N1) | P0 | **errata file: 26 entries** (+1-57, +0-23 from dev reasoning); reporting with/without in progress |
| A6 | Tuning on the test set | tuned on conv-26: fixes +10.5 there vs +5.1 on the rest; no split file (E EVAL-4) | dev/held-out split or 2-fold cross-fit saved in repo (tune on conv-26/30/41/42, report the other 6); external check on LongMemEval / LoCoMo-Plus; pre-registration note before each tuning round | P0 | **split saved** (`locomo_split.json`: dev conv-26/30/41/42, held-out 6); screens now on dev only |
| A7 | Fix run changes 5 things at once | prompt, retry, judge rubric, window, server context (E HAR-4) | re-judge baseline with guarded rubric and fix answers with original rubric (judge-only, ~15 min each); 2x2 {engine old/new} x {reader+judge old/new}; headline = judge-controlled number | P0 | open |
| A8 | Hidden sampler | Ollama applied `presence_penalty 1.5` (64-token window) to all 6,341 calls, judge included (E SRV-2) | send `presence_penalty 0`, `top_p`, `seed` explicitly and record them; derived Modelfile with correct defaults; measure effect by re-judging 300 answers | P0 | **implemented** - sampler sent and recorded on every call; default presence_penalty 0 (old runs = `--presence-penalty 1.5`) (merged, verified live) |
| A9 | Thinking-mode conclusion is an artefact | all 49 "truncated" think-on answers hit the old 4,096 server window (E SRV-1) | re-run those 49 questions at 16K context (~12 min); mark the result "confounded" in `QWEN_STACK_RESULTS.md` | P0 | open |
| A10 | No confidence intervals | single runs; measured noise is tiny (identical retrieval 1,540/1,540, 2 judge flips) but question-sampling CI is +-2.2 pts (77.7-82.7 around 80.1) (E EVAL-3/8) | cluster-bootstrap CI + paired test in every summary (code exists); never print a bare % | P0 | **implemented** - `accuracy_ci` (conversation cluster bootstrap) in every summary (merged, verified live) |
| A11 | Small ablations overstated gains | one-conversation ablation 84.2% vs full 80.1% (E) | ablate on held-out dev set with paired tests; full run before any claim | P1 | **addressed** - screens now paired on dev conversations; held-out run pre-registered (`prereg/2026-10-10_heldout_reader_fixes.md`) |
| A12 | "Sufficiency" vs QA accuracy differ in denominator and context size | retrieval-only on 1,986 incl. adversarial; QA on 1,540 (E EVAL-5) | report sufficiency on the same 1,540; at matched tokens; add R@10 before expansion | P1 | **in progress** - sufficiency on the QA set, R@10 before expansion (harness agent) |
| A13 | Excluded category and label mapping | cat 5 (adversarial/abstention) never reported; category mapping by convention (E EVAL-6) | separate abstention metric run; mapping and n in every table header; verify published entries' category definitions | P1 | **in progress** - adversarial/abstention metric (harness agent) |
| A14 | Results docs contain superseded conclusions | think-mode, localhost "1.7 h saved", reranker conclusions (E PROC-5) | "status of each claim" table; generate tables from manifests with CI and judge id | P1 | **done** - "Status of each claim" table in `QWEN_STACK_RESULTS.md` |
| A15 | Corpus text in git | 6,476 rows of LoCoMo text committed (E EVAL-9) | kept by user decision; if shared: keep only summaries, stripped JSONL, CI check for corpus strings | - | decided: keep |

## B. Retrieval - why gold evidence does not reach the context

Measured on the fixed run: 517 of 2,348 gold turns (22%) never reach the context - 280 in no leg's top-30 (recall), 237 in a
leg but cut at the fused top-10 (fusion); 427 more arrive only via the neighbour window; 75% of lost gold sits in a session
with no retrieved turn. Image captions, relative-time words and speaker confusion do NOT predict loss (R 2).

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| B1 | Category-vs-instance (multi-hop lists) | 164 lost gold turns (26-32%), 52% of such turns lost, 40 wrong questions; only 50% of multi-hop questions have all gold in any leg (R H) | H1 pool 30 + rank-tiered window (46 turns, +90 fully covered q); H2 rerank pool 30 keep 10 (oracle +81 q); H3 LLM decomposition `planner: llm` v2/v3 (+18-36 multi-hop q est.); H4 list cards / mined facts at write time (`mine_facts`, `list_cards`); H5 instance-seeded second round (new code) | P1 | **designed (B1_LIST_QUESTIONS_DESIGN.md)** - list mode behind a question-shape trigger: speaker-restricted vector vote + pool 30 with window only on ranks 1-10; simulated +30 fully covered list questions (95% CI 20-41), est. +4 to +8 correct; existing subject_leg harmful, speaker_probe neutral, diversity/second-round no gain. Build after the engine agent finishes (B8/B5); caveat: trigger written after reading all conversations |
| B2 | Answer not literally in the gold turn | 112 turns, 35 wrong q; part is weak gold labelling (R N) | N1 audit labels (metric hygiene); N2 pool/rerank (37 turns); N3 HyDE / answer-free rewrite leg (+5-15 q est.); N4 reply-aware indexing (measured -11..+2, skip); N5 Qwen3-Embedding-4B (unlikely: median vector rank 127) | P1 | open |
| B3 | Open-domain inference | 96 turns, 28 wrong q; reader converts only ~25% even with all gold (R I) | I1 pool/rerank (8-14 turns); I2 evidence-seeking sub-queries for "would X" (+3-8 covered); I3 reflective profile memory at sleep; I4 prioritise reader-side fixes (C3/C4) | P2 | open |
| B4 | Temporal / date-dependent evidence | 43 turns, 29 wrong q; temporal leg fires on only 187/1,540 questions (R T) | T1 `temporal_relative`, `temporal_infer_year` (5-12 turns); T2 `lexical_dates: true` (+10 q net simulated); T3 smarter write-time date index (`mine_event_dates`); T4 `rerank_date_prefix` (3-8) | P1 | **config screen queued** (lexical_dates; temporal_relative + temporal_infer_year) |
| B5 | Lexical overlap present but out-ranked | 63 turns, 11 wrong q (R O) | O1 pool/rerank (35 turns); O2 `lexical_analyzer: english` + strip speaker names from BM25 query (+13-23 q, churn); O3 weights / rrf_k / reserved slots measured 0 or negative - do not run | P2 | **in progress** - BM25 speaker-name stripping (engine agent); english analyzer screen queued |
| B6 | Morphology-only / paraphrase | 27 + 12 turns (R M/P) | `lexical_analyzer: english` (recovers 21); larger pool | P2 | **config screen queued** (lexical_analyzer english) |
| B7 | Top-10 fusion cut | 237 gold turns cut; 195 vector-only; BM25 top-10 is 34% noise (R 2.5, 3) | candidate pool 30 + tiered window; rerank pool 30; hit selection aware of the window (X2: 1.87 of 10 hits overlap a higher hit's window, +15 q) | P1 | **bug found and fixed** - `read.rerank_balanced` was a no-op (fused list cut to the pool size before the balanced selection; identical answers on 233 dev q). Fixed with 3x fused list when on, unit test added; re-screen queued |
| B8 | Gold in sessions with no hit | 75% of lost gold; window saturated - no window shape reaches it (R 5) | session-level leg (session digests/summaries); two-stage session-then-turn search; diversity across sessions in hit selection | P1 | **in progress** - session-level leg (engine agent) |
| B9 | Reranker deletes hits | keeps 5.7 of 10; 97.6% of questions lose >=1; 74% of its lost gold were window neighbours; rr run 70.8% (R 6, E RET-1) | `rerank_floor: skip` (implemented); `relative_floor 0` / `rerank_blend` / `rerank_gate`; normalise by raw P(yes); log `n_dropped_by_floor` | P1 | **fixed in code; 2-conversation screen (233 q, dev conv-26/30): 84.5% vs 84.1% (+12/-11), all gold in context 83.1 -> 86.6%, survives fusion 64.5 -> 69.3%; best evidence coverage so far; full run pending** |
| B10 | Reranker scope | with pool 1 it sees only the fused top-10 and can only delete (R, P) | `candidate_pool` 2-3 + `rerank_keep` (exists, tested); `rerank_context` 1-2 so a reply is scored with its question turn | P1 | **in use (pool 2, keep 10) in the B9 measurement; `rerank_context` not yet tried** |
| B11 | Reranker unavailable = silent skip | sticky skip-log, no run assertion (E RET-3). Audited 2026-10-10: all 22 retrieval runs - every configured reranker scored 100% of queries, 0 failures, none unavailable; past reranker results are valid | runner asserts rerank calls > 0 and failures == 0 when configured; `rerank_required` engine flag | P1 | **implemented** - `rerank_check` in summary; CLI exits 3 if a configured reranker did not run (merged, verified live) |
| B12 | Window was hard-coded +-2 | `replay_window_before/after` added (worktree) and used in the fixed config (2/4) | merge; record in `describe()`; regenerate arm JSONs | P2 | **implemented and in use; merged** |
| B13 | Batched vs unbatched arms mixed | bf16 embeddings not batch-invariant (E RET-5) | one batching mode for all arms; measure bt1 vs bt32 once | P2 | open |
| B14 | Existing expansion options are neutral or harmful | `statement_probe`, `prf_expansion`, `cluster_expand`, `session_cap`, leg weights, rrf_k, cohesion/sentence/entity_expand legs (R 3, 7) | do not run; keep as documented negatives | - | decided |
| B15 | BM25 misses related wording; word2vec / word-vector leg | user request 2026-10-09/10 | IMPLEMENTED `read.word_vector_leg` (model2vec potion-retrieval-32M or gensim word2vec), per-leg weights and per-shape weights. Measured conv-26 (152 q, paired vs 83.6%): in place of BM25 78.9% (+6/-13), alongside BM25 80.9% (+7/-11); BM25 finds rare exact terms, a third leg dilutes fusion, temporal improves | P1 | **closed - measured negative.** Implemented, opt-in, default off. Paired screens on dev conversations: in place of BM25 -7 (conv-26); alongside BM25 -4 (weight 0.5), -5 with the fixed reranker, -7 with temporal-only weighting (2 conversations, 233 q). Always lowers multi-hop (65 -> 56-60), raises temporal (86 -> 87-91). Keep BM25; best = BM25 + fixed reranker |
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
| C8 | Refusal / multi-hop link / date anchoring / conflict | 12 q (D) | implicit-date rule (line date = event date for past-tense "when") 2-4 q; coreference via previous 2 lines | P3 | **in test** - earlier-line reference rule kept in `grounded_v3` (screen queued) |
| C9 | Regressions from the grounded prompt + bigger context | 49 previously correct answers now wrong (lists, detail, distractors) (D 2.3) | C1 + C2 + per-shape window; screen every prompt change on dev set with paired test | P1 | open |
| C10 | Retry limits | fired 107 times, 31 end correct; rescues none of the 32 that refuse again (D 3) | second-stage retry with a different instruction; keep first retry (pays on open-domain) | P3 | open |
| C11 | Reader size | 9B Q4 reader; ceiling ~88% with all gold present | larger reader (Qwen3-14B/32B local) as a separate arm; thinking mode re-test (A9) | P2 | open |

## D. Harness and token budget

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| D1 | Three token counters disagree; harness cuts from the tail | real tokens 1.12-1.25x engine count; harness +7.2% (date prefixes); top_k 20 overflowed -> 48.7% (E HAR-1) | count with the reader's tokenizer; engine counts the exact rendered string; truncate by least relevance not recency; assert per call | P0 | **in progress** - replay-mode ranked truncation (harness agent) |
| D2 | Server-side truncation invisible | prompts over n_ctx silently cut (E HAR-3) | per call: raise if prompt+completion >= n_ctx-8; pre-run check of server n_ctx; native endpoint with explicit `num_ctx` | P0 | **implemented** - per-call check vs `--server-ctx 8192`, flag in row and summary, `--strict-ctx` aborts (merged, verified live) |
| D3 | `prompt_tokens` sums retries | up to 2.6x engine count (E HAR-2) | store first/retry tokens separately; cost with/without retries | P1 | **implemented** - first-call and retry tokens stored separately (merged, verified live) |
| D4 | Manifest misses what determines results | no server config, sampler, model digest, engine path, git dirty flag (E HAR-5) | `runtime` block: package versions, `git describe --dirty`, argv, env, Ollama `/api/version`/`show`/`ps`; fail on dirty tree | P1 | **implemented** - `runtime` block: versions, git state, argv, env, Ollama version/ps (merged, verified live) |
| D5 | Latency confounded by concurrency | two arms on one Ollama slot: 3.6x server time (E HAR-6) | report server-side timings; run serially; record concurrency in manifest; `bench_llm.py` | P1 | **in progress** - server-side timings (harness agent) |
| D6 | Scripts report success on failure; hidden env controls | rc=0 on failure, `MEMSPINE_*` env vars not recorded (E HAR-7, PROC-4) | one `evals/run.sh` wrapper: `set -euo pipefail`, arm check, STATUS file, row-count check, explicit `--topk/--flags`, derived call cap, no overwrite without `--force` | P1 | **done and verified live** - `evals/run.sh` 2-question run: STATUS ok, sampler + runtime manifest + rerank_check + accuracy_ci + truncation guard + n_quarantined all recorded |
| D7 | Forensics logs append without run/query id | 11 duplicate questions break text joins (E INJ-6) | write `run_id`, `query_id` per row; open per run; refuse a non-empty dir | P2 | **implemented** - run_id/query_id in logs, refuse to overwrite, report joins on query_id (merged, verified live) |

## E. Inference serving

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| E1 | `localhost` slow on Windows | 2.1 s per plain request; not visible in recorded run latencies (~0.6-0.8 s/call overhead) (E SRV-3) | 127.0.0.1 everywhere (done); `bench_http.py`; warn on `localhost` | P2 | fixed |
| E2 | New HTTP client per call | ~130 ms per call (E SRV-4) | pooled client per event loop | P3 | **done, merged** |
| E3 | Ollama config not pinned | flash-attn flipped, model reloads, 237 MB debug log, no prefix cache for hybrid Qwen3.5 (E SRV-5) | one `ollama_env.ps1` (KEEP_ALIVE, FLASH_ATTENTION, CONTEXT_LENGTH, KV type, parallel) recorded in manifest; lower log level; retry with backoff on 5xx | P2 | **in progress** - ollama_env.ps1 + manifest (harness agent) |
| E4 | GPU shared with desktop and models | dwm ~2.4-3 GB; two arms + Ollama reached 15.7/16.3 GB (E SRV-6) | record GPU memory at run start/end; one heavy arm at a time; `expandable_segments` | P2 | **in progress** - GPU memory in manifest (harness agent) |

## F. Injection (write path)

| ID | Gap | Evidence | Solution options | Pri | Status |
|---|---|---|---|---|---|
| F1 | Write path audit | 5,882/5,882 written, identical text, correct dates, no session merges (E INJ-1) | make these permanent post-ingest invariants + `ingest_audit` block in summary | info | clean |
| F2 | Quarantine / trust not logged | `written` true for quarantined rows (E INJ-2) | log `quarantined`, `trust`, `status` per turn + `n_quarantined`; one control run with firewall off | P1 | **implemented** - quarantined/trust/status per turn, `n_quarantined` in summary (merged, verified live) |
| F3 | Timestamp parse failure becomes "now"; locale-dependent | latent (all parse today) (E INJ-3) | raise on unparsed timestamp; explicit month-name parser; engine counter `valid_from_defaulted` | P1 | **implemented** - unparseable timestamp raises (merged, verified live) |
| F4 | Ingest cost / time not measured | per-turn deposit latency in trace is not real (E INJ-4) | record flush time and records; `ingest_s`, `turns_per_s`, `wall_clock_s` | P2 | **in progress** - ingest timing (harness agent) |
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

## Progress log

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
