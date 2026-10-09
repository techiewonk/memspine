# memspine evals — any system × any dataset × one fixed protocol

Outside the wheel (D-35). Nothing here ships, nothing here is importable from `memspine`, and the
core runs with **stdlib only** — the baselines need no dependencies at all, which is what makes them
usable as a floor and a ceiling in an environment where the engine is not installed.

Research plan of record: `../../PLAN_A_EVIDENCE_REFRESH.md` §A4. Gating experiment: `../../PLAN_C_MEMSPINE_SERVICE.md` §1.1.

## Why this exists

`MASTER_PLAN.md` records the engine as *"82%, evals absent"*. Every one of the seven novelty claims
(N1–N7) needs evidence, and nothing could produce it. Separately, the survey's own audit of the
field's twenty highest reported scores found **21 inadmissible, 6 partial, zero fully admissible**
under D16 — not one published number carries a complete protocol, and no existing harness is
system-agnostic (`mem0ai/memory-benchmarks` is Mem0-only).

So the harness is built around the failures we can already name:

| The failure, observed in the literature | What the harness does about it |
|---|---|
| MAGMA's 0.700 is a graded partial-credit mean; Mem0's 92.5 is an accuracy. Same column header. | `JudgeSpec` has **no default scale**. Every row carries its scale. `aggregate()` raises on mixed scales. |
| LongMemEval was silently re-released in Sept 2025 with cleaned histories. | `DatasetInfo.revision_id` is mandatory; the LongMemEval adapter refuses to construct without it. |
| Mastra: 84.23 on `gpt-4o` vs 94.87 on `gpt-5-mini` — same harness, same judge, same data. | The backbone is a declared `ReaderSpec` on every manifest; a run with no backbone is marked inadmissible for answer metrics. |
| MemPalace's 96.6% is R@5 **recall**, quoted everywhere as if it were accuracy. | `JudgeScale.RETRIEVAL_RECALL.is_answer_metric` is `False`; retrieval-only runs cannot pass as QA runs. |
| ConvoMem: full context beats memory systems below ~150 conversations. | Full-context replay is a first-class arm, not an afterthought. |
| Runs that cost more than intended. | `max_model_calls` is a hard cap; `expect_model_calls=False` makes "no model calls" a property of the run. |
| The native run lost 14 of 60 scheduled slots to an abort and 17 answers to an output cap. | Every scheduled question gets a row — `completed`, `truncated`, `error` or `unattempted`. A run that stops early still accounts for what it never ran. |
| 1,986 LoCoMo questions come from 10 conversations. | The 95% CI is bootstrapped by **item**, not by question. |

## It follows the paper's own evaluation contract

`../../paper_spine/EVALUATION_PLAN_2026-09.md` and `../../paper_spine/evaluation/LOOP_METRIC_CONTRACT.md`
are the authority here, not Plan A's sketch. Specifically:

- **Budget 4,096 retrieved tokens** is the primary setting (§4); 2,048 and 8,192 are *later* ablations,
  never mid-comparison.
- **Five baseline conditions** (§3): no persistent memory, BM25 over timestamped raw turns,
  dense/hybrid RAG over the same turns, the engine, and the engine plus a named governance
  intervention. Full-context replay is the separately costed reference. The first four ship;
  the governance arm arrives with A4-10.
- **Development items reserved before tuning** (§4): `python -m memspine_evals split` writes the ids
  to a file — 2 whole LoCoMo conversations, 40 LongMemEval histories — and `SplitView` stamps
  `split:dev` or `split:heldout` into the subset field, so every row discloses which side it came from.
- **Two means, both reported** (`LOOP_METRIC_CONTRACT.md` observation contract): `score_mean` keeps
  failed answers in the denominator at the declared failure score; `score_mean_measured` covers only
  gradeable answers. Unattempted questions are in neither — they are *unknown*, and never imputed.
- **CPC per stage** (§5): one homogeneous unit, each cost event assigned to exactly one of
  𝓡/𝓒/𝓖/𝓓/𝓚, failed attempts included. A stage whose cost the adapter cannot observe is marked
  unknown and `cost_accounting_complete` goes false — an unmeasured total never passes as a measured one.

This harness does **not** replace `paper_spine/evaluation/`. That tree holds the governance probes
(correction, deletion, scope, forgetting audit) over the two synthetic development histories — the
plan's P0 row. This is the P1+ benchmark track: many systems, public datasets, one protocol.

## Three interfaces, and nothing else

```python
DatasetAdapter:  info() -> DatasetInfo          # id + revision + licence + content hash
                 items() -> Iterator[EvalItem]  # history stream + queries + gold + type labels

SystemAdapter:   reset(item_id); insert(turn) -> DepositResult; query(q, budget, k) -> RetrievedContext

RunProtocol:     budget_tokens, top_k, seed, + ReaderSpec + JudgeSpec
```

Insert is sequential, a query sees only what preceded it, and the returned context is truncated to
the declared budget before the reader sees it — the LongMemEval-V2 precedent, cited rather than
reinvented.

## Run wrapper: `evals/run.sh`

One entry point for a LoCoMo run (replaces the core of the older `run_*.sh` scripts, which are kept
for provenance):

```bash
bash evals/run.sh --arm qs-eq06-fix --run-id qa-full-qs-eq06-fix-s152 --mode qa --topk 10     --items 1 --flags "--qa-prompt grounded --retry-refusal --judge-guards" --forensics
bash evals/run.sh --arm local-combo-A --run-id v33-combo-A --mode retrieval --topk 10 --questions 1540
```

Required: `--arm` (an `evals/arms/<arm>.json`), `--run-id`, `--mode qa|retrieval`, `--topk`.
Optional: `--items N`, `--max-queries N` (per-item cap), `--questions N` (expected row count, needed
unless it can be derived from the LoCoMo categories 1-4 defaults: 1540 full, 152 for `--items 1`),
`--flags "..."` (passed through to `c0-1`), `--forensics` (sets `MEMSPINE_FORENSICS_DIR=runs/<run-id>--forensics`),
`--engine-src PATH` (sets `PYTHONPATH`, default `<repo>/src`), `--data`, `--categories`, `--batch-turns`, `--force`.
Env: `PYTHON`, `READER_MODEL`, `JUDGE_MODEL`, `BASE_URL`.

What it does: refuses to overwrite `runs/<run-id>*` unless `--force`; clears `AWS_*` with `unset`; caps
`--max-model-calls` at 3 per question + 200 (0 in retrieval mode); writes the resolved command to the top of
`runs/_logs/<run-id>.log`; writes `runs/<run-id>.STATUS` (`ok` or `failed ...`); and after the run checks that
`results.jsonl` exists with the expected number of result rows. Exit code is non-zero on any failure.

## Run-integrity flags (gap register A8, D1-D4, D7, B11, F2, F3, A10)

**Default change (A8).** Every reader and judge request on the OpenAI-compatible endpoint now sends
`presence_penalty`, `frequency_penalty`, `top_p` and `seed` explicitly. Before, only `temperature` was sent, so
Ollama silently applied the model's own defaults (presence_penalty 1.5 for Qwen3.5). The new defaults are
`--presence-penalty 0 --frequency-penalty 0 --top-p 1 --sampler-seed <--seed>`. Runs made before this change used
the server's sampler and are not comparable to runs after it; the sampler is recorded in `manifest.reader.params.sampler`,
`manifest.judge.params.sampler` and `manifest.labels.sampler`. Pass `--presence-penalty 1.5` to reproduce an old run.

