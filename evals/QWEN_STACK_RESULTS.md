# Local Qwen stack: LoCoMo retrieval-only grid

Dataset: LoCoMo (10 conversations, 1,986 questions, all categories). Mode: `--retrieval-only`,
zero model-API calls, $0. Engine base: `local-combo-A`, budget 4096, top-k 10, replay read mode.
Hardware: one RTX 5080 (16 GB), two arms in parallel. "Sufficiency" is retrieval sufficiency, **not
answer accuracy**. Run ids: `qs-<embedder>-<reranker>`; arms in `evals/arms/`.

Embedders: `ebge` = fastembed BAAI/bge-small-en-v1.5 (384-d, CPU); `ebgeb` = bge-base-en-v1.5;
`eq06` = Qwen3-Embedding-0.6B (BF16, GPU, 1024-d, query instruction).
Rerankers: `roff` none; `rbge` bge-reranker-base (fastembed); `rq06` Qwen3-Reranker-0.6B;
`rq4b4` Qwen3-Reranker-4B, 4-bit (bitsandbytes).

| Embedder | Reranker | Sufficiency | Ctx tokens (mean) | Wall-clock |
|---|---|---|---|---|
| bge-small | none | 0.594 | 1508 | 1334 s |
| bge-small | bge-reranker-base | 0.579 | 971 | 1775 s |
| bge-small | Qwen3-Reranker-0.6B | 0.573 | 789 | 1782 s |
| Qwen3-Emb-0.6B | none | **0.615** | 1567 | 1321 s |
| Qwen3-Emb-0.6B | bge-reranker-base | 0.602 | 1001 | 1745 s |
| Qwen3-Emb-0.6B | Qwen3-Reranker-0.6B | 0.593 | 871 | 1685 s |
| bge-small | Qwen3-Reranker-4B 4-bit | pending | pending | pending |
| Qwen3-Emb-0.6B | Qwen3-Reranker-4B 4-bit | pending | pending | pending |
| bge-base | none / bge-reranker-base / Qwen3-Reranker-0.6B / 4B 4-bit | pending | pending | pending |

Pilot (2 items) timings, for estimating: 167 s (bge-small), 184 s (Qwen3-Emb), 197-242 s with a reranker.

## Readings so far
- Qwen3-Embedding-0.6B beats bge-small by 2.1 points of sufficiency at the same time cost.
- Every reranker so far lowers sufficiency by 1-2 points and cuts context by 35-50%.
- Rerankers add about 30-35% wall-clock. The 2-item pilot did not predict the sufficiency ordering.

## Not yet valid
- Qwen3.5-9B QA smoke (`qa-smoke-q35`): all scores 0.000 because the run hit `--max-model-calls 150`
  with 603 queries in scope (`--items 1` did not limit the query set), so answers were unattempted.
  Not a result. Open: `/no_think` behaviour for Qwen3.5, and a 9B judge versus earlier judges.

## Throughput per token (`evals/bench_models.py`, 2026-10-09, RTX 5080)
Measured while the grid shared the machine (GPU 8-99% busy), so treat as conservative. Embedders: 256
passages (~70 tokens) in batches of 32, plus batch 1. Rerankers: one query vs a pool of 30 passages.
LLM: Ollama, thinking off, Q4_K_M.

