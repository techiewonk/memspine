# Engineering and process gaps: MemSpine engine + LoCoMo evaluation (injection to scored answer)

Audit date 2026-10-10. Scope: everything that is neither retrieval ranking nor reader wording (see
`RETRIEVAL_GAPS.md`, `READER_GAPS.md`). Nothing was changed, run or killed; all numbers below were computed
from existing artefacts.

Artefacts used (paths relative to `C:\Users\hembad\Documents\GitHub\`):
- `memspine\evals\runs\qa-full-qs-eq06-roff-fx--forensics\{ingest,forensics}.jsonl` (5,882 turns, 1,540 questions)
- `memspine\evals\runs\qa-full-qs-eq06-roff{,-fx}--memspine\results.jsonl` (same config, two runs about 10 h apart)
- `memspine\evals\runs\qa-full-qs-eq06-rq4b4-fx`, `qa-full-qs-ejina-rjina`, `qa-full-qs-ebge-roff`, `qa-q35-think-on`
- `memspine-fixes\evals\runs\qa-full-qs-eq06-fix--memspine\results.jsonl` (the fixed run, 80.1%)
- Ollama `server-1.log` (09:22 to 22:20 IST, n_ctx 4096) and `server.log` (from 22:20 IST, n_ctx 8192)

Priority: **P0** = can invalidate a reported number or conclusion; **P1** = fix before the next headline run;
**P2** = hygiene that will bite later; **P3** = nice to have. "Known" = on the user's list (a) to (o); the entry
verifies and quantifies it. "NEW" = found in this audit.

Legend for token counters used throughout:
- **E** = engine `tokens_used` = sum of `len(content)//4 + 1` over the final (post-replay-expansion) records.
  Verified exact: recomputed from `forensics.jsonl` `context_records`, difference 0 on all 1,540 questions.
  This is what `results.jsonl` `context_tokens` reports.
- **H** = harness recount in `runner.py` (`truncate_to_budget`, `heuristic-chars4`) on the rendered text, which adds a
  `[YYYY-MM-DD] ` prefix (13 chars) and newline per record. Used only for the 4096 tail-cut decision.
- **R** = real tokenizer count of the reader prompt, `usage.prompt_tokens` from Ollama.

## Index

| ID | Area | Finding (short) | Pri |
|---|---|---|---|
| INJ-1 | injection | Write path audited: 5,882/5,882 written, text identical, dates correct, no merges (clean) | info |
| INJ-2 | injection | Quarantine / trust not in ingest log; `written` is true for quarantined rows (m, Known) | P1 |
| INJ-3 | injection | Unparsable or locale-dependent timestamp silently becomes "now" | P1 |
| INJ-4 | injection | Ingest cost and wall-clock never measured; per-turn deposit latency in trace is fiction | P2 |
| INJ-5 | injection | Day-only timestamps; naive local time stamped as UTC; ordering inside a session rests on write order | P3 |
| INJ-6 | injection | Forensics logs append, carry no run id or query id; 11 duplicate questions break text joins | P2 |
| RET-1 | retrieval plumbing | Rerank min-max + 0.3 floor removes 43% of the top-10 (e, Known, quantified) | P1 |
| RET-2 | retrieval plumbing | Replay window was hard-coded (f, Known); still absent from the manifest config | P2 |
| RET-3 | retrieval plumbing | Reranker unavailable = sticky silent skip; no run-level assertion | P1 |
| RET-4 | retrieval plumbing | Engine budget (E) is not the budget that is enforced (H) and not the model's (R) | P1 |
| RET-5 | retrieval plumbing | Mixed batched / unbatched arms in the grid: embeddings are not batch-invariant in bf16 | P2 |
| HAR-1 | harness | Tail-cut at heuristic 4096 (d, Known): E vs H vs R quantified, safe budget derived | P0 |
| HAR-2 | harness | `prompt_tokens` sums retry calls; prompt overhead is not constant (up to 2.6x E) | P1 |
| HAR-3 | harness | Truncation of a prompt by the server is invisible to the harness | P0 |
| HAR-4 | harness | Fix run changes five things at once; the headline delta is not attributable | P0 |
| HAR-5 | harness | Manifest omits server config, sampler, model digest, engine path, dirty flag (main) | P1 |
| HAR-6 | harness | Latency columns are confounded by concurrent arms on one Ollama slot | P1 |
| HAR-7 | harness | Hidden env-var controls and one-shot scripts with rc=0 on failure | P1 |
| SRV-1 | serving | n_ctx 4096 silently truncates; the think-on conclusion is an artefact (c, NEW consequence) | P0 |
| SRV-2 | serving | Sampler is not what the harness thinks: presence_penalty 1.5, repeat window 64 (NEW) | P0 |
| SRV-3 | serving | `localhost` cost (a, Known): not reproduced in recorded latencies; per-call overhead measured | P2 |
| SRV-4 | serving | New httpx client per call (b, Known): fixed in worktree only | P3 |
| SRV-5 | serving | Hybrid Qwen3.5: no prefix cache; model reloads; 237 MB debug log; flash-attn flipped | P2 |
| SRV-6 | serving | GPU shared with desktop and with embedder/reranker (o, Known) | P2 |
| EVAL-1 | methodology | Same 9B model is reader and judge; judge flips on identical answers (k, Known) | P0 |
| EVAL-2 | methodology | Judge prompt is home-made, tuned on test data; not comparable with published J scores | P0 |
| EVAL-3 | methodology | Single runs, no CI (l, Known): measured run-to-run noise and proper CIs | P0 |
| EVAL-4 | methodology | Tuned on conv-26 (in the test set); no split file exists; gain on conv-26 is 2x the rest | P0 |
| EVAL-5 | methodology | "Sufficiency" vs QA accuracy: different denominators and context sizes | P1 |
| EVAL-6 | methodology | Excluded categories and category label mapping; published comparison table | P1 |
| EVAL-7 | methodology | Truncated and refusal answers credited by judge (extends k) | P1 |
| EVAL-8 | methodology | Determinism: what is and is not reproducible (measured) | P1 |
| EVAL-9 | methodology | LoCoMo licence: 6,476 rows of question/gold text committed (n, Known) | P0 |
| ENV-1 | environment | `uv.lock` gitignored, extras unbounded: root cause of (i) and (j) | P1 |
| ENV-2 | environment | `evals/datasets` shadows HuggingFace `datasets` (g, Known): only `_launch.py` is protected | P1 |
| ENV-3 | environment | `env -u` pattern still in 6 scripts (h, Known) | P1 |
| ENV-4 | environment | CRLF/LF churn: 985 files LF in index, CRLF in working tree, no `.gitattributes` | P2 |
| PROC-1 | process | CI never runs `evals/tests` (59 files) and never installs the `st` extra | P1 |
| PROC-2 | process | No tests for the forensic and ingest-log code, the floor interaction, or the budget invariants | P1 |
| PROC-3 | process | Two diverged trees (132 files, 18.8k lines) plus duplicated run directories and 20 to 25 one-off scripts | P1 |
| PROC-4 | process | Config traps: `TOPK:-20`, `FLAGS` default on, destructive `rm -rf`, hard-coded call caps | P1 |
| PROC-5 | process | Results docs contain superseded or wrong conclusions without a pointer | P1 |

---

## 1. Injection

### INJ-1 Write path audit result (verified clean)
- **Evidence** (`ingest.jsonl`, 5,882 lines, 10 conversations): `written: true` 5,882; `text_identical: true` 5,882; unique `record_id`
  5,882; no duplicate (item, turn); 0 turns with a "now" date (no 2026 `valid_from`); each of 272 sessions has exactly one
  `valid_from`; recomputing the parse of `source_timestamp` with `strptime` gives 0 mismatches against `valid_from`
  (including `12:09 am` -> 00:09); write order equals turn-id order in every conversation (0 out-of-order); 1,226 turns
  (20.8%) carry an appended `[image: caption]`; longest turn 487 chars, mean 144.
- **Derived-session merging** (the 30 min gap rule used by the replay window): minimum gap between consecutive dataset
  sessions is 28.07 **hours**, median 147.5 h; 0 sessions merge, 0 non-monotonic session dates. Because all turns of a session
  share one stamp, a session can never be split either. 25 of 272 sessions exceed 32 turns and are written in two batches
  (harmless, order preserved). Two exact-duplicate texts exist ("John: Take care, bye!", "Jolene: See you!"); both stored.
- **Impact**: injection is not a source of the observed errors for the baseline arm. Any "never retrieved" gold is a
  retrieval or quarantine question, not a write defect.
- **Solution options**: (1) make these checks permanent post-ingest invariants in the adapter, raising on violation (cost: 1 h);
  (2) emit them as a `ingest_audit` block in `summary.json` so every run carries the proof (cost: 1 h).
- **Priority**: info (turn into P2 work below).

### INJ-2 Quarantine and trust are invisible in the ingest log (m, Known)
- **Evidence**: `memspine_system.py:404-447` writes `written: record is not None`; the engine returns a record for a quarantined
  write too (`engine.py:3351-3378` stores the event inert), so `written` is true for quarantined turns. The firewall is on by
  default (`firewall.enabled`), runs `_assess_write` (`engine.py:3396`, `core/firewall.py:259`), and a quarantined row is dropped
  at read by `_gate_hits` (`engine.py:3786`) after consuming leg slots.
- **What can be said without the DB** (static replay of the rules over `ingest.jsonl`): `minja_bridge_prefix` (first 96 chars equal
  to a different one of the previous 50 turns): 0 hits. Instruction-shaped regex: 1 hit (conv-42 D27:37), but that rule only
  quarantines external-channel or tool/assistant roles, and every LoCoMo turn is written as role `user`, channel `messages`, so it
  is not quarantined. `embedding_outlier` (nearest cosine < 0.05 among 8 neighbours) cannot be replayed without vectors; with
  normalised Qwen3 vectors on natural text it is practically impossible, but it is **unproven**.
- **Impact**: low probability, high cost if wrong (a quarantined gold turn looks like a recall failure and is also excluded from
  the replay window, since `EpisodicMemory._active` skips it, `PIPELINE_TREE.md` 2.10). It also means the headline "memory
  firewall" is evaluated on a benchmark where, by construction, it does nothing (every turn is role `user`); do not cite LoCoMo
  runs as firewall evidence.
- **Solution options**: (1) log `record.quarantined`, `trust`, `status`, `instruction_flag` per turn and add `n_quarantined` to the
  run summary; abort or warn if > 0 on a benchmark that has no adversarial turns (cost: 30 min, trivial); (2) run once with
  `firewall.enabled: false` as a control and diff `retrieved_ids` (cost: one 3 min retrieval-only screen); (3) replay the
  embedding-outlier rule offline from the LanceDB table if the DB is kept (cost: 1 h).
- **Priority**: P1.

### INJ-3 Timestamp parse failure silently becomes "now"; parser is locale dependent
- **Evidence**: `parse_turn_time` (`memspine_system.py:57`) tries `strptime` formats with `%B`, `%b` and `%p`
  (`memspine_system.py:25-31`). Those directives follow the process locale; under a non-English locale the month names fail,
  the function returns `None`, and `engine.py:3250` (`_parse_event_time(message.timestamp) or valid_from`) falls back to the
  record default factory, i.e. wall-clock now. All sessions then share a 2026 date, the 30 min session split would merge a whole
  conversation, relative-date resolution anchors on the wrong day, and the temporal leg never fires. Nothing raises. Today all
  5,882 stamps parse, so this is latent.
- **Impact**: a silent catastrophic failure mode (temporal and open-domain questions) on another machine, locale or dataset.
- **Solution options**: (1) in the adapter, raise if `turn.timestamp` is non-empty and `parse_turn_time` returns `None` (cost: 15 min);
  (2) parse with an explicit month-name table instead of `strptime` (cost: 30 min); (3) engine-side: add a counter
  `valid_from_defaulted` and surface it in `describe()` (cost: 1 h).
- **Priority**: P1.

### INJ-4 Ingest cost and time are not measured
- **Evidence**: `trace.jsonl` `kind: deposit` rows show `latency_ms` 0.002 to 0.011 and `n_records: 0, buffered: k`, because the
  adapter buffers (`batch_turns` 32) and the real write happens at the next flush; the write cost is folded into the first
  query of each session (`query()` -> `flush()`). `report/run_summary.json` has `"wall_clock_s": null` and
  `forensics_report.py:275` hard-codes `None`. `QWEN_STACK_RESULTS.md` quotes wall-clock only from shell logs.
- **Impact**: "cost per cycle", "ingest throughput" and any latency claim in the paper cannot be derived from the run artefacts;
  per-query `latency_retrieve` (p50 125 to 214 ms) is contaminated by occasional flushes.
- **Solution options**: (1) record flush time and record count in the adapter and report `ingest_s`, `ingest_turns_per_s` in the
  summary (cost: 1 h); (2) set `wall_clock_s` from the manifest `created_at` and the last row time (cost: 15 min).
- **Priority**: P2.

### INJ-5 Day-only timestamps, naive-local stamped as UTC, order inside a session = write order
- **Evidence**: all turns of a session carry the identical `valid_from` (minute resolution, e.g. `2023-05-08T13:56:00+00:00`);
  the dataset time is local-naive and is labelled UTC (`replace(tzinfo=UTC)`); 14 of 272 sessions fall within one hour of
  midnight. The reader sees only `[YYYY-MM-DD]`. Within-session ordering relies on `recorded_at` (strictly increasing write
  order, `records.py:200-236`), which holds because the adapter awaits sequentially (verified 0 out-of-order).
- **Impact**: none today. A future timezone conversion, a parallel writer, or a reader that wants time of day would shift dates
  by one day for those 14 sessions.
- **Solution options**: (1) document "dataset-local, no conversion" in the manifest (cost: 10 min); (2) assert sequential write order
  in the adapter (cost: 15 min).
- **Priority**: P3.

### INJ-6 Forensics files: append mode, no run id, no query id, duplicate questions
- **Evidence**: both `ingest.jsonl` and `forensics.jsonl` are opened with `"a"` (`memspine_system.py:419`, `:485`), the directory
  comes from the global env var `MEMSPINE_FORENSICS_DIR`, rows carry the conversation id and question **text** but no
  `run_id` or `query_id`. 1,540 forensic rows map to 1,529 distinct (item, question) pairs (11 duplicates); a text join is
  ambiguous for those. Re-running into the same directory concatenates two runs silently (the run scripts `rm -rf` only some
  dirs).
- **Impact**: stage attribution joins can mis-assign; re-runs corrupt the evidence.
- **Solution options**: (1) write `run_id` and `query_id` into every row and open with `"w"` per run (cost: 30 min);
  (2) refuse to start if the forensics dir is non-empty (cost: 15 min).
- **Priority**: P2.

---

## 2. Retrieval plumbing (engine-side mechanics, not ranking quality)

### RET-1 Min-max + relative floor deletes 43% of the reranked top-10 (e, Known, quantified)
- **Evidence** (`qa-full-qs-eq06-rq4b4-fx--forensics`): of the 10 `final` hits, a mean of **5.66** reach the context; 97.6% of
  questions lose at least one hit; mean context 19.7 records. Without a reranker (roff) all 10 of 10 survive (0.0% of questions
  lose any). Code: `_minmax_normalize` (`engine.py:931`) forces best 1.0 and worst 0.0, then `AssemblyPolicy` drops scores below
  `0.3 * best` (`assembly.py:121`). Abstention can never fire after rerank (best = 1.0).
- **Impact**: explains a good part of the "every reranker costs 1 to 3.6 points" result in `QWEN_STACK_RESULTS.md`; the reranker
  arms are not a fair test of reranking. (Paired: Qwen3-4B 4-bit rerank vs none = -3.57 points, 95% CI [-5.13, -2.01],
  McNemar p < 0.001, so the loss is real but attributable to the floor interaction until proven otherwise.)
- **Solution options**: (1) `relative_floor: 0.0` for reranker arms, or `rerank_gate`/`rerank_blend` (cost: config, one 25 min run);
  (2) normalise by raw P(yes) instead of min-max (cost: 1 h engine change + test); (3) log `n_dropped_by_floor` per query
  (cost: 30 min).
- **Priority**: P1.

### RET-2 Replay window radius (f, Known)
- **Evidence**: `replay_window=2` was a call-argument default (`engine.py:4874`), never in config or manifest; the worktree
  `7e7ee01` made it configurable and asymmetric.
- **Impact**: any run's window is not reconstructible from `config_hash` on the main branch.
- **Solution options**: (1) make the window a `read.*` config key and include it in `describe()` (cost: 1 h); (2) after merging the
  worktree, regenerate all arm JSONs with the explicit value (cost: 30 min).
- **Priority**: P2.

### RET-3 Reranker unavailable = silent sticky skip
- **Evidence**: `_rerank_provider` (`engine.py:8613-8640`): "An unavailable adapter is skip-logged ONCE and the stage disables
  itself"; at read, a rerank exception keeps the fusion scores (`engine.py:4144`). QA result rows carry `meta.reranked`
  (true for 1,540/1,540 in the rq4b4, ejina-rjina and eq06-rjina QA runs, so those arms are sound), but nothing aborts a run when
  it is false, and `huggingface-hub`/`bitsandbytes` breakage (i, j) is exactly the failure that triggers it. The retrieval-only
  grid summaries were not checked for `rerank_stats().failures`.
- **Impact**: a "reranker arm" can silently be a no-rerank arm; the conclusion "rerankers lower sufficiency" in the 16-arm grid
  is only as good as that check.
- **Solution options**: (1) runner asserts `rerank_stats().calls > 0 and failures == 0` for any arm whose config has
  `read.rerank != off`, failing the run otherwise (cost: 1 h); (2) engine flag `rerank_required: true` that raises instead of
  skipping (cost: 1 h); (3) re-audit the 16 retrieval-only summaries (cost: 15 min).
- **Priority**: P1.

### RET-4 The three token budgets disagree (engine E, harness H, model R)
- **Evidence**: see HAR-1. The engine's replay expansion budgets in E (content only), the harness enforces in H (E plus date
  prefixes), the server context window limits R.