| flag | default | what it does |
|---|---|---|
| `--presence-penalty`, `--frequency-penalty`, `--top-p`, `--sampler-seed` | 0, 0, 1, run `--seed` | sampler sent with every reader and judge request (A8) |
| `--server-ctx` | 8192 | the model server's context window. A reader or judge call with `prompt + completion >= server_ctx - 8` sets row `meta.server_truncation_suspected` (and `server_truncation_by`) and is counted in `summary.json` `server_truncation_suspected` (D2) |
| `--strict-ctx` | off | raise (abort the run, rest UNATTEMPTED) instead of flagging (D2) |
| `--token-count heuristic\|reader` | `heuristic` | `reader` counts context tokens with the reader's tokenizer from the local HF cache (offline; falls back to the heuristic with a logged warning), and an over-budget context drops its lowest-ranked lines instead of cutting the tail. `context_tokens` is then the reader count and `meta.engine_tokens` the engine's (D1) |
| `--tokenizer-id` | first cached Qwen3 | tokenizer for `--token-count reader` |
| `--no-runtime-capture` | off | skip the manifest `runtime` block (D4) |

Always recorded, no flag:

- **D3** with `--retry-refusal`, row `meta` keeps `first_prompt_tokens`, `first_completion_tokens`, `retry_prompt_tokens`,
  `retry_completion_tokens` (the row's `prompt_tokens` is still the sum of both calls).
- **D4** `manifest.runtime`: `memspine.__file__`, versions of memspine/torch/transformers/sentence-transformers/httpx/lancedb,
  `git describe --always --dirty` of the engine's repo, argv, all `MEMSPINE_*`/`OLLAMA_*` env vars (names containing
  KEY/TOKEN/SECRET/PASSWORD are redacted), and, for a local-Ollama QA run, `/api/version` and `/api/ps` (2 s timeout, never fails the run).
- **D7** `forensics.jsonl` rows carry `run_id` and `query_id`, `ingest.jsonl` rows carry `run_id`; `forensics_report.py` joins on
  `query_id` (old logs fall back to the question text). A run refuses to start if `MEMSPINE_FORENSICS_DIR` already holds
  `forensics.jsonl` or `ingest.jsonl`, unless `MEMSPINE_FORENSICS_OVERWRITE=1` (which deletes them first).
- **B11** `summary.json` `rerank_check`: `FAILED` when the memspine arm's config has `read.rerank` other than `off` and the engine
  made no rerank call or any failed one (the CLI then exits 3 after writing its outputs), `ok` when it ran cleanly, `not_applicable`
  otherwise.
- **F2** `ingest.jsonl` rows add `quarantined`, `trust`, `status`; `summary.json` has `n_quarantined` (a warning is logged if > 0).
- **F3** a turn with a non-empty timestamp that `parse_turn_time` cannot read raises an error instead of being stored as "now".
- **A10** `summary.json` `summary.accuracy_ci`: accuracy with a conversation-level cluster-bootstrap 95% CI (2000 resamples by item;
  failed rows count at the failure score, like the headline mean).

### Measurement and harness additions (A4, A12, A13, D1, D5, E3, E4, F4)

- **A4 `--judge-prompt mem0-official`**: the Mem0 paper's LoCoMo J-score judge (CORRECT/WRONG, generous on same-topic
  answers). The text is the vendored `ACCURACY_PROMPT` from Backboard-io/Backboard-Locomo-Benchmark@164d45c (upstream
  `mem0ai/mem0 evals/metrics/llm_judge.py`); it is not byte-compared with upstream, so it is `vendored`, not
  `official-verbatim`. It cannot grade cat-5 abstention. To re-judge stored answers later, with no new reader calls:
  `python -m memspine_evals.rejudge --run runs/<run-id>/results.jsonl --suites mem0-official --model <litellm-model-id>
  --max-model-calls N` (add `--dry-run` first; `--suites` takes any registered suite, `--model` any LiteLLM model id).