| Model | Backend | tokens/s | ms/token | Other |
|---|---|---|---|---|
| bge-small-en-v1.5, batch 32 | fastembed ONNX (CPU) | 1,246 | 0.80 | 36 texts/s |
| bge-small-en-v1.5, batch 1 | fastembed ONNX (CPU) | 361 | 2.77 | |
| bge-base-en-v1.5, batch 32 | fastembed ONNX (CPU) | 365 | 2.74 | 10 texts/s |
| bge-base-en-v1.5, batch 1 | fastembed ONNX (CPU) | 119 | 8.42 | |
| Qwen3-Embedding-0.6B, batch 32 | torch bf16 (GPU) | 7,360 | 0.136 | 196 texts/s |
| Qwen3-Embedding-0.6B, batch 1 | torch bf16 (GPU) | 91 | 10.99 | |
| jina-embeddings-v5-text-small, batch 32 | torch bf16 (GPU) | 6,756 | 0.148 | 185 texts/s |
| jina-embeddings-v5-text-small, batch 1 | torch bf16 (GPU) | 81 | 12.36 | |
| bge-reranker-base, pool 30 | fastembed ONNX (CPU) | 4,529 | 0.221 | 91 pairs/s |
| Qwen3-Reranker-0.6B, pool 30 | torch (GPU) | 6,325 | 0.158 | 139 pairs/s |
| Qwen3-Reranker-4B 4-bit, pool 30 | bitsandbytes (GPU) | 1,617 | 0.618 | 35 pairs/s |
| jina-reranker-v3.5, pool 30 | torch listwise (GPU) | 8,943 | 0.112 | 196 pairs/s |
| Qwen3.5-9B Q4_K_M prefill | Ollama | 4,869 | 0.205 | |
| Qwen3.5-9B Q4_K_M decode | Ollama | 115 | 8.72 | |

Reading: batching the GPU embedders is 80-90x cheaper per token than one text at a time, so runs should use
`--memspine-batch-turns 32`. Jina reranker v3.5 is the fastest reranker and Qwen3-Reranker-4B 4-bit the slowest.

## Qwen3.5-9B Q4_K_M: thinking off vs on (LoCoMo conversation 1, categories 1-4, 152 questions)
Reader and judge both Qwen3.5-9B via Ollama; only the reader thinks. Retrieval: Qwen3-Embedding-0.6B, no
reranker, batched writes. Thinking off sends `reasoning_effort: none` (the `/no_think` switch does not work on
Qwen3.5). Thinking on allows 4,096 completion tokens.

| Setting | Headline accuracy | Answered | Truncated | Tokens/answer | Answer latency | Run time |
|---|---|---|---|---|---|---|
| thinking off | 0.730 | 152 | 0 | 45 | 1.2 s | 320 s |
| thinking on | 0.592 | 103 | 49 | 1,017 | 8.9 s | 1,834 s |

On the 103 questions both settings answered, accuracy is identical: **0.806 vs 0.806** (think-on wins 5,
loses 5). The 49 truncated answers hit the token limit; think-off scores 0.571 on those same questions.
Thinking costs 5.7x the run time and 22x the tokens for no measured gain, so keep it off for this task.

**Correction (judge artifact).** Of the 49 truncated think-on answers (all empty strings), the Qwen3.5-9B judge
marked 7 as correct, so the harness headline 0.592 includes 7 credits for empty answers. `make_sota_table.py`
scores any unanswered or truncated question as a miss, giving think-on 54.6 vs think-off 73.0 on the same 152
questions. The matched-question comparison above (0.806 vs 0.806 on the 103 both answered) is unaffected.
A small judge can credit an empty answer; a stronger or rubric-checked judge is needed for publishable numbers.

## Consolidated retrieval grid (full LoCoMo, 1,986 questions) - updated 2026-10-09 10:40
Raw per-arm summaries are tracked in `evals/results_qwen_stack/<run>/` (COMPARISON.md, summary.json); the
large `runs/` folder is gitignored. "bt32" = batched writes (`--memspine-batch-turns 32`), which halves wall-clock.