- **Impact**: with `top_k 20` the engine filled 4096 E, H exceeded 4096 and the tail was cut (the 48.7% collapse, d).
- **Solution options**: see HAR-1.
- **Priority**: P1.

### RET-5 Batched and unbatched arms are mixed in the grid
- **Evidence**: the FINAL grid table marks arms with `*` as `--memspine-batch-turns 32`; bge-small/none, bge-small/rerankers and
  the Qwen3-Embedding arms ran unbatched (batch 1), Jina and bge-base ran batched. Document embedding is `embedding.batch_size` 32
  in both modes, but the composition of each embedding batch differs (whole 32-turn writes vs single-turn writes), and in
  bf16 with padding the vectors are not bit-identical across batch compositions. Queries are always batch 1.
- **Impact**: differences of 0.2 to 0.5 sufficiency points between arms (several are of that size) are within the plausible
  effect of batch composition on near-tie ranks. Not measured here (needs GPU).
- **Solution options**: (1) run every grid arm with one batching mode (cost: re-run the 6 unbatched arms, about 2 h GPU);
  (2) measure the effect once: run one arm at `bt1` and `bt32` and compare `retrieved_ids` (cost: 40 min GPU).
- **Priority**: P2.

---

## 3. Harness / token budget

### HAR-1 Tail truncation at a heuristic 4096 (d, Known): quantified and safe budget
- **Evidence** (`runner.py:793-796`, `tokens.py:74-96`): the harness recounts the rendered text with `heuristic-chars4` and
  cuts from the tail at 4096; the reported `context_tokens` is E, not that count.
  Baseline arm: E mean 1,571, H mean 1,684 (**+7.2%**, the date prefix), R mean prompt 2,062 (context plus a ~300 token template
  and question). Regression of R on E (1,540 points each): roff slope 1.075 (intercept 374); rq4b4 slope 1.23 (intercept 90);
  ejina-rjina slope 1.25 (intercept 79); residual +-250. So real tokens of the context are about **1.12x E on the no-rerank arm
  and 1.23x to 1.25x on the rerank arms** (short contexts, more digits and dates per token). Largest observed prompt: 2,850
  (baseline), 7,795 (fix run, see HAR-2).
  Because the final order is chronological, a cut removes the **latest** turns, not the least relevant ones, so it is also a
  systematically biased truncation.
