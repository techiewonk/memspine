# MD-03: AMD iGPU (DirectML) vs CPU for the small models

Date 2026-10-13. Machine: Windows 11, DXGI adapters: 0 = NVIDIA RTX 5080 (never used), **1 = AMD Radeon(TM) Graphics
(2 GB dedicated + shared RAM, driver 32.0.21045.5002)**, 2 = Basic Render. DML always run with `device_id=1`.
Venv: isolated `memspine-md03-venv` (python 3.13, fastembed, onnxruntime-directml 1.24.4, no plain onnxruntime).
Scripts: `evals/bench/md03_{adapters,bench,run,qwen3}.py`. Synthetic multilingual texts (en/de/es/hi/zh/ja), 96 items
(half short, half long, truncated at model max length), 2 timed passes per batch size after a shape warm-up.

**Caveat on the CPU numbers:** the machine sat at ~100% CPU the whole time (other agents' test suites), and ORT's CPU
provider used all ~14 cores contending with them. CPU figures are therefore pessimistic (bge-small bs64 should be far
above 10 items/s on an idle box). DML still lost where it worked at all. While on DML the process used ~1 core
(`proc_cores` 1.0) against ~14 for CPU, so DML does take load off the CPU, but at a large throughput cost.

## Small fastembed models (items/s, p50/p95 per-batch latency ms)

| Model | bs | CPU items/s | CPU p50/p95 | DML items/s | DML p50/p95 | Correctness (DML vs CPU) |
|---|---|---|---|---|---|---|
| bge-small-en-v1.5 (default) | 1/8/32/64 | 37.6 / 12.1 / 10.5 / 9.7 | 20/70, 681/949, 3114/3247, 4948/6724 | **device hang (DXGI 0x887A0006, "GPU will not respond") on first run, with and without DML-recommended session options** | n/a | n/a |
| paraphrase-multilingual-MiniLM-L12-v2 | 1 | 79.1 | 12/24 | 15.3 | 84/98 | min cos 0.99998 |
| | 8 | 86.6 | 93/113 | 23.4 | 341/356 | min cos 0.99998 |
| | 32 | 79.2 | 404/432 | 26.1 | 1214/1270 | min cos 0.99998 |
| | 64 | 67.4 | 749/984 | 27.5 | 1744/2320 | min cos 0.99998 |
| ms-marco-MiniLM-L-6-v2 reranker (query + N docs) | 1 | 106.8 | 8/20 | 13.1 | 57/165 | max abs diff 1.2e-5, same rank |
| | 8 | 39.3 | 223/288 | 7.2 | 1125/1744 | 8.1e-6, same rank |
| | 32 | 25.7 | 1219/1389 | 5.2 | 6587/6787 | 1.0e-5, same rank |
| | 64 | 25.5 | 1872/2594 | 3.8 | n/a (see log) | 1.1e-5, same rank |

(The reranker only ran on DML after setting `enable_mem_pattern=False` and sequential execution; without them it hung
the device exactly like bge-small. fastembed does not set these options.)

Warm-up and memory: first DML call 0.06-0.18 s but each new batch shape cost 3.5-19 s of recompilation (CPU: 1-9 s);
RSS delta CPU 1.0-4.5 GB (arena growth on long inputs) vs DML 0.5 GB for the multilingual model.

## Qwen3-Embedding-0.6B (ONNX export onnx-community/Qwen3-Embedding-0.6B-ONNX, last-token pool, L2 norm, 48 items, max 512 tokens)

Reference = CPU fp32 of the same ONNX graph (torch not installed; CPU ONNX fp32 vs CUDA bf16 cosine was not measured).

| Variant | bs 1 items/s (p50/p95 ms) | bs 8 | bs 16 | min cos vs CPU fp32 | RSS delta |
|---|---|---|---|---|---|
| CPU fp32 (contended) | 4.15 (187/498) | 2.08 (3952/4319) | 1.80 (8881/9022) | reference | 7.3 GB |
| DML fp16 | 0.77 (1169/2836) | 0.40 (20343/22789) | 0.34 (48111/48635) | 0.99997 | 2.8 GB |
| DML fp32 | 0.66 (1213/3402) | 0.29 (28791/31765) | 0.26 (63315/63488) | 1.0000 | 6.2 GB |

Correct but 5-8x slower than the contended CPU, and 100-250x slower than the 5080. Not useful.

## Decision

| Model | Decision |
|---|---|
| bge-small-en-v1.5 | Do not wire: DML crashes the device. |
| multilingual MiniLM (CPU embedder) | Do not wire: correct, 3x slower than CPU. |
| ms-marco MiniLM reranker | Do not wire: correct, 4-8x slower, needs non-default session options. |
| Qwen3-Embedding-0.6B | Do not wire: correct, 5-8x slower than CPU, far slower than CUDA. |

No device option was added to the engine. Recommended setting for screens: leave embedder and reranker on the existing
paths (CUDA for Qwen3-Embedding / jina on the 5080, or CPU for the small models); do not use the iGPU.