| Embedder | Reranker | Sufficiency | Ctx tokens | Wall-clock |
|---|---|---|---|---|
| bge-small | none | 0.594 | 1508 | 1,334 s |
| bge-small | bge-reranker-base | 0.579 | 971 | 1,775 s |
| bge-small | Qwen3-Reranker-0.6B | 0.573 | 789 | 1,782 s |
| bge-small | Qwen3-Reranker-4B 4-bit | 0.575 | 695 | 3,318 s |
| Qwen3-Embedding-0.6B | none | **0.615** | 1567 | 1,321 s |
| Qwen3-Embedding-0.6B | bge-reranker-base | 0.602 | 1001 | 1,745 s |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | 0.593 | 871 | 1,685 s |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-4B 4-bit | 0.597 | 774 | 3,294 s |
| Jina v5 text-small (bt32) | none | 0.610 | 1517 | 662 s |
| Jina v5 text-small (bt32) | Jina reranker v3.5 | running | | |
| Qwen3-Embedding (bt32) / bge-small (bt32) | Jina reranker v3.5 | running / queued | | |
| bge-base | various | queued last | | |

## FINAL - complete retrieval grid (16 arms, full LoCoMo, 1,986 questions, retrieval-only)
Time marked * used batched writes (`--memspine-batch-turns 32`); unbatched runs are about 2x slower.

| Embedder | Reranker | Sufficiency | Ctx tokens | Time |
|---|---|---|---|---|
| bge-small | none | 0.594 | 1508 | 22 min |
| bge-small | bge-reranker-base | 0.579 | 971 | 30 min |
| bge-small | Qwen3-Reranker-0.6B | 0.573 | 789 | 30 min |
| bge-small | Qwen3-Reranker-4B 4-bit | 0.575 | 695 | 55 min |
| bge-small | Jina reranker v3.5 | 0.577 | 649 | 18 min* |
| bge-base | none | 0.592 | 1523 | 45 min* |
| bge-base | Jina reranker v3.5 | 0.579 | 651 | 47 min* |
| Qwen3-Embedding-0.6B | none | **0.615** | 1567 | 22 min |
| Qwen3-Embedding-0.6B | bge-reranker-base | 0.602 | 1001 | 29 min |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | 0.593 | 871 | 28 min |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-4B 4-bit | 0.597 | 774 | 55 min |
| Qwen3-Embedding-0.6B | Jina reranker v3.5 | 0.597 | 673 | 13 min* |
| Jina v5 text-small | none | 0.610 | 1517 | 11 min* |
| Jina v5 text-small | Qwen3-Reranker-0.6B | 0.586 | 838 | 18 min* |
| Jina v5 text-small | Qwen3-Reranker-4B 4-bit | 0.589 | 741 | 23 min* |
| Jina v5 text-small | Jina reranker v3.5 | 0.590 | 654 | 13 min* |

## FINAL - full LoCoMo QA in the published format (1,540 questions, categories 1-4)
Qwen3.5-9B Q4_K_M as reader and judge, thinking off, batched writes. Unanswered or truncated = miss. The judge
is a local 9B model, so these are NOT directly comparable with the GPT-4-class-judged published numbers below;
context tokens per question are comparable. Table also in `results_qwen_stack/SOTA_FORMAT_TABLE.md`.

| System | Single-hop | Multi-hop | Temporal | Open-domain | **Overall** | Ctx tok/q | Answer p50 / p95 | Answered | Run time |
|---|---|---|---|---|---|---|---|---|---|
| bge-small, no reranker | 86.1 | 50.0 | 67.0 | 29.2 | **71.9** | 1,513 | 1.0 s / 1.8 s | 1,535 / 1,540 | 58 min |
| Qwen3-Embedding-0.6B, no reranker | 87.2 | 56.0 | 70.4 | 29.2 | **74.4** | 1,571 | 1.0 s / 1.9 s | 1,536 / 1,540 | 54 min |
| Jina v5 + Jina reranker v3.5 | 85.9 | 51.8 | 64.2 | 19.8 | **71.0** | 655 | 0.8 s / 1.6 s | 1,538 / 1,540 | 48 min |

Published (LoCoMo LLM-judge, overall; not re-run): Mem0 vendor 92.5 (~6,956 tok), Mnemon 91.7 (3.8k), MemOS 88.83
(5.4k), Cognee 83.48, EverMemOS 82.75, Hindsight 81.99 (24.7k), Mem0 OSS 77.68 (17.4k), Letta 77.12, Zep 63.83 (1.9k),
A-Mem 61.4. Sources: `scraped_mem0_benchmark_2026.md`, `SOTA_SYSTEMS_UPDATE_2026-10-02.md` in memory-research.