- **Impact**: at the configured top_k 10 the 4096 budget never binds (max E 2,315), so published "budget 4096" numbers are in
  effect unbudgeted; at top_k 20 it bound and 107/152 questions were cut. The "ctx tokens per question" columns compared with
  Mem0 (~6,956), Hindsight (24.7k) etc. are E (no prefix, a chars/4 guess), not the tokenisation those systems report, so the
  token-efficiency claim has a +12 to +25% unquantified error bar plus unknown differences in the competitors' counters.
- **Safe budget**: with budget B in E units, the real context is at most about 1.07 x 1.25 x B = 1.34 B; add 0.4k template and
  the completion cap. For B = 4096: 5.5k + 0.4k + 0.5k = **6.4k**, so `n_ctx` >= 8192 (what the server runs now); for B = 6000 use
  12,288. Rule: `n_ctx >= ceil((1.35 * B + 0.5k + max_tokens) / 1024) * 1024`, and 2x when reasoning prompts (`evermemos_cot`,
  `max_tokens` 1536) are used.
- **Solution options**: (1) count with the model's own tokenizer (HF `AutoTokenizer` for Qwen3.5, loaded once; cost 1 h + a test)
  and report that as `context_tokens`; keep E as `engine_tokens` (cost: 1 h); (2) make the engine count the same string it
  returns (include the date prefix in `_render`, and budget in the same counter) so engine and harness cannot disagree
  (cost: 2 h, touches `assembly.py`); (3) when the harness has to truncate, truncate by least relevance, not by recency, and
  flag `context_truncated` loudly in the summary header (cost: 1 h); (4) assert `prompt_tokens + max_tokens < n_ctx` per
  call (see HAR-3).
- **Priority**: P0 for (1) and (4) before any token-efficiency claim.

### HAR-2 `prompt_tokens` is summed over retries; prompt overhead is not constant (NEW)
- **Evidence** (fix run): `RefusalRetryReader` (`refusal.py:76-107`) returns `prompt_tokens = first + second`. 107 of 1,540
  questions (7.0%) have `model_calls == 3` (reader twice plus judge); their accuracy is 29.0%. Mean `prompt_tokens/context_tokens`
  is 1.49 overall and reaches 2.6 for those rows (7,795 prompt tokens for a 2,989 token context). The baseline run has no
  retries: overhead min 239, max 697 tokens.
- **Impact**: "tokens per question" and cost-per-correct are inflated by retries and are not a property of the context; anyone
  regressing prompt tokens on context size (as done here) must exclude retried rows. It also means the retry silently doubles
  load on the single Ollama slot for 7% of questions.
- **Solution options**: (1) store `prompt_tokens_first`, `prompt_tokens_retry` separately in `meta` (cost: 30 min);
  (2) report cost per question with and without retries (cost: 15 min).
- **Priority**: P1.

