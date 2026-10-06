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

**Datasets (A4-5, first half):** LoCoMo and LongMemEval S/M/oracle. Tier 2 and tier 3 are not
written yet.

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
- **Rehearse before paying:** `python evals/rehearse.py --plan evals/plans/aamas_runs.json --path data/locomo10.json --price bedrock/converse/qwen.qwen3-32b-v1:0=IN,OUT` runs every planned arm on one conversation over a local stub transport (no network) and writes `PROJECTION.md` with the projected cost of the full LoCoMo cats 1–4 run.