Findings: (1) Qwen3-Embedding-0.6B is the best embedder on both retrieval (0.615) and QA (74.4), +2.5 QA points over
bge-small. (2) Every reranker trades 1-2 sufficiency points (and 3.4 QA points for Jina v5 + Jina reranker) for 40-60%
less context. (3) Thinking mode does not help Qwen3.5-9B here. (4) Batching halves wall-clock. (5) Weakest category is
open-domain (20-29). Caveats: single run per arm, no confidence intervals, local 9B judge that can credit empty answers.

## Qwen3-Embedding-0.6B + Jina reranker v3.5, full QA (Qwen3.5-9B Q4_K_M reader + judge, 1,540 questions)
| System | Single-hop | Multi-hop | Temporal | Open-domain | **Overall** | Ctx tok/q | Answer p50 / p95 | Answered | Run time |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3-Embedding-0.6B, no reranker | 87.2 | 56.0 | 70.4 | 29.2 | **74.4** | 1,571 | 1.0 s / 1.9 s | 1,536 / 1,540 | 54 min |
| **Qwen3-Embedding-0.6B + Jina reranker v3.5** | 85.4 | 53.2 | 64.2 | 25.0 | **71.3** | 672 | 0.75 s / 1.5 s | 1,540 / 1,540 | 47 min |
Reranking costs 3.1 QA points (mostly temporal, -6.2) for 57% less context and 20% faster runs. It is level with
the bge-small baseline (71.9) while using 56% fewer context tokens. Only run with every question answered.

## Status of each claim (corrections, 2026-10-10)
Read this before quoting anything above. Evidence: `analysis/ENGINEERING_GAPS.md`, `analysis/GAP_REGISTER.md`.

| Claim above | Status | Why |
|---|---|---|
| "Thinking costs 5.7x for no gain" (Qwen3.5-9B think on vs off) | **INVALID - confounded** | all 49 "truncated" think-on answers hit Ollama's 4,096-token window, not max_tokens (gap A9); re-test at 16K pending |
| "localhost cost ~2.1 s per request, ~1.7 h per run" | **OVERSTATED** | 2.1 s is real for plain urllib/httpx calls, but recorded harness latencies were ~1 s; the faster later run came mainly from running alone (gap E1, HAR-6) |
| Reranker arms lose 1-2 points of sufficiency | **valid as measured, mechanism found** | min-max scores + 0.3 relative floor deleted ~half the hits (gap B9); with the floor fix the reranker ties or leads on conv-26 (84.2 vs 83.6) |
| Fixed config 80.1% vs baseline 74.5% (+5.6) | **valid, not attributable** | five changes at once incl. a more lenient judge rubric (gap A7); +10.5 on the tuned conversation vs +5.1 elsewhere (gap A6); single run, CI about +-2.2 (gap A10) |
| Judge = Qwen3.5-9B for all QA numbers | **caveat** | same model as reader; est. 24-50 false negatives and 45-60 partial-list false positives (gaps A1-A3); `presence_penalty 1.5` applied silently to every call (gap A8) |
| Published-format comparison table | **not comparable** | home-made judge, different reader; only context tokens per question are comparable (gap A4) |
| Word vectors (potion) in place of BM25 | **negative on conv-26** | 78.9% vs 83.6% paired (gap B15); 2-conversation screen of BM25 + word vectors running |
| 21+ LoCoMo gold errors | **listed** | `analysis/locomo_errata.json` (24 entries incl. 3 image-only); headline should be reported with and without |
| Development / held-out split | **saved** | `analysis/locomo_split.json`: dev conv-26/30/41/42, report on conv-43/44/47/48/49/50 |