### HAR-3 Server-side truncation is invisible to the harness
- **Evidence**: Ollama silently truncates (or stops at the window): `server-1.log` has 49 lines
  `stop processing: n_tokens = 4095, truncated = 1`. The OpenAI-compatible response gave `finish_reason: length` for these, which
  the harness maps to `answer_truncated`, but a **prompt** that exceeds `n_ctx` is cut with no signal at all (`usage.prompt_tokens`
  just equals the window). The `/v1` endpoint cannot set `num_ctx` per request; it is fixed by `OLLAMA_CONTEXT_LENGTH`, a
  Modelfile, or the native `/api/chat` `options`.
- **Impact**: any larger context (top_k 20, reasoning prompts, retries) can lose its head or tail unnoticed, which is exactly
  mechanism (d) again, one layer down.
- **Solution options**: (1) before a run, query the server (`/api/ps` or the first request's `prompt_tokens`) and abort if
  `n_ctx < required`, with required from HAR-1 (cost: 1 h); (2) per call, raise if `prompt_tokens + completion_tokens >= n_ctx - 8`
  (cost: 30 min, trivial and decisive); (3) switch to the native endpoint and pass `num_ctx` explicitly per request so the
  setting travels with the run (cost: 3 h, also records the effective value).
- **Priority**: P0.

### HAR-4 The "fix" run changes five variables at once (NEW)
- **Evidence** (manifest of `qa-full-qs-eq06-fix`): `qa_prompt: grounded`, `retry_refusal: true`, `judge_guards: true` (judge prompt
  `memspine/rubric-guarded`, which tells the judge that hedging and relative-date equivalents are CORRECT), asymmetric/wider replay
  window and date changes in the engine (`7e7ee01`), and a different server context (8192 vs 4096). Baseline vs fix: 74.48% vs
  80.13% = +5.65 points (154 questions only fixed, 67 only base). The judge change alone can move the score because the judge is
  the same 9B model and the rubric was written to forgive exactly the errors the analysis found.
- **Impact**: the 80.1% cannot be presented as "retrieval and reader fixes give +5.65"; part of it is a more lenient judge.
  Per-conversation ablations exist only on conv-26 (see EVAL-4).
- **Solution options**: (1) re-judge the **baseline answers** with the guarded rubric and the **fix answers** with the original
  rubric (`rejudge.py` exists; judge-only, about 15 min each locally, or about $0.1 with the configured Bedrock Qwen3-32B) to
  isolate the judge effect (cost: 30 min); (2) a 2x2 on the full set: {old, new engine} x {old, new reader+judge}
  (cost: 2 more 55 min runs); (3) report the judge-controlled number as the headline.
- **Priority**: P0.

### HAR-5 Run manifest omits what actually determines the result (NEW)
- **Evidence**: manifest `code` = `{memspine_git: c0ab450, evals_version: 0.2.0}` (no `-dirty` marker in the main-tree runs, a
  `-dirty` marker in the fix run: `8a813a9-dirty`), `env` = python and platform only, `system.version` = "0.0.1" (hard-coded
  package version). Missing: Ollama version, model digest (the log shows `sha256-02d45dc1...`), quantisation, `n_ctx`, `num_parallel`,
  flash-attention state, sampler (SRV-2), GPU name/driver, torch/transformers/sentence-transformers versions, the path the
  `memspine` module was imported from (the worktree runs rely on `PYTHONPATH=<worktree>/src` over a venv that has the main tree
  installed; a missing export silently runs the main engine under the fix harness), environment overrides (HAR-7), and the CLI
  argv. The fx runs recorded `base_url: http://localhost:11434/v1` although `run_qwen_qa_full.sh` passes `127.0.0.1`.
- **Impact**: two runs with the same `config_hash` can differ in server context window (4096 vs 8192, both on 2026-10-09) and
  flash-attention; results cannot be re-derived from the manifest.
- **Solution options**: (1) add a `runtime` block: `memspine.__file__`, versions of the 6 key packages, `git describe --dirty`,
  argv, selected `MEMSPINE_*` env, and a call to Ollama `/api/version`, `/api/show`, `/api/ps` (cost: 2 h, and the single most useful
  reproducibility change); (2) fail if the git tree is dirty unless `--allow-dirty` (cost: 15 min).
- **Priority**: P1.

### HAR-6 Latency numbers are confounded by concurrency (NEW)
- **Evidence**: the roff-fx and rq4b4-fx runs executed at the same time against one Ollama slot (`-np 1`). Server-side per-call time
  (Ollama GIN log, chat/completions, 200): median **0.207 s** during the earlier solo run (12:00 to 13:00 IST) vs **0.747 s** while
  both ran (20:36 to 22:06 IST), 3.6x. Harness `latency_answer` p50 1.02 s (solo) vs 1.59 s (shared); whole run 3,262 s vs about 5,400 s.
  `QWEN_STACK_RESULTS.md` compares "Answer p50 / p95" and "Run time" across arms that did not run under the same load.
- **Impact**: speed claims such as "reranking cuts run time 20%" are partly queueing noise.
- **Solution options**: (1) report server-side time-to-first-token and decode time from the response (`usage`/Ollama `timings`) rather
  than wall time (cost: 1 h); (2) run latency comparisons serially on an idle GPU, or record concurrent-arm count in the manifest
  (cost: 15 min); (3) add a short dedicated latency bench with fixed prompts (`bench_llm.py` exists).
- **Priority**: P1.

### HAR-7 Hidden controls and scripts that report success on failure
- **Evidence**: behaviour is switched by env vars that never appear in the manifest: `MEMSPINE_EVAL_THINK` (changes `max_tokens`
  to 4096 and timeout to 600 s), `MEMSPINE_FORENSICS_DIR`, `MEMSPINE_EVAL_MAX_QUERIES`, `PYTORCH_CUDA_ALLOC_CONF`. Scripts: of 20
  `evals/run_*.sh`, 19 contain neither `set -e` nor `PIPESTATUS`; `run_qwen_stack.sh` prints `rc=` but **exits 0 always** (last
  command is `echo`); `run_qwen_qa_full.sh` appends "QA FULL DONE" even if every run failed, and starts with
  `while ! grep -q "STAGE2 DONE" ...; do sleep 60; done`, which blocks forever if the sentinel never appears; `run_next.sh` ends with
  an unconditional `echo NEXT DONE` after `cd ... && rm -rf runs/...-fx*` (the `rm -rf` deletes prior results without asking);
  `--arm` files are read with `$(cat arms/$arm.json)`, so a typo yields `--memspine-config ""` rather than an error.
  The earlier `--items 1` smoke test did not limit the query set and `--max-model-calls 150` then produced "all 0.000 scores"
  (`QWEN_STACK_RESULTS.md`) instead of an invalid-run flag.
- **Impact**: a crashed or aborted run looks identical to a finished one in the shell log; the (h) incident (`env -u ... python`
  doing nothing, rc=0, empty logs) is a symptom of this class.
- **Solution options**: (1) one `evals/run.sh` wrapper with `set -euo pipefail`, `arm` existence check, `trap` that writes a
  `STATUS` file (`ok`/`failed rc=N`), and a post-run check that `results.jsonl` row count equals the expected query count
  (cost: 2 h, replaces most of the 20+ scripts); (2) record all `MEMSPINE_*` env vars in the manifest (cost: 15 min);
  (3) make an aborted or call-capped run exit non-zero and stamp the headline `INVALID` (cost: 1 h).
- **Priority**: P1.

---

## 4. Inference serving (Ollama)

### SRV-1 n_ctx 4096 truncated the think-on arm; the "thinking does not help" conclusion is an artefact (c, new consequence)
- **Evidence**: `server-1.log` ran with `-c 4096 -np 1`. The think-on run (`qa-q35-think-on`, 152 questions) configured
  `max_tokens 4096`. All **49** truncated answers have `prompt_tokens + completion_tokens` between 4,090 and 4,096 (49/49 rows;
  completion tokens 1,645 to 2,379, not 4,096) and the log contains exactly 49 matching lines `n_tokens = 4095, truncated = 1`
  between 09:44 and 10:13 IST. They were cut by the **context window**, not by the completion cap, roughly half way through their
  reasoning. `QWEN_STACK_RESULTS.md` says "The 49 truncated answers hit the token limit".
- **Impact**: the table "thinking on: 0.592, 49 truncated, 5.7x run time, no gain" and the statement "Thinking costs 5.7x ... for no
  measured gain" compare think-off to a think-on that was handicapped on exactly the questions that need long reasoning.
  The matched-103 comparison (0.806 vs 0.806) is also biased toward questions where reasoning was short. The conclusion is
  **untested**, not refuted.
- **Solution options**: (1) re-run think-on for the 49 truncated questions only with `OLLAMA_CONTEXT_LENGTH` 16384 (cost: about 49 x 15 s,
  12 min); (2) amend `QWEN_STACK_RESULTS.md` now with a "confounded" marker (cost: 5 min); (3) apply HAR-3 so this cannot recur.
- **Priority**: P0.

### SRV-2 The effective sampler is not "temperature 0 only" (NEW)
- **Evidence**: llama-server `sampler params` for all 6,341 requests of the current server instance: `temp 0.000, top_k 20, top_p 1.0,
  presence_penalty 1.500, repeat_penalty 1.0, repeat_last_n 64, dry_multiplier 0` (3 warm-up requests show top_p 0.95). The harness sends
  only `temperature` and `max_tokens` (`readers.py:481-500`); presence penalty 1.5 comes from the model's default parameters. In
  llama.cpp the penalty window (last 64 tokens) includes the **tail of the prompt**. For the judge, whose prompt ends with
  `Respond with JSON only: {"label": "CORRECT"} or {"label": "WRONG"}`, the very tokens it must emit are inside that window.
  Circumstantial symptom: the judge's raw output alternates between `{"label": "CORRECT"}` and `{"label":"CORRECT"}` (no space):
  8.6% of rows in the fx baseline run, 28.1% (432/1,540) in the fix run, i.e. token-level behaviour changes with the prompt.
  The reader is penalised for repeating any token (names, dates) that sits in the last 64 tokens of the prompt/answer.
- **Impact**: unknown sign and size; it affects reader and judge identically across arms (so arm comparisons are less affected) but it
  makes absolute numbers non-portable (other servers use penalty 0) and it is exactly the kind of hidden default that makes a
  published comparison with GPT-4-class judges meaningless.
- **Solution options**: (1) send `presence_penalty: 0`, `top_p: 1`, `seed` explicitly in every request and record them (cost: 30 min); (2) measure the effect: re-judge 300
  stored answers with penalty 0 and compare labels (cost: 10 min GPU); (3) set the same values in a Modelfile derived from the base model
  so the server default is also correct (cost: 10 min).
- **Priority**: P0 (cheap to fix; do it before the next run).

### SRV-3 `localhost` cost (a, Known): measured overhead differs from the quoted 2.1 s
- **Evidence**: the roff run (manifest `base_url: http://localhost:11434/v1`): server-side per-call median 0.207 s vs harness
  `latency_answer` p50 1.02 s and `latency_judge` p50 0.58 s, i.e. about 0.6 to 0.8 s of client-side overhead per call (name resolution
  plus a new client, SRV-4); the whole run took 3,262 s = 2.12 s per question for retrieve + answer + judge, which may be the origin of the
  "2.1 s" figure. The per-request 2.1 s IPv6 fallback itself is not visible in the recorded latencies of the runs inspected. The code
  defaults are now `127.0.0.1` (`cli.py:324`, `experiments.py:79`, `readers.py:433`), but two runs still recorded `localhost`.
- **Impact**: wall-clock comparisons across runs made before and after the switch are not like for like.
- **Solution options**: (1) add a 20-line micro-benchmark (`bench_http.py`: 50 requests to `localhost` vs `127.0.0.1`) and keep its output
  in the repo (cost: 30 min); (2) refuse `localhost` in `base_url` on Windows with a warning (cost: 15 min).
- **Priority**: P2.

### SRV-4 A new httpx client per call (b, Known)
- **Evidence**: `readers.py:499` and `:537` (main tree) create `httpx.AsyncClient` inside every call (about 130 ms); the worktree
  (`8a813a9`) has `_shared_client` pooled per loop and timeout.
- **Impact**: about 0.13 s x 2 calls x 1,540 = 7 min per full run, plus connection churn.
- **Solution options**: merge the worktree change (cost: part of PROC-3).
- **Priority**: P3.

### SRV-5 Server behaviour that changes results or cost without showing in the harness (NEW)
- **Evidence**: (1) Qwen3.5 is a hybrid/recurrent model; the log repeatedly says `forcing full prompt re-processing due to lack of cache
  data`, and `cached_prompt_tokens` is 0 on all rows, so no prefix cache helps even though the question template repeats. (2) The model
  was reloaded 7 times on 2026-10-09 (`starting llama-server` at 09:23, 09:38, 11:47, 18:52, 19:58, 20:01, 20:35) because of the 5 minute
  default `OLLAMA_KEEP_ALIVE`; each reload costs tens of seconds and lands inside timed runs. (3) `server-1.log` is 237 MB (`--log-verbosity 4`),
  `server.log` 77 MB. (4) flash attention is `auto` in the 4096 server and `enabled` in the 8192 server (different kernels, so not bit-
  identical). (5) One `500` response at 22:18:41 (an in-flight call during the 22:20 restart), which the harness would have to handle.
- **Impact**: timing noise, and a run that spans a restart mixes two server configurations.
- **Solution options**: (1) `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_CONTEXT_LENGTH=<explicit>` set in one `ollama_env.ps1` and recorded in the manifest (cost: 20 min);
  (2) lower log verbosity (cost: trivial); (3) retry-with-backoff on 5xx and count retries (cost: 1 h).
- **Priority**: P2.

### SRV-6 GPU contention (o, Known)
- **Evidence**: Ollama reports `total 15.9 GiB, free 14.6 GiB` at each model load (so desktop usage is about 1.3 GiB at those moments; the 2.4 to
  3 GB dwm figure is higher than the free VRAM Ollama saw). The embedder (Qwen3-Embedding-0.6B bf16), the 4-bit 4B reranker and the 9B reader
  share this GPU with the desktop; the two fx arms ran concurrently (HAR-6).
- **Impact**: out-of-memory stalls become silent retries; timing noise.
- **Solution options**: (1) record `nvidia-smi --query-gpu=memory.used,memory.total` at run start and end in the manifest (cost: 20 min);
  (2) run arms strictly serially, or at most one retrieval-only arm beside one QA arm (cost: scheduling only); (3) lower `PYTORCH_CUDA_ALLOC_CONF` fragmentation with `expandable_segments` (already used in `run_next.sh`).
- **Priority**: P2.

---

## 5. Evaluation methodology

### EVAL-1 Reader and judge are the same 9B model (k, Known), and the judge is nondeterministic
- **Evidence**: reader `qwen3.5:9b`, judge `qwen3.5:9b` (manifest). Between two same-config runs the judge gave different verdicts on
  **identical answers** for 2 of 1,540 questions (EVAL-8). `qa-q35-think-on` shows the judge crediting empty answers (7 of 49). In the
  baseline runs 11 answers match refusal phrases ("not mentioned", "cannot be determined", ...) and the judge credited 1 of them; across the
  baseline, bge, jina and 4B runs 0 to 3 refusal-like answers are credited (the rubric says refusals are WRONG unless the gold is an
  abstention).
- **Impact**: self-preference and correlated errors (the same model that misreads a date also misjudges it); the absolute level is unknown
  to within several points.
- **Solution options**: (1) re-judge every stored answer with a different, stronger judge (Bedrock Qwen3-32B is already priced in the manifest
  at $0.16/$0.62 per Mtok; 1,540 judge prompts of about 250 tokens is about 0.4M tokens, **under $0.10** per run, `rejudge.py` exists)
  (cost: 1 h); (2) hand-label 150 stratified rows and report judge accuracy and Cohen's kappa (cost: 2 h of human time);
  (3) report both judges and the disagreement set.
- **Priority**: P0 for any externally quoted number.

### EVAL-2 The judge prompt is home-made and tuned on the test data; comparison with published numbers is invalid
- **Evidence**: `judge.py:RUBRIC_BINARY_PROMPT` is ours ("agreed with hand labels on an 8-case calibration set"); the guarded version
  (`RUBRIC_GUARDED_BINARY_PROMPT`) was written from `READER_GAPS.md`, i.e. from errors observed on these same 1,540 questions, and tells the
  judge to accept hedged answers and relative-date equivalents. The published figures in `QWEN_STACK_RESULTS.md` (Mem0 92.5, Mnemon 91.7,
  MemOS 88.8, Hindsight 82.0, ...) were produced with each vendor's own judge model and prompt (mostly GPT-4o-mini with the Mem0 J-score
  prompt, which grades generously) and some are vendor-reported, not reproduced. The doc already says "NOT directly comparable", but
  the table sits next to ours and the Findings text ranks us against them.
