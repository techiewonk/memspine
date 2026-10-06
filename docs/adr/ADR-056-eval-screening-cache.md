# ADR-056: Screen memory-side changes with retrieval-only runs and a disk cache of paid calls

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** feat/eval-screen-cache. Harness only (`evals/`); no engine code, config key or
  prompt changes.

## Context

A paid LoCoMo QA run of one arm costs dollars and hours, and most of the arms we try change only
the memory side (what is stored, ranked or assembled). Two costs dominate a screen of such an arm
and neither tells us anything new: the reader and judge calls, and the embedding and engine-role
calls that repeat what an earlier run already paid for (the same turns, the same questions, the
same mining prompts).

## Decision

1. **`c0-1 --retrieval-only`.** Ingest and read every question exactly as the QA run would (same
   engine config, budget, `top_k`, read mode, sleep, batching); skip the reader and the judge.
   Rows record `retrieved_ids` (after the budget cut), `context_tokens`, the gold evidence and
   `ev_all` / `ev_any` / `ev_frac`; `summary.json` a per-category `coverage` block. Protocol id
   `c0-1-retrieval-only`, reader model `none`: a coverage number can never be read as accuracy.
2. **`c0-1 --cache-dir PATH`.** A content-addressed SQLite cache at the LiteLLM boundary
   (`litellm.acompletion` / `litellm.aembedding`, wrapped the same way the stub transport
   replaces them). Embeddings are cached per text (model, input type, dimensions, text; float64
   bit-exact); completions on model, full messages, temperature, `max_tokens`,
   `response_format` and the other sampling options, as SHA-256 of canonical JSON. A hit returns
   the stored reply with zeroed usage, is not counted as a model call, costs $0 and is reported
   (`cache_hits`, `cache_misses`, `usd_saved`).
3. **What is never cached.** A completion with a non-zero temperature (or `n` > 1, streaming).
   Reader and judge completions, which the runner marks with a context variable, unless
   `--cache-reader` is given.
4. **Absent temperature is cacheable.** The engine's roles send no temperature, so the provider
   default applies. Their requests are keyed with `temperature: null` and cached: a screen is a
   replay of the populating run's replies, which is what comparing a memory-side change against
   that run needs. An explicit non-zero temperature is refused.

## Consequences

- A read-side arm screened against a populated cache costs about $0 (LoCoMo conv-26, combo-A
  config, replay read: 618 embedded texts, all hits, ~3.5 minutes on the stub-timed path). An arm
  adding write-time LLM roles pays for its new calls once; its re-screens are free.
- Coverage credits raw turns only; an arm whose gain is in derived records (mined facts, cards)
  reads low and should go to the paid 2-subset QA screen.
- A prompt that embeds a per-run random value (a fresh record id, the wall clock) never hits;
  `cache_misses` in the summary makes that visible.
- Old runs are untouched: without the flags the runner, its rows and its summary are unchanged.