- **A12** `forensics_report.py` run summaries add `sufficiency_on_qa_set` (retrieval-only runs: share of the non-adversarial
  questions with all gold in context, with `n` and `complete_qa_set` true at 1,540) and `recall_at_10_hits` (all gold in the
  engine's final top-10 search hits before neighbour expansion; needs the stage log).
- **A13** `--categories 5` runs the adversarial (abstention) questions with the default `rubric` judge (abstention-aware);
  a judge that cannot grade a refusal refuses to start. `forensics_report.py` labels cat 5 `adversarial`, excludes it from
  `n_questions`/`accuracy`/`by_category`, and reports `n_adversarial` and `adversarial.accuracy` apart (null for retrieval-only
  runs). `summary.json` has `adversarial_split` (headline excluding cat 5 next to cat 5) when a run includes cat 5. The default
  categories are unchanged.
- **D1 replay mode**: with `--token-count reader`, an over-budget replay context (chronological, `ranked=False`) is cut by hit
  rank, not by tail: the memspine adapter stamps `meta.hit_rank` (rank in the final search hits, None for neighbour lines) on
  every evidence row; neighbours of the lowest-ranked hits go first, then the lowest-ranked hits, and what remains keeps its
  chronological order. Without hit ranks it still falls back to the tail cut.
- **D5** `summary.json` `latency`: reader and judge latency `p50`/`p95` (wall time, queueing at the server included),
  `server_side_ms` (the server's own timing when the response carries `total_duration`/`timings`; Ollama's `/v1` endpoint does
  not, so it is null there) and `concurrent_arms` (read from the `MEMSPINE_CONCURRENT_ARMS` environment variable; set it in
  the launcher when arms share a server, otherwise null).
- **E4/F4** `summary.json` `ingest_timing`: ingest wall seconds (deposits, flushes, build) and turns per second, per item and
  overall. `manifest.runtime.gpu.start` and, in `summary.json`, `.end` (plus `gpu_memory`) hold GPU memory used/total in MiB
  from `nvidia-smi` (3 s timeout; null when unavailable, never fails a run).
- **E3** `evals/ollama_env.ps1` sets `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_CONTEXT_LENGTH=8192`,
  `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=-1` as user environment variables and restarts Ollama (`-NoRestart` to only set
  them). It interrupts any run using Ollama; do not run it mid-benchmark.

## Run it

```bash
# end-to-end check: no data, no models, no network
python -m memspine_evals smoke

# reserve development items BEFORE tuning anything, and commit the file
python -m memspine_evals split --dataset locomo --path data/locomo10.json \
    --revision auto --dev-items 2 --output data/locomo_split.json

# the C0-1 gate, retrieval mode (zero model calls, zero cost)
python -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json --revision auto

# same gate with dense retrieval — the like-for-like MemPalace comparison
python -m memspine_evals c0-1 --dataset longmemeval --path data/longmemeval_s.json \
    --revision 2025-09-cleaned --dense

# QA mode against a local Ollama backbone; the cap is mandatory
python -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json --revision auto \
    --mode qa --reader-model qwen3:4b --max-model-calls 500

# tests (68, all offline, zero model calls)
python -m pytest tests/ -q
```

Every run writes `runs/<run_id>/`:

| file | contents |
|---|---|
| `results.jsonl` | **line 1 is the manifest**, then one line per query, then the summary |
| `trace.jsonl` | per-turn stage trace: `E_t`, `M_ctx,t`, `y_t`, `Δ_t`, `P^u_t` |
| `summary.json` | manifest + summary + a ready-made `_shared/score_matrix.csv` row |
| `COMPARISON.md` | the cross-system table (C0-1 runs) |

## Data: fetch it yourself, and record the licence

The harness **never downloads data**. Put files under `evals/data/` (git-ignored) and pass `--path`.

| dataset | where | licence position |
|---|---|---|
| LoCoMo | `snap-research/locomo` → `locomo10.json` | check the release before publishing any number |
| LongMemEval | `xiaowu0162/LongMemEval` → `longmemeval_s.json` | cleaned release is MIT — **verify the file you hold** |
| LoCoMo-Plus | upstream repo | **no LICENSE file** as of the last check — clear terms before use |

| ConvoMem | HF `Salesforce/ConvoMem` @ `e3e9b39` → `data/convomem/core_benchmark/...` | **CC BY-NC 4.0** (data), Apache-2.0 (code); research only |
| HaluMem | HF `IAAR-Shanghai/HaluMem` @ `cb04336` → `data/halumem/HaluMem-Medium.jsonl` | **CC BY-NC-ND 4.0**; research only, never redistribute |
| MemoryAgentBench | HF `ai-hyz/MemoryAgentBench` @ `7ea0669` → `data/mab/Conflict_Resolution.parquet` | MIT |
| StateMemBench | arXiv 2608.19652 | **unreleased** (registry entry only; no adapter) |

Download commands (run them yourself; the harness and its tests never fetch anything):

```bash
# ConvoMem: only the evidence_questions tree is used (the full repo is ~27 GB)
huggingface-cli download Salesforce/ConvoMem --repo-type dataset --revision e3e9b39115b02346824c70d349350de738f8be41     --include "core_benchmark/evidence_questions/*" --local-dir evals/data/convomem
# HaluMem (Medium; HaluMem-Long.jsonl is 107 MB)
huggingface-cli download IAAR-Shanghai/HaluMem HaluMem-Medium.jsonl --repo-type dataset     --revision cb04336aa1b732d4b24f5186c552456b4099806e --local-dir evals/data/halumem
# MemoryAgentBench, Conflict_Resolution split (FactConsolidation)
huggingface-cli download ai-hyz/MemoryAgentBench data/Conflict_Resolution-00000-of-00001.parquet --repo-type dataset     --revision 7ea066982b140a19337e17e60d45d4076e042faf --local-dir evals/data/mab
```

The MemoryAgentBench file lands at `evals/data/mab/data/Conflict_Resolution-00000-of-00001.parquet`
(the name in the HF repo's `data/` listing); move it to `evals/data/mab/Conflict_Resolution.parquet`.

Scoring, as each benchmark defines it:

- **HaluMem** (Memory QA only): an LLM judge labels each answer `Correct`, `Hallucination` or
  `Omission` against the reference answer and the key memory points (`query.meta["key_memory_points"]`);
  the official code takes the judge model from `OPENAI_MODEL`, so a run must declare it. The
  extraction and updating tasks need a system's extracted memory list and are not adapted.
  There is no turn-level retrieval gold.
- **ConvoMem**: exact match for user, assistant and changing facts; semantic match for preference
  and implicit-connection questions (`query.meta["official_metric"]`). The semantic judge is not
  reproduced here.
- **MemoryAgentBench FC**: substring match against the answer aliases (`AliasContainsJudge`).

`memspine_evals.datasets.registry` lists every dataset with its source, licence and status;
`registry.load("statemembench", ...)` raises `DatasetUnavailable`.

## Retrieval-only (free) adapters — plan v3.2 M3-t2

Every adapter below runs in `--mode retrieval` with zero model calls: gold evidence lands in
`Query.gold_turn_ids`, so the runner's coverage / `R@k` / `R_all@k` machinery scores it directly.
Measures that are not R@k are pure functions in the adapter module, applied to a run's result rows
(`retrieved_ids`) joined to `Query.meta` by `query_id`. All tests use hand-written synthetic
fixtures under `tests/fixtures/<name>/`; data-gated tests only parse. BEAM needs the project venv's
`pyarrow` (the anaconda build cannot read its parquet).

| registry id | class | free measure | licence |
|---|---|---|---|
| `memoryagentbench` | `MemoryAgentBenchDataset` | gold-fact R@k + `mab_gold.supersession_order_rate` | MIT |
| `op_bench` | `OPBenchDataset` | `op_bench.injection_rate`, `context_repetition` (no R@k gold) | none; run only |
| `perltqa` | `PerLTQADataset` | Reference Memory R@k per memory type | CC BY-NC |
| `prefeval` | `PrefEvalDataset` | preference-turn R@k under seeded filler | CC BY-NC |
| `beam` | `BEAMDataset` | `source_chat_ids` R@k; `update_order`, `contradiction_pair_recall` | CC BY-SA |
| `tofu`, `muse_news` | `TOFUDataset`, `MUSENewsDataset` | `erase_and_verify` residual R@k (forget), retain R@k, `membership_auroc` | MIT / CC BY |
| `personabench` | `PersonaBenchDataset` | `segment_id` R@k per noise level; `official_recall` | CC BY-NC-SA |
| `lamp2`, `memorycd` | `LaMP2Dataset`, `MemoryCDDataset` | `knn.knn_vote` tag accuracy / `knn.knn_mean` rating MAE | research only |
| `halumem_proxy` | `HaluMemProxyDataset` | R@k on lexical memory-point → turn proxy gold | CC BY-NC-ND |
| `cpb_live` | `CPBLiveDataset` | `cpb_retrieval_rates`: true R@k, false-retrieval rate, true-above-false order | MIT |
| `asb` | `ASBDataset` | `firewall_rates(asb_samples(root), "base"\|"extended")` TPR / FPR | MIT |

Per-set notes:

- **MemoryAgentBench Conflict_Resolution** (`mab_gold`): facts are parsed with the release's
  relation templates; per (subject, relation) the highest serial is *current*, the rest *stale*.
  Single-hop gold = the current fact of a question subject whose object matches an alias;
  multi-hop gold = the shortest chain of current facts (≤ 4 hops). 798/800 questions map
  (`meta["gold_mapping"]`: `mapped` / `ambiguous` / `inconsistent` / `unmapped`); `inconsistent`
  (67) marks answers that need a superseded fact — the release is not always self-consistent —
  and those hops are left out of the order metric. *Supersession order*: a query passes when
  every current fact whose stale version was retrieved outranks all its stale versions; the rate
  is over applicable queries and `n_applicable` is reported beside it. `map_gold=False` restores
  the gold-free behaviour.
- **OP-Bench**: LoCoMo history (same turn ids); irrelevance probes carry
  `meta["persona_turn_ids"]`. A system that always returns top-k scores 1.0 injection, so the
  rate separates only systems with a floor or gate. Sycophancy probes carry `false_premise`.
- **PerLTQA**: one memory record per turn; en_v2: 31 characters, 8,316 queries (11 unmapped).
- **PrefEval**: explicit and choice forms have exact gold; persona-form gold is heuristic (filter
  on `meta["gold_method"]`; 166 of 1,000 left empty). Only 24 local filler conversations exist.
- **BEAM**: only the 100K split is local; QA needs the rubric judge (not run).
- **TOFU / MUSE-News**: one item holds the corpus; queries carry `meta["probe_set"]`. After
  deposit, `erase_and_verify(engine, item, deposits)` hard-forgets the forget set and calls
  `Engine.verify_forget(probe=...)`; re-querying gives forget-set residual R@k (target 0) and
  retain R@k (collateral). MUSE knowmem is not adapted.
- **PersonaBench**: 263 measurable QA per noise level (only 3 people per community have private
  data); Subjective is flagged `official_excluded`.
- **LaMP-2 / MemoryCD**: no retrieval gold in the releases; `gold_turn_ids` are label-consistent
  proxies (same tag / same-domain same rating). MemoryCD queries use the review title, which leaks
  sentiment.
- **HaluMem proxy**: evidence memory points mapped to turns by token overlap ≥ 0.5; report the
  mapped share (407/496 on two users) beside any R@k.
- **CPB Live**: 160 scenarios locally (card says 180); stage-1 echo scripts are not replayed.
- **ASB**: the five injection templates are rebuilt from the benchmark card — check them against
  commit `544540f` before publishing. On the real data the base detector gives pooled TPR 0.286,
  the extended one 0.714, both at FPR 0 on 71 negatives; raw attack instructions and tool
  descriptions score 0/400 for both.

## Pre-registered sweeps

`evals/prereg/` holds pre-registrations, committed before their run. G8a
(`G8a_wrapper_threshold.md`) fixes the grid, metrics and decision rule for
`integrity.untrusted_wrap_below`; the offline arm is

```bash
python evals/sweep_wrapper_threshold.py --locomo evals/data/locomo10.json     --out evals/prereg/G8a_wrapper_threshold_results.md
```

Its paid QA and ASR arms are listed in the pre-registration and are not run without approval.

G24 (`G24_gliner2_planner.md`) tunes the GLiNER2 decision planner's options against a frozen,
rule-labelled set of 100 LoCoMo questions (`G24_gliner2_planner_set.json`). It runs locally on
CPU and needs the `[ner]` extra and a cached checkpoint:

```bash
HF_HOME=... python evals/gliner2_planner_eval.py run --split both     --out evals/prereg/G24_gliner2_planner_results.md
```

`--revision auto` labels a file by its own content hash. That is a weaker label than a release name
and a stronger identifier than the nothing most papers record.

## What is built, and what is not

**Built and tested (A4-1 … A4-4, A4-9):** contracts, provenance + D16 admissibility, stage trace,
(accuracy, tokens, latency) triplet with per-stage cost and CPC, judge module with four scales and
no silent default, the runner with both call guards, denominator preservation, cluster-bootstrap
CIs, deterministic splits, result/summary/score-matrix writers, the CLI.

**Systems (A4-8, first half):** `no-memory`, `full-context`, `naive-rag`, `verbatim` (lexical,
dense or hybrid RRF), and a `memspine` adapter that drives the public facade only (`write_messages`
in, `assemble` out). Peer systems — Mem0, A-MEM, Zep, MAGMA, MemOS, T-Mem, SaliMory — are not
written yet.

**Datasets (A4-5):** LoCoMo, LongMemEval S/M/oracle, LoCoMo-Plus, ConvoMem, HaluMem,
MemoryAgentBench CR, and the retrieval-only tier-2 set above (plan v3.2 M3-t2). Not yet adapted:
PersonaMem v1, MAB AR/TTL/LRU, MemBench, GroupMemBench, MemoryArena, LME-V2.

**Not started:** A4-10 (firewall ablation, MINJA / AgentPoison / MemoryGraft), A4-11 (E1–E9 single
toggles), A4-12 (go/no-go).

**Known limitation, recorded rather than hidden:** the memspine adapter cannot see deposit-stage
model calls, because the public facade does not report them. Profiles with LLM extraction or entity
NER on the write path *do* call models. The adapter therefore marks the deposit stage **unknown**
rather than zero, which flips `cost_accounting_complete` to false for that run — the cost is
unmeasured, and the summary says so instead of implying a free write. Reading it properly means
instrumenting the engine's structlog events.

## Note on the synthetic dataset

`synthetic-smoke` exists to prove the harness, not a system. Its `DatasetInfo.notes` says
*"never quote as a system result"*, and nothing it produces belongs in a score matrix that anyone
reads.

## Bedrock backends (Qwen3 + Cohere Embed v4)

`memspine_evals.bedrock` runs readers, judges and multi-agent LLM agents on AWS Bedrock through LiteLLM.

- **Credentials:** `load_aws_credentials(".env")` exports **only** `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` and the region (`AWS_MODEL_REGION`). Nothing else in `.env` is loaded. Pass `dotenv_path=None` to `Engine` so the rest of the file never enters config layering.
- **Models** (verified on-demand in us-east-1, 2026-09-30): `QWEN3_32B` = `bedrock/converse/qwen.qwen3-32b-v1:0`, `QWEN3_NEXT_80B`, and `COHERE_EMBED_V4` = `bedrock/cohere.embed-v4:0` (1536-d, the default; 256/512/1024/1536 valid), and `TITAN_V2` = `bedrock/amazon.titan-embed-text-v2:0` (1024-d, optional).
- **Engine:** `Engine(..., dotenv_path=None, **bedrock_engine_config(region))` gives Cohere v4 embeddings and Qwen3 for the `extract`/`judge`/`chat` roles.
- **Cost control:** every LLM call goes through `CallBudget(max_calls=N)`, which raises *before* the call. Token counts are always recorded; dollars only if you pass a price table from the AWS pricing page (none are hard-coded).
- **Needs** the `[aws]` extra (`uv sync --extra aws`) for boto3. LiteLLM is core.
- **Smoke test:** `uv run --no-sync python evals/bedrock_smoke.py` (1 Qwen3 call plus a few Cohere embeds).
- **Engine roles on Qwen3:** `c0-1 --memspine-llm bedrock-qwen3` binds every engine role (extract, extract_edges, summarize, reflect, anticipate, relevance, query_rewrite, judge, chat) of the memspine arm to Qwen3-32B in the environment's region; explicit `llm.roles` in `--memspine-config` win. Engine calls count against `--max-model-calls`. Qwen3 roles get the `/no_think` switch automatically (`llm.roles.<role>.no_think`).
- **Dollar cap:** `--price MODEL=IN,OUT` (USD per 1M tokens, repeatable) and `--max-usd X` per arm. Reader/judge calls are refused before a call whose worst case (prompt + `max_tokens`) would cross the cap; engine spend is charged as observed and stops the next call. The arm ends with UNATTEMPTED rows. Embedding calls (Cohere) are not metered.
- **Raw reader reply:** when a reader extracts its final answer (`extract_answer`, e.g. `--qa-prompt dated3`), the row's `answer` holds the extracted line and `meta["reader_raw"]` keeps the full reply (capped at 4000 chars, `meta["reader_raw_truncated"]: true` when cut), so a truncated list can be audited; rows of non-extracting prompts carry no such key.
- **Routed QA prompt (C1 / H11):** `--qa-prompt routed` picks one prompt per question by its shape (`memspine.core.query_shape`): a date or span question (`is_temporal`) gets `dated` with the refusal replaced by "infer the date from the line's date and any relative phrase; say you do not know only when nothing bears on it; answer dates as DD Month YYYY (or the granularity asked)"; a "would / likely / might / could ...?" question (`is_inference`) gets `dated` with the refusal replaced by "give the most plausible answer and say 'likely'" (no refusal); every other question gets the `dated` text verbatim. All three keep `dated`'s one-sentence answer (no "very concise" wording: `dated3` lost 3.3 points). The reader's `describe()` adds `qa_prompt: routed`, `qa_router` and `prompt_variants_sha256` (one hash per variant; `prompt_sha256` hashes the three), and each row records `meta["qa_variant"]` (`plain` / `temporal` / `inference`). Every other `--qa-prompt` keeps its text, `describe()` and rows byte-identical. The engine's matching switch is `prompts.selection.chat_by_shape` (`docs/USAGE.md`).
- **Rehearse before paying:** `python evals/rehearse.py --plan evals/plans/aamas_runs.json --path data/locomo10.json --price bedrock/converse/qwen.qwen3-32b-v1:0=IN,OUT` runs every planned arm on one conversation over a local stub transport (no network) and writes `PROJECTION.md` with the projected cost of the full LoCoMo cats 1–4 run.

## Screening memory-side changes (retrieval-only + disk cache)

A paid LoCoMo QA run of one arm costs dollars and hours. Most memory-side changes (what is
stored, how it is ranked, what the read path assembles) can be screened first for whether
they put more gold evidence into the reader's context, with no reader, no judge, and the
paid embedding / engine-role calls replayed from disk.

**Flags (`c0-1`):**

- `--retrieval-only`: ingest the history and call the arm's read path for every question
  exactly as the QA run would (same `--memspine-config`, `--budget`, `--top-k`,
  `--memspine-read-mode`, `--memspine-build-sleep`, `--memspine-batch-turns`), then skip the
  reader and the judge. Each row carries `retrieved_ids` (after the budget cut),
  `context_tokens`, `meta.gold_evidence` (LoCoMo `qa[*].evidence`, split on `;` and
  normalised to `D<s>:<t>`) and `meta.ev_all` / `ev_any` / `ev_frac`; `score` is `ev_all`.
  Cat 5 has no gold evidence (`ev_* = null`, left out of the means). `summary.json` gains a
  `coverage` block: per category, the mean `ev_all` / `ev_any` / `ev_frac` and context
  tokens. Protocol id `c0-1-retrieval-only`, reader model `none`: never a QA number.
- `--cache-dir PATH`: a SQLite cache (`PATH/llm_cache.sqlite`) at the LiteLLM boundary for
  embeddings (one row per text, keyed on model, input type, dimensions, text; vectors stored
  bit-exact) and engine-role completions (extract / mine, summarize, reflect, anticipate,
  extract_edges, plan, ...; keyed on model, the full messages, temperature, max_tokens,
  response_format and the other sampling options). A hit costs $0, is not counted as a
  model call, and is reported in `summary.json` (`cache_hits`, `cache_misses`, `usd_saved`,
  and a `cache` block with the split). A completion with a non-zero temperature is never
  cached (`uncacheable`); the engine's roles send no temperature and are keyed as such, so a
  hit replays the reply the populating run received. Several processes can share one cache.
- `--cache-reader`: with `--cache-dir`, also cache reader and judge completions (temperature
  0 only). Off by default: reader and judge calls pass through (`bypassed`).

Coverage credits raw turns only: a derived record (a mined fact, a summary, a card) that
carries the evidence is not a turn and is not counted, so an arm whose gain is in derived
records reads low on coverage. Screen those on the paid 2-subset QA step instead.

**Workflow:**

1. **Populate the cache once** with a normal (or retrieval-only) run of combo-A over the
   conversations, with `--cache-dir evals/cache/locomo` added. This stores every turn and
   question embedding (Cohere v4) and any engine-role reply the arm made.
2. **Screen arms** with `--retrieval-only --cache-dir evals/cache/locomo` (keep
   `--max-model-calls` as the cap on cache misses). A read-side arm re-embeds nothing and
   costs about $0; an arm that adds write-time LLM roles (mining, cards, reflection) pays
   for its own new calls once, and its re-screens are then free too.

   ```
   python -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json --categories all \
     --with-memspine --only-systems memspine --memspine-llm bedrock-qwen3 \
     --memspine-read-mode replay --memspine-config "$(cat arm.json)" \
     --retrieval-only --cache-dir evals/cache/locomo --max-model-calls 2000 \
     --item-ids conv-26 --run-id screen--ARM--conv-26
   python evals/screen_compare.py --base "evals/runs/screen--combo-A--conv-*--memspine" \
     --arm "evals/runs/screen--ARM--conv-*--memspine"
   ```

   `screen_compare.py` pairs questions by `(item_id, query_id)` across split
   per-conversation run dirs (globs) and prints per-category coverage deltas with an exact
   sign test on `ev_all` (won / lost on discordant questions, two-sided binomial); given two
   QA runs it adds accuracy deltas with the same sign test (`--locomo` recomputes coverage
   for QA rows from their `retrieved_ids`; `--json` writes the result).
3. Only arms that **raise coverage** go to a paid **2-subset QA screen**.
4. A **full run** only if both subsets are positive.


---

## Legacy harness (2026-10-02, `evals/run.py`, `evals/harness/`)

# `evals/` — the memspine evaluation harness

**No benchmark has been run and this directory contains no results.** Any number you see
about memspine anywhere is, as of today, unmeasured. This is machinery, not evidence.

Two seams are still unimplemented and both are named below (§ *What is not built*).
Everything else — configuration, the ablation bridge, both dataset adapters, the build
cache, the cost ledger, the result schema, the frontier ladder — is complete and tested.

This harness lives **outside the wheel** (D-19/D-35). It is not shipped, is not
importable from `memspine`, and may depend on things the slim core cannot. Nothing in
`src/memspine` imports anything here.

---

## What it is for

memspine must run LoCoMo and LongMemEval at an operating point comparable to lightweight
research systems, **and** report what each governance layer costs. The headline result is
a **cost-versus-capability frontier**, not a single score:

```
        accuracy
           ▲
           │      ● full     entity extraction + graph extraction + rerank + compression
           │    ●   graph    + link evolution and graph traversal
           │  ●     consolidation
           │ ●      firewall
           │●       lean     ← the row that sits beside peer systems
           └────────────────────────────────────►  tokens / latency per cycle
```

Only a system whose layers are toggles can produce that curve. Producing it is this
harness's whole job, and it is why every mechanism is a flag and every flag is priced.

Two outcomes are possible and **both are publishable**: the lean profile may score below
peer systems, and the full profile almost certainly costs more per cycle. They are only
embarrassing if we claim to be lightweight and are not.

---

## Quick start

Run from the repository root, in the dev environment (`uv sync --all-extras`).

### 1. See what you can toggle, without running anything

```bash
uv run python -m evals.run --list-ablations
```

Prints all 22 toggles, the exact memspine config key each one binds to, every caveat, and
the frontier ladder. A toggle marked `UNAVAILABLE` names the config option memspine would
need for it to be priceable — selecting it is an error, never a silent no-op.

### 2. Resolve a run without spending a token

```bash
uv run python -m evals.run \
    --dataset locomo --data-path data/locomo10.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --dry-run
```

Prints the complete `RunConfig`, both digests, the resolved memspine config, and every
caveat. **Do this first for any run you intend to publish**: if the protocol is wrong,
this is where it is cheapest to find out.

### 3. A lean baseline

```bash
uv run python -m evals.run \
    --dataset locomo --data-path data/locomo10.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --profile base --ablation no_firewall,no_evolve_links,no_graph_retrieval,no_consolidation \
    --out evals/runs/locomo-lean.json
```

Generation writes the result file **before** the judge spends a token, so a judge failure
never costs you the generation pass. Re-run the judge with §4.

> `--profile base` rather than the default `benchmark` template — see *Blockers* below.

### 4. Re-judge a stored run — the harness's most valuable trick

```bash
uv run python -m evals.run --score-only \
    --input-results evals/runs/locomo-lean.json \
    --judge-model openai/gpt-4o \
    --out evals/runs/locomo-lean.gpt4o.json
```

**The engine is never constructed.** The generation seam is imported lazily inside the
generation path, so that guarantee is structural rather than a promise. The result file
is self-contained — question, gold answers, dataset-native metadata, prediction, raw
generation, and the retrieved evidence *with its text* — so re-judging never needs the
corpus.

This is what turns judge sensitivity into a *result* ("X under a graded reference-based
judge, Y under a binary reference-free one") rather than an apology in the limitations
section. It is also the single largest cost saver in an LLM-judged evaluation.

`--score-only` refuses any flag it would ignore, rather than accepting it and changing
nothing. Only the judge may change.

### 5. The whole frontier — one command, one row per governance layer

```bash
uv run python -m evals.run \
    --dataset longmemeval --data-path data/longmemeval_s_cleaned.json \
    --model openai/gpt-4o-mini --judge-model openai/gpt-4o-mini \
    --profile base --frontier \
    --out evals/runs/frontier.json
```

Every rung is resolved and pre-flighted **before any of them runs** — a ladder that fails
on its last rung after paying for the first four is a wasted run — and the frontier file
is checkpointed after each rung. Each rung is a delta on the one below:

| Rung | Turns on | Delta measures |
|---|---|---|
| `lean` | — | the comparable operating point |
| `firewall` | `firewall` | the trust-threshold limb (see the caveat it prints) |
| `consolidation` | `consolidation` | episodic → semantic consolidation (N6) |
| `graph` | `graph_retrieval` + `evolve_links` | associative memory, both sides at once |
| `full` | 6 toggles | a bundle, **not** an attribution |

`--rungs lean,firewall` emits a subset; accumulation still walks the whole ladder, so a
subset row still carries everything it inherits.

### 6. A single-factor ablation

```bash
uv run python -m evals.run ... --ablation no_hybrid_retrieval --single-toggle
```

`--single-toggle` refuses to run unless the ablation deviates from the reference in
exactly one field, so a half-applied or accidentally-compound ablation cannot be reported
as a single-factor result. Read-side ablations reuse the cached build (§ *Caching*).

---

## What is not built

Two seams. Both are resolved lazily by dotted `module:attribute` and both are
overridable, so you can point them at your own implementation today.

| Seam | Default | Contract |
|---|---|---|
| generation | `evals.harness.generation:generate` | `generate(config: RunConfig)` → `RunResults` \| `(items, builds)` \| `[ItemResult]`; sync or async |
| judge | `evals.harness.judge:score` | `score(results: RunResults)` → `RunResults` \| `[ItemResult]`; sync or async |

Neither module exists. Running without them fails with a message that states the full
contract. **The harness does not substitute a stub, a placeholder score, or a mock**:
producing a number nobody measured is the one failure mode this directory exists to
prevent.

The generation seam is the engine adapter, and it is the larger of the two. Its job:

1. Load the corpus — `evals.legacy_datasets.base.load_dataset(name, path)` yields
   `BenchmarkSample` for either benchmark.
2. Build or reuse each sample's memory — `BuildCache` keyed on `config.build_digest()`;
   for LoCoMo, `evals.legacy_datasets.locomo.build_or_load_memory` already does this end to end
   through the real write door.
3. Retrieve and assemble per question, honouring `config.budget`.
4. Call the backbone named in `config.generation.backbone`.
5. Record per-stage cost with `evals.harness.accounting.CostLedger`, bridged into the
   result schema by `evals.harness.results.stage_costs_from_report`.
6. Stamp `RunSummary.resolved_system_config` from the started engine (the driver already
   stamps the pre-flighted resolution; the engine's own is better).

Aggregation is **not** a seam: `RunResults.save()` recomputes every breakdown, including
the per-category and per-memory-type views, so a stored file cannot disagree with its own
items.

---

## The discipline it enforces

These are not conventions. Each is enforced by the schema or by an import-time check, so
a run that violates one either fails validation or is visibly incomplete in its own file.

### 1. Every result carries (accuracy, tokens, latency)

A score without a cost is not a result. Every `ItemResult` records per-stage `TokenCounts`
and latency keyed by loop stage; every `BuildRecord` records what constructing a sample's
memory cost; the summary carries a `CostPerCycle` block.

Peer harnesses commonly report accuracy and no cost at all. Inheriting that omission
would forfeit both memspine's stated discipline and the framework's cost-per-cycle metric.

The judge's own token spend is tracked **separately** and never enters cost per cycle. The
instrument's cost is not the system's cost.

### 2. Every run records its full configuration

Unreported protocol variation is the single largest source of incomparable scores in this
field — two papers reporting "LoCoMo accuracy" may differ in judge model, rubric,
backbone, sampling, token budget, retrieval depth, and whether memory was rebuilt per
sample, and none of it is written down.

So if it can move a number, it is a typed field: sample selection **and the corpus's
SHA-256**, backbone and judge model with temperature and seed, the answer prompt's and
rubric's SHA-256, the token budget and its `B = system + history + output + retrieved`
split, the embedder and its quantisation, the enabled memory types, the event-log mode,
the engine-internal LLM role, **the user config file's SHA-256**, and **concurrency**
(which does not change accuracy but certainly changes measured latency).

Two digests fall out of this:

| Digest | Covers | Used for |
|---|---|---|
| `protocol_digest()` | everything that can change a number | comparability — same digest means same protocol |
| `build_digest()` | only what changes the *constructed memory* | the per-sample build cache key |

Exclusions from the protocol digest are explicit and audited at import, so a newly added
field is fingerprinted **by default** — the safe direction.

### 3. One flag = one toggle

`AblationConfig` is a flat set of independent booleans **whose defaults are the reference
configuration**. Three import-time checks keep it honest:

- every toggle is classified write-side or read-side, exactly once — an unclassified one
  would drop out of the build cache key, and a run with it flipped would reuse a build
  made without it;
- every toggle has a declared memspine config seam — one without would be accepted on the
  command line and change nothing, putting an unearned row on the frontier;
- every `Dataset` the CLI offers has a loader behind it.

Where memspine has no seam, the driver **refuses the run and names the missing option**.
Where a binding is an approximation, it carries a caveat that is printed to stderr and
recorded in the run's notes, so the published number says what was actually switched off.
`no_firewall` is the sharpest example: it neutralises one of `should_quarantine`'s three
limbs, and the caveat says so.

### 4. Generation is separable from scoring

See Quick start §4. Consequently the result file is self-contained.

### 5. Breakdowns are first-class, not derived later

`per_category` and `per_memory_type` are computed at write time and stored. A breakdown
that is nobody's job does not get reported.

Per-memory-type attribution rule, stated because it is a choice and not a fact: an item
belongs to type *X* when at least one retrieved item came from *X*. Items appear under
several types and the counts do not sum to the item total; `evidence_share` and
`top1_share` give the complementary non-overlapping view.

### 6. No fabricated numbers

Where a statistic is undefined the field is `None`, never `0.0`:

- no items, no judged items, no recorded turns → `None`;
- **no stage cost recorded anywhere** → `cost_recorded: false` and every token *rate* is
  `None`. A run that was not instrumented did not cost zero;
- **any unattributed tokens** → `tokens_per_cycle` is `None`, not a lower bound. Cost that
  arrived outside every phase belongs to no leg of the cycle.

Totals stay integers, because a total is a count of what was recorded. Rates are the field
a reader mistakes for a measurement.

---

## Layout

```
evals/
  README.md              this file
  run.py                 the run driver: CLI, run protocol, toggle→config bridge, frontier
  harness/
    config.py            RunConfig — everything that can change a number, and the digests
    results.py           ItemResult / BuildRecord / RunSummary / RunResults — the file format
    accounting.py        span-level token + latency ledger (stdlib only, imports nothing)
  datasets/
    base.py              the shared contract: sample model, build cache, registry
    locomo.py            LoCoMo parser + ingestion through the real write door
    longmemeval.py       LongMemEval parser, session ordering, abstention
  tests/                 117 tests, no corpus, no keys, no engine
```

`evals/` deliberately has **no `__init__.py`** — it is an implicit namespace package, so
it can never be mistaken for part of the distribution.

### Running the checks

```bash
uv run ruff check evals/ && uv run ruff format --check evals/
MYPYPATH=. uv run mypy --explicit-package-bases --namespace-packages evals/
uv run pytest evals -q
```

`mypy` needs those two flags here: `evals/` is a namespace package, so a bare
`mypy evals/` resolves `evals/harness/accounting.py` under two module names at once and
refuses. Note that **CI runs none of these on `evals/`** — `[tool.mypy] files` is
`["src/memspine"]` and `[tool.pytest] testpaths` is `["tests"]`. Closing that gap is a
pyproject change, which is outside this directory's ownership.

---

## Caching

Construction is the expensive half of a benchmark run; read-side ablations are the cheap
half. Caching the built store per sample is what makes the ablation matrix affordable at
all (MAGMA_HARVEST §2.3). One cache serves both benchmarks, keyed on

```
(dataset, sample_id, profile, config_hash, options_hash, adapter_version)
```

plus a content hash checked inside the manifest. Every component is load-bearing:

- **`config_hash`** — the engine configuration. Without it, a run under one governance
  configuration silently reuses a memory built under another.
- **`options_hash`** — the *ingestion* options: channel, timestamping, **sleep cadence**,
  speaker-role mapping. Sleep cadence is a governance layer this project exists to price;
  absent from the key, pricing it measures nothing and reports a number.
- **content hash** — an edited or re-downloaded corpus invalidates the build.

None of these fails loudly when it is wrong. They just report. `--rebuild` is the escape
hatch when the write path itself changed; the cache lives at `evals/.cache/` and is
gitignored, as are `evals/runs/`.

The cache holds the engine's own on-disk store plus a bookkeeping manifest. It is **not**
a second source of truth for memory content — the event log remains that.

`IngestionStats` is replayed from the manifest on a hit, so a cached run still reports its
write cost instead of a blank cell. Note that `wall_seconds` on a hit is the *original*
build's; `BuildOutcome.reused` disambiguates.

---

## What each result field means

A result file is `{schema_version, summary, items[], builds[]}`.

### `summary`

| Field | Meaning |
|---|---|
| `config` | the complete `RunConfig`. What was **asked for** |
| `resolved_system_config` | the **effective** memspine config the run actually used. `config.system` is a template name plus overrides — a pointer to a mutable file; this closes the gap |
| `protocol_digest` | comparability key. Same digest ⇒ same protocol |
| `build_digest` | what the constructed memory depended on. Runs sharing it share builds |
| `environment` | harness/memspine/python versions, platform, git commit |
| `n_samples` / `n_items` / `n_judged` / `n_errors` | errored items are counted, never silently dropped |
| `overall` | `ScoreBreakdown` over every item |
| `per_category` | one `ScoreBreakdown` per dataset-native category. Uncategorised items land under `"uncategorised"` rather than vanishing |
| `per_memory_type` | `MemoryTypeBreakdown` per memory type, plus `evidence_share` and `top1_share` |
| `costs` | per-loop-stage totals, items **and** builds |
| `judge_cost` | the instrument's spend. Never enters cost per cycle |
| `cost_per_cycle` | see below |
| `ablation_label` / `ablation_deviations` | e.g. `no_hybrid_retrieval`; round-trips back into `--ablation` |

### `overall` / `per_category` / `per_memory_type` (`ScoreBreakdown`)

`n`, `n_judged`, `n_errors`, `judge_score_mean`, `pass_rate` (fraction at or above
`judge.pass_threshold`), `exact_match`, `f1`, `tokens_total` (a count),
`tokens_mean` (`None` when nothing was measured), `latency_ms_mean` / `_p50` / `_p95`
(nearest-rank, so a p95 is always a latency that actually happened), `retrieved_mean`.

### `cost_per_cycle`

LoCoMo and LongMemEval are snapshot QA: the history is frozen and the agent answers probes
over it, which is not the live multi-session episode the framework's cost-per-cycle metric
is defined over. So the harness reports the two halves separately **and** a combined
figure, and does not pretend the combined figure is the episode metric.

| Field | Meaning |
|---|---|
| `cost_recorded` | whether *any* per-stage cost reached this summary. `false` ⇒ every rate below is `None` |
| `questions` / `turns_ingested` | the two denominators |
| `query_tokens_total` | retrieve + compose + generate |
| `build_tokens_total` | deposit + synthesise, from builds **and** from items |
| `query_tokens_per_question`, `build_tokens_per_turn`, `build_tokens_per_question` | the three rates |
| `tokens_per_question` | query plus amortised build, per question. The single-figure cost of a run |
| `query_latency_ms_per_question` | mean item wall time |
| `per_stage_tokens`, `per_stage_tokens_per_question` | the R/C/G/D/K split |
| `cached_prompt_tokens` | prompt tokens served from a provider prefix cache (E2's headline claim, never yet measured in this codebase — this is the field that would measure it) |
| `builds_reused` | cached builds. Their cost is *not* counted as work done by this run |

### `items[]` (`ItemResult`)

`item_id`, `sample_id`, `question`, `gold_answers`, `gold_evidence`, `category`,
`question_meta` (dataset-specific fields a judge may need, stored so re-judging never
reopens the corpus), `prediction`, `raw_generation` (pre-normalisation, because answer
extraction is itself a protocol choice), `context` / `context_tokens`, `retrieved[]`
(record id, memory type, rank, score and its named components, **text**, trust,
quarantined, namespace, provenance), `costs` (per loop stage), `latency_ms`, `attempts`,
`seed`, `error`, `judge`, `lexical`, `started_at` / `finished_at`.

`judge` carries the verdict **and its instrument**: score, passed, label, kind, judge
model, rubric id/version/SHA-256, rationale, raw output, its own cost, and
`judge_run_id` when the verdict came from a re-judge rather than the original run. A score
without its rubric is not comparable to anything.

### `builds[]` (`BuildRecord`)

`sample_id`, `cache_key`, `cache_hit`, `turns_ingested` (the denominator for per-turn
cost), `sessions_ingested`, `records_written`, `events_appended`, `costs`, `wall_ms`,
`store_bytes`, `error`. Without this the write path is invisible and the firewall's price
is unknowable, which is the whole point of the frontier.

---

## Run-config file

Everything the CLI can set, `--config` can set too; CLI flags override the file. Unknown
keys are **rejected** rather than ignored, because a typo'd toggle that does nothing is
exactly how an ablation becomes a lie.

```yaml
run_id: locomo-lean-001
seed: 0
dataset:
  name: locomo
  limit: 10
system:
  kind: memspine
  template: base
  enabled_memories: [semantic, episodic, associative]
  event_log_mode: rolling
  memory_llm: null          # lean target: no LLM call on the write path
ablation:                   # omitted keys take the reference value
  entity_extraction: false
budget:
  total_context_tokens: 8192
  retrieved_tokens: 4096
  top_k: 10
generation:
  backbone: { provider: openai, model: gpt-4o-mini, temperature: 0.0 }
judge:
  kind: graded_reference
  model: { provider: openai, model: gpt-4o-mini, temperature: 0.0 }
  rubric_version: v1
  pass_threshold: 0.5
```

`--out` is still recommended even with a config file: `run_id` is regenerated per
invocation (and per rung) so two runs cannot overwrite each other's results.

---

## Blockers — read before the first real run

1. **The `benchmark` template blocks the frontier.** `benchmark.yaml` sets a `graph:`
   block, and memspine rejects a `graph:` block when the memory that projects it is
   disabled — which the `lean` rung does. An override layer cannot unset a key a template
   set, so the driver refuses with the fix spelled out. Until the `graph:` block is
   dropped from that template (its value is already the schema default), use
   `--profile base`. The template is outside this directory's ownership.

2. **Both seams in *What is not built* must exist.** Nothing runs without them.

3. **Corpora must be downloaded.** Nothing here vendors data.
   - LoCoMo: Snap Inc.'s, under its own terms. Pass `--data-path`, or set
     `MEMSPINE_LOCOMO_PATH`.
   - LongMemEval: `xiaowu0162/longmemeval-cleaned` —
     `longmemeval_s_cleaned.json` (~40 sessions/question), `longmemeval_m_cleaned.json`
     (~500 sessions, **~2.7 GB**, parsed eagerly — expect it to need well over 10 GB of
     RAM through the current loader), or `longmemeval_oracle.json`.

4. **API keys** for the backbone and the judge. The harness names models as
   `provider/model` and never reads a key itself; the seam you write owns that.

5. **`reorganize` is an environment change, not a config change.** Leiden self-skips
   unless `memspine[community]` is installed. The `full` rung records intent; install the
   extra or the row overstates what ran.

6. **memspine cannot inject event time.** No public write method takes a timestamp, so
   LoCoMo folds session dates into the turn text and LongMemEval can only convey time
   through ingestion order. Temporal reasoning is 133/500 LongMemEval questions. Decide
   how to report that before publishing a TR number.

---

## What must not be cut, whatever the schedule pressure

- **The (accuracy, tokens, latency) triplet.** Dropping cost to ship a score forfeits the
  only result this harness is uniquely able to produce.
- **Determinism.** Same log ⇒ same result is a claim memspine can make and most peers
  cannot. Temperatures default to `0.0` for this reason.
- **Pricing the firewall rather than hiding it.** The `lean` rung turns it off so the
  `firewall` rung has something to be a delta *from* — a deliberate deviation from
  PLAN §2.2, recorded in `run.py` beside the ladder. What matters is that the rung above
  turns it back on and prices it, and that the caveat says precisely what the off-row
  disabled.
- **Re-running peers under this harness.** A comparison against numbers copied from
  someone else's paper, with a different judge and backbone, is not a comparison.
  `SystemUnderTest.EXTERNAL` exists so peer systems are re-run here, and `full_context` /
  `no_memory` exist as the cost ceiling and the accuracy floor.

## Reading list

- `docs/memspine-structure-plan.md` — decision register, E1–E9 (the ablation matrix)
- `docs/ARCHITECTURE_FLOWS.md` §2 — the write / read / sleep flows the toggles switch
- `docs/adr/` — one decision, one ADR; harness decisions belong there too

## SpineTune: guarded self-tuning of the read configuration (G-23)

`python -m memspine_evals.spinetune` searches memspine's existing read keys for a better
configuration. It avoids the leaks of SimpleMem's EvolveMem, which tunes on the reported
questions and lets an LLM read their gold answers.

| Control | What it does |
|---|---|
| `--space` | JSON list of knobs: dotted config keys and their allowed values. Only existing keys are searched; no LLM proposes changes, and no answer is ever seen |
| `--algo` | `coordinate` (one key at a time, repeated passes), `random` (sampled configs against the incumbent) or `halving` (successive halving: many configs on a small dev subset, survivors on larger ones) |
| `--dev-fraction`, `--seed` | Conversations are split into dev (search) and held-out (confirmation). The search never sees held-out items |
| `--alpha`, `--max-trials` | A change is accepted on dev only if the paired exact sign test has p < `alpha / max_trials` (Bonferroni over every try) |
| `--min-delta` | ...and the coverage gain is at least this many points |
| `--guard-dataset/-path/-items` | A second dataset where the result must not be significantly worse |
| `--max-hours`, `--cache-dir` | Time budget. Every evaluation is cached, so resuming is free |
| `--pythonpath` | Run against a frozen engine copy, as the screens do |
| `--dry-run` | Print the split and the plan, run nothing |

The objective is retrieval-only coverage (`ev_all`, $0, local embedder). The report
(`SPINETUNE.md`, `spinetune.json`) lists every trial and the held-out and guard tests. A
result is labelled **auto-tuned** and reported next to the hand-built baseline, never in
place of it. A default changes only through an ADR, after U5 (two independent corpora).