- **Impact**: the headline comparison "74.4 / 80.1 vs published" is not a like-for-like claim in either direction.
- **Solution options**: (1) run the Mem0 official J-score judge prompt (verbatim; `official_prompts.py` / `judge_prompts.py` carry a registry
  with `official-verbatim` status) with a strong judge on our stored answers, and publish that number as the comparable one (cost: 2 h and
  < $1); (2) freeze the judge prompt before looking at results and keep a separate calibration set that is not LoCoMo-test (cost: process);
  (3) never place published numbers in the same table as ours without an "unreproduced, different judge" column.
- **Priority**: P0.

### EVAL-3 Single runs, no confidence intervals (l, Known): what the data say
- **Evidence**: (i) **Run-to-run noise at temperature 0 is tiny**: roff vs roff-fx (same config, 10 h apart): `retrieved_ids` identical on
  1,540/1,540; answers identical on 1,533 (7 differ); scores differ on 2 (74.35% vs 74.48%); both score differences are judge-only on identical
  answers. So sampling noise is about +-0.1 point. (ii) **Sampling error over questions is large**: single-arm binomial 95% CI +-2.2 points;
  conversation-cluster bootstrap for the fix run 80.1% [77.7, 82.7]; per-conversation SD 4.5 points. (iii) **Paired tests are far tighter than
  overlapping unpaired CIs suggest**: Qwen3-Emb vs bge-small, +2.53 points, 95% CI [0.84, 4.29], McNemar p = 0.0046 (110 vs 71 discordant);
  Qwen3 vs Jina reranker arm +3.2 [1.5, 4.9]; vs 4B reranker +3.6 [2.0, 5.1]; vs jina/jina +3.4 [1.5, 5.3]. These ignore judge noise
  and conversation clustering; the repository already has `significance.py` with cluster bootstrap and `test_significance_cluster.py`.
