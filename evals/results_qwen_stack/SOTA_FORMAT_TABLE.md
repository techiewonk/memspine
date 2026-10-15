| System (local stack: Qwen3.5-9B Q4_K_M reader + judge) | Single-hop | Multi-hop | Temporal | Open-domain | **Overall** | Ctx tok/q | Answer p50 / p95 (ms) | Answered / total |
|---|---|---|---|---|---|---|---|---|
| qs-ebge-roff | 86.1 | 50.0 | 67.0 | 29.2 | **71.9** | 1513 | 1011 / 1800 | 1535 / 1540 |
| qs-eq06-roff | 87.2 | 56.0 | 70.4 | 29.2 | **74.4** | 1571 | 1022 / 1874 | 1536 / 1540 |
| qs-ejina-rjina | 85.9 | 51.8 | 64.2 | 19.8 | **71.0** | 655 | 764 / 1577 | 1538 / 1540 |
| qs-eq06-rjina | 85.4 | 53.2 | 64.2 | 25.0 | **71.3** | 672 | 751 / 1543 | 1540 / 1540 |

**Published systems, same metric (LLM-judge accuracy %, overall LoCoMo; not re-run, judge and reader differ):**

| System | Overall J | Ctx tok/q | Source |
|---|---|---|---|
| Mem0 (vendor page, Apr 2026) | 92.5 | ~6,956 | vendor-run; scraped_mem0_benchmark_2026.md |
| Mnemon (gpt-4.1-mini) | 91.7 | 3.8k | arXiv 2609.36059, per SOTA_SYSTEMS_UPDATE_2026-10-02.md |
| MemOS (OmniMemEval) | 88.83 | 5.4k | vendor-run harness, via Mnemon Table 1 |
| Cognee (OmniMemEval) | 83.48 | n/r | OmniMemEval, via Mnemon Table 1 |
| EverMemOS (OmniMemEval) | 82.75 | n/r | OmniMemEval, via Mnemon Table 1 |
| Hindsight (OmniMemEval) | 81.99 | 24.7k | OmniMemEval, via Mnemon Table 1 |
| Mem0 OSS (OmniMemEval) | 77.68 | 17.4k | OmniMemEval, via Mnemon Table 1 |
| Letta (OmniMemEval) | 77.12 | n/r | OmniMemEval, via Mnemon Table 1 |
| Zep (OmniMemEval) | 63.83 | 1.9k | OmniMemEval, via Mnemon Table 1 |
| A-Mem (gpt-4.1-mini, Mem++) | 61.4 | n/r | third-party, SOTA_SYSTEMS_UPDATE_2026-10-02.md |