- **Impact**: the ranking embedder > reranker is statistically supported on the question level; claims below 2 points between embedders
  or any category-level claim (open-domain has n = 96, +-9 points) are not.
- **Solution options**: (1) make `summary.json` always carry the cluster-bootstrap CI and paired-vs-baseline test (cost: 1 h, code exists);
  (2) cheap variance measurement: re-run only the judge twice over stored answers (about 12 min each) to measure judge-only noise, and one
  reader re-run on 300 stratified questions (about 8 min) for reader noise; the existing duplicate pair already shows both are small
  (cost: 30 min); (3) never print a bare percentage without CI in result docs (cost: process).
- **Priority**: P0.

### EVAL-4 Tuning on conv-26, which is in the test set; no dev/held-out split exists
- **Evidence**: the worktree ablation scripts (`run_ablation*.sh`) use conv-26; `find` finds no `*split*.json` in either tree; all
  retrieval-grid choices (embedder, floor, window, `candidate_pool`) were made while looking at the full 10 conversations.
  Per-conversation gain of the fix run over baseline: conv-26 **+10.5**, conv-41 +11.2, then +6.7, +6.3, +6.0, +5.1, +3.2, +2.5, +1.2, +0.8
  (overall +5.65; excluding conv-26 +5.1). The one-conversation ablation (84.2%) vs full run 80.1% matches conv-26 83.6% in the full run:
  conv-26 is simply an easy and high-gain conversation, and a 152-question sample has +-3 points standard error.
- **Impact**: the effect of the fixes is overstated by selecting on the conversation they were designed on (about 0.5 points overall,
  about 5 points on that conversation); the "fixed" 80.1% is tuned-on-test.
- **Solution options**: (1) use the existing `memspine-evals split --dataset locomo` to reserve held-out conversations and **save the file in the
  repo**; because only 10 conversations exist, use a 2-fold or 5-fold cross-fit (tune on A report B and vice versa) and put conv-26 in the
  development side explicitly (cost: 1 h + reruns of the final config once on held-out); (2) external validation on a different benchmark
  with adapters already in the repo (LongMemEval 40-history sample, LoCoMo-Plus) so the headline is on data nothing was tuned on
  (cost: 1 to 2 h per run); (3) freeze a pre-registration note like `prereg/G24_gliner2_planner.md` before the next tuning round
  (cost: 30 min).
- **Priority**: P0.

### EVAL-5 "Sufficiency" and QA accuracy measure different things on different denominators
- **Evidence**: the retrieval grid uses all 1,986 questions (incl. 446 category-5 adversarial) and a context-contains-gold criterion;
  QA uses 1,540. Sufficiency rises with context size (bge-small/none 1,508 tokens 0.594 vs Qwen3/none 1,567 tokens 0.615 vs reranker arms
  650 to 1,000 tokens 0.575 to 0.602); the replay expansion adds about 29 neighbour records (39 per question without rerank), so
  "retrieval sufficiency" is mostly "context window size". It is also not R@k (cat 5 has no R@k gold per manifest notes).
  The QA rank of the arms (Qwen3 74.4 > bge 71.9 > jina 71.0) matches the sufficiency rank only loosely.
- **Impact**: choosing arms by sufficiency can pick the largest context rather than the best retrieval.
- **Solution options**: (1) report sufficiency at matched context tokens (truncate to the same R) or sufficiency per 1k tokens (cost: 1 h);
  (2) add R@10 on the engine's own top-10 (before expansion) as a second column (forensics `final`) (cost: 1 h);
  (3) compute sufficiency on the same 1,540 questions as QA (cost: 15 min).
- **Priority**: P1.

### EVAL-6 Excluded categories and category labels; comparison hygiene
- **Evidence**: category 5 (446 questions) is excluded from QA, as the Mem0 evaluation does, and the harness keeps an `abstention-v1` judge
  for it but it is not in the headline. Category ids: cat1 = multi-hop (n=282), cat2 = temporal (321), cat3 = open-domain (96), cat4 =
  single-hop (841); the harness labels match the dataset (`test_locomo_category_counts.py`), but vendors' papers have swapped
  names for cat1 and cat3 in the past, so the by-category columns of the "published" table must be checked entry by entry. Open-domain
  is only 6.2% of the questions and reads 20 to 29% here.
- **Impact**: a category-wise comparison with a published number can be a comparison of different categories; abstention behaviour (a
  stated memory property) is not measured at all in the headline.
- **Solution options**: (1) add an abstention row (cat 5, abstention judge) as a separate reported metric (cost: one 10 min run);
  (2) put the category mapping and n in every results table header (cost: trivial); (3) verify each published entry's category definition
  from its source before putting it in a column.
- **Priority**: P1.

### EVAL-7 Truncated and refusal-like answers are credited by the judge (extends k)
- **Evidence**: rq4b4 run: 2 of the 3 `answer_truncated` rows were scored 1.0; jina run: 1 of 2; `make_sota_table.py` counts truncated as a
  miss, but `summary.json`/`run_summary.json` headline (0.7448, 0.7091, ...) does not, so two different "accuracy" numbers exist per run
  (`QWEN_STACK_RESULTS.md` quotes the table version in some places and the harness version in others). The empty-answer guard exists only in
  the fix branch (`--judge-guards`) and is opt-in.
- **Impact**: 0.1 to 0.2 points of inconsistency; more importantly, two definitions of the headline number.
- **Solution options**: (1) one scoring function in `results.py`, used by the harness and by `make_sota_table.py`, with truncation = miss
  by default (cost: 1 h); (2) default `--judge-guards` empty-answer rule on while keeping the lenient rubric opt-in (cost: 15 min).
- **Priority**: P1.

### EVAL-8 Determinism: what is and is not reproducible (measured)
- **Evidence**: retrieval is bit-reproducible here (identical `retrieved_ids` for 1,540/1,540 across two runs; ties are broken by
  (recorded_at, fingerprint, id), `core/ties.py`), the embedder runs bf16 CUDA with `embedding.batch_size` 32. Generation: temperature 0,
  `-np 1` (one slot, so no cross-request batching), but the sampler carries presence penalty 1.5 (SRV-2) and the seed is not sent (the
  manifest `seed: 11` is not forwarded to the server). 7 of 1,540 answers differ between identical runs and 2 judge verdicts differ on
  identical answers, which points to numerical non-determinism in the server (prompt processing batch composition, checkpoint
  restores after `forcing full prompt re-processing`, flash-attention state) rather than sampling. Different server configs (4096 vs 8192
  context, flash-attention auto vs enabled) were used for the baseline and the fix runs, so even "same config" claims across those runs
  carry that difference.
- **Cheap variance measurement**: (1) re-judge stored answers twice (judge-only noise, 12 min each); (2) re-run the reader on a fixed 300
  question stratified subset twice (8 min each); (3) one `bt1` vs `bt32` retrieval-only comparison (RET-5). Together under 1 h of GPU and
  they bound all three noise sources; the existing duplicate pair already shows <= 0.15 point for the whole chain.
- **Solution options**: pass `seed`, `presence_penalty`, `top_p` explicitly (SRV-2); fix the server config per campaign (SRV-5); keep one
  "canary" 150-question set that is re-run at the start of every session and must reproduce within 1 point, as a drift alarm (cost: 1 h).
- **Priority**: P1.

### EVAL-9 LoCoMo licence: result files embedding corpus text are committed (n, Known)
- **Evidence**: `git ls-files evals/results_qwen_stack` includes six `results.jsonl` files with 6,476 rows, each holding `question`, `gold`
  and `answer` text from LoCoMo; `evals/.gitignore` ignores only `runs/`, `reports/` and `*locomo*.json`, and its own header states "a committed
  result file is a number nobody can re-derive" and "LoCoMo is Snap Inc.'s". The manifest `licence` field reads "see LoCoMo release (check
  before publishing a number)". `evals/analysis/*.md` and `docs/PIPELINE_TREE.md` quote names and a few question strings (short quotes, lower
  risk). LoCoMo is, to my knowledge, released under a non-commercial licence (CC BY-NC 4.0); verify before relying on this.
- **Impact**: a redistribution problem if the repository is made public with those files; also contradicts the repository's own policy.
- **Solution options**: (1) keep only `summary.json` and `COMPARISON.md` under `results_qwen_stack/` and `git rm --cached` the `results.jsonl`
  files (cost: 10 min; history rewrite only if the repo is already public); (2) publish a stripped JSONL (query_id, score, tokens, latency,
  retrieved turn ids) which carries no corpus text (cost: 1 h); (3) add a CI check that fails if a tracked file contains the dataset's
  turn strings (cost: 1 h).
- **Priority**: P0 if the repository will be shared.

---

## 6. Environment and tooling

### ENV-1 No lock file, unbounded extras: root cause of the (i) and (j) breakages
- **Evidence**: `.gitignore` lists `uv.lock`; `pyproject.toml` extra `st = ["transformers>=4.51", "torch>=2.4", "sentence-transformers>=3",
  "bitsandbytes>=0.45"]` has no upper bounds; the installed set is `torch 2.11.0+cu128`, `transformers 5.16.1`, `huggingface-hub 1.33.0`,
  `sentence-transformers 6.1.0`, `bitsandbytes 0.50.2`, `lancedb 0.37.1`, `datasets 5.1.0`. A `huggingface-hub` 2.x release broke `transformers`
  (i); a CUDA torch wheel is not what `uv sync` gives by default (j). The CI installs only `--extra dev`, so it can never see either failure.
  (The venv inspected has CUDA torch installed; this audit only used CPU libraries, so "CPU only" in the brief refers to what was run here.)
- **Impact**: the environment that produced today's numbers cannot be rebuilt from the repo.
- **Solution options**: (1) commit a lock file for an `evals` environment (remove `uv.lock` from `.gitignore` or keep a separate
  `evals/uv.lock`) and pin `huggingface-hub<2` until `transformers` supports it (cost: 1 h); (2) a `just evals-setup` recipe that installs the CUDA
  torch index explicitly (`--index-url .../cu128`) and prints versions (cost: 30 min); (3) `pip freeze` into every run directory
  (HAR-5).
- **Priority**: P1.

### ENV-2 `evals/datasets` shadows HuggingFace `datasets` (g, Known): protected only where `_launch.py` is used
- **Evidence**: `evals/datasets/` (tracked: `__init__.py`, `base.py`, `locomo.py`, `longmemeval.py`) is a legacy package; `evals/conftest.py` inserts
  `evals/` at `sys.path[0]` for the test suite; `_launch.py` removes it for the harness. Other entry points in `evals/` run as scripts with
  `sys.path[0] = evals/`: `bench_models.py`, `bench_llm.py`, `forensics_report.py`, `build_html_report.py`, `screen_compare.py`,
  `significance.py`, `rehearse.py`. Any of them that triggers `import datasets` (sentence-transformers does) gets the wrong package.
  The tests under `evals/tests` also run with the shadow in place.
- **Impact**: intermittent, import-order dependent failures (LanceDB converter registration) that differ by entry point.
- **Solution options**: (1) rename `evals/datasets` to `evals/legacy_datasets` (or delete; the registry strings `evals.datasets.*:adapter` in
  `base.py:708-712` need updating) (cost: 1 h); (2) add a test that `import datasets; datasets.__file__` is not under `evals/` (cost: 15 min);
  (3) keep `_launch.py` but call it from all scripts (cost: 30 min).
- **Priority**: P1.

### ENV-3 `env -u VAR python` is still in six scripts (h, Known)
- **Evidence**: `run_v32_chain.sh`, `run_v32_screens.sh` and `run_v32_screens2` to `5` (both trees) use `env -u`; in Git Bash on Windows this
  silently did nothing (empty logs, rc=0). `run_qwen_stack.sh` and `run_fix_qa.sh` were already moved to `unset`.
- **Impact**: any rerun of those chains would repeat the (h) incident, and AWS credentials intended to be unset stay set (paid-call
  leakage risk, the reason `unset AWS_*` is there).
- **Solution options**: (1) replace with `unset` in a subshell, or retire these scripts (cost: 30 min); (2) post-run check that the log is
  non-empty and the run directory has `results.jsonl` (HAR-7).
- **Priority**: P1.

### ENV-4 CRLF / LF churn
- **Evidence**: `git config core.autocrlf` = true, no `.gitattributes`; `git ls-files --eol`: **985 files index LF / working tree CRLF**,
  42 LF/LF, 1 mixed; `engine.py` and `runner.py` are CRLF in the working tree. Shell scripts checked (`run_qwen_stack.sh`, `_launch.py`) are LF,
  but nothing guarantees it for new ones (a CRLF shell script fails with `\r: command not found`). Tools that rewrite files (editors, agents)
  can flip endings and produce whole-file diffs; line numbers in `PIPELINE_TREE.md` survive because endings do not change line counts.
- **Impact**: noisy diffs, merge conflicts between the two trees (PROC-3), and shell-script breakage.
- **Solution options**: (1) add `.gitattributes` with `* text=auto eol=lf` and `*.sh text eol=lf`, then `git add --renormalize .` in one dedicated
  commit (cost: 30 min); (2) set `core.autocrlf=input` locally (cost: 1 min).
- **Priority**: P2.

---

## 7. Process

### PROC-1 CI does not run the evals tests and cannot see the evals environment
- **Evidence**: `pyproject.toml` `testpaths = ["tests"]`; `evals/tests` has 59 test files and is not collected by `uv run pytest -q`;
  `ci.yml` runs `uv sync --extra dev` only (no `st`, `ner`, torch) and mypy only on `src/memspine`. Ruff does cover `evals/`.
- **Impact**: harness regressions (token counter, judge parsing, truncation, split, significance) can merge unnoticed; the GPU paths are never
  exercised.
- **Solution options**: (1) add `evals/tests` to a second CI job (`pytest evals/tests`) with the stub reader/judge (`stub_llm.py` exists)
  (cost: 1 h); (2) a nightly or manual job with the `st` extra on CPU running a 2-conversation, 20-question smoke with a golden
  `retrieved_ids` file (cost: 3 h); (3) `mypy` on `evals/memspine_evals` (cost: variable).
- **Priority**: P1.

### PROC-2 Missing tests for the new forensic and injection-audit code, and for the interactions that hurt
- **Evidence**: no test references `_write_ingest_log`, `_write_forensics`, `MEMSPINE_FORENSICS_DIR`, `search_forensics` (the engine ContextVar sink at
  `engine.py:653-672`, hooked at `:4017`, `:4105`, `:4123`, `:4185`), `forensics_report.py` or `build_html_report.py` (the single grep hit,
  `forensic` in `tests/unit/test_engine_integrity.py`, is an unrelated fixture name). Also untested: min-max + floor interaction (RET-1), harness
  tail truncation vs engine budget (HAR-1), `parse_turn_time` failure path (INJ-3), `prompt_tokens + completion < n_ctx` (HAR-3), manifest completeness (HAR-5),
  the shell scripts (HAR-7).
- **Impact**: the forensic tooling is now the basis for gap analyses; a silent bug in it (wrong join, wrong stage label) would misdirect effort.
- **Solution options**: (1) unit tests with the stub engine: ingest log has one line per turn and `quarantined` field; forensic row has all stages;
  `search_forensics` is a no-op when unset (zero overhead) (cost: 2 h); (2) schema validation of every produced row against
  `evals/schemas/*.json` inside the tests (the schemas already exist) (cost: 1 h); (3) property test: `E <= H <= R/1.0` ordering on sample texts
  with the real tokenizer, so the safety factor in HAR-1 is checked (cost: 1 h).
- **Priority**: P1.

### PROC-3 Two diverged trees and duplicated artefacts
- **Evidence**: main `memspine` on `feat/local-qwen-stack` (HEAD `1004d02`) and worktree `memspine-fixes` on `feat/locomo-fixes` (HEAD `8a813a9`):
  `git diff main..HEAD --stat` in the worktree = **132 files, +18,787 / -60**. Runs and forensics exist in both `evals/runs` directories
  (e.g. `qa-full-qs-eq06-fix--forensics` only in the worktree; `qa-full-qs-eq06-roff-fx*` and `rq4b4-fx*` only in main, produced by the main tree's code
  but launched from the worktree script `run_next.sh`). `evals/` holds 20 (main) to 25 (worktree) `run_*.sh` variants with near-identical bodies, `run_next.sh` is untracked in the main
  tree, and run ids encode state by suffix (`-fx`, `-fix`, `-n2`, `-bt32`) rather than by recorded config.
- **Impact**: the comparison "baseline vs fix" crosses two code trees, two Ollama configs and two sets of run folders; easy to compare the wrong pair.
- **Solution options**: (1) merge the worktree into the evals line behind opt-in flags it already uses, delete the duplicated scripts, keep one
  `evals/runs` and a `runs/INDEX.md` generated from manifests (cost: 3 to 4 h); (2) tag every campaign commit (`git tag eval-2026-10-09-baseline`) and record the tag in the
  manifest (cost: 10 min); (3) until merged, a one-page `CAMPAIGN.md` listing which run lives where with its git hash (cost: 30 min).
- **Priority**: P1.

### PROC-4 Config traps in the run scripts
- **Evidence**: `run_fix_qa.sh` defaults `--top-k "${TOPK:-20}"` (protocol is 10; top_k 20 is the setting that collapsed to 48.7%) and `FLAGS=${FLAGS-"--qa-prompt
  grounded --retry-refusal --judge-guards"}` (fixes on by default, so a forgotten `FLAGS=` gives a non-baseline run named like a baseline); `run_next.sh`
  passes `TOPK=10` explicitly only for the first run; `--max-model-calls` is hard-coded (4000 in `run_qwen_qa_full.sh`, 6000 in `run_fix_qa.sh`) while the
  need is 2 x 1,540 + retries; the arm file is not validated; `rm -rf runs/qa-full-qs-eq06-rq4b4-fx*` inside a chain.
- **Impact**: wrong-config runs that look valid; destructive reruns.
- **Solution options**: (1) one wrapper that takes explicit `--topk`, `--flags`, derives the call cap from the expected query count and fails on a missing arm (cost: part of HAR-7);
  (2) print and store the resolved command line at the top of the log (cost: 10 min); (3) refuse to overwrite an existing run id unless `--force` (cost: 15 min).
- **Priority**: P1.

### PROC-5 Results docs mix superseded or confounded conclusions
- **Evidence**: `QWEN_STACK_RESULTS.md` still contains "Not yet valid", "pending" rows, the think-on conclusion that SRV-1 shows is confounded, headline accuracy figures from
  two definitions (EVAL-7), published comparison rows (EVAL-2), and the correction that appeared after the original number ("Correction (judge artifact)"). There is no single
  "current best understanding" block and no mention of the fix run (80.1%) or the n_ctx finding.
- **Impact**: the next reader (or paper section) will quote the wrong row.
- **Solution options**: (1) a "Status of each claim" table (claim, evidence run ids, CI, caveat, confounded?) at the top (cost: 1 h); (2) mark SRV-1 now; (3) generate result tables from
  manifests with the CI and the judge id printed next to every number (cost: 2 h with `make_sota_table.py` as the base).
- **Priority**: P1.

---

## Suggested order of work (all discussable item by item)

1. **Make every run self-checking** (no GPU, about half a day): HAR-3 (n_ctx guard per call), SRV-2 (explicit sampler), HAR-5 (runtime block in the manifest), INJ-2 + INJ-3 (quarantine log, raise on unparsable stamps), HAR-7/PROC-4 (one safe wrapper).
2. **Re-score what exists without new GPU generation** (about 1 h plus a few cents): EVAL-1/2 (strong judge, official J prompt on stored answers), HAR-4 (judge-controlled comparison via `rejudge.py`), EVAL-3 (CIs into every summary), EVAL-7 (one headline definition).
3. **Fix the two wrong conclusions in the docs**: SRV-1 (think-on is confounded; re-run the 49 questions at n_ctx 16384) and RET-1 (reranker arms are floor-limited; rerun with `relative_floor: 0`).
4. **Split before tuning again**: EVAL-4 (saved dev/held-out or cross-fit, plus an external benchmark).
5. **Housekeeping**: EVAL-9 (remove committed corpus text), ENV-1 (lock), ENV-2 (rename `evals/datasets`), PROC-1/2/3 (CI, tests, merge the trees), ENV-4 (`.gitattributes`).
