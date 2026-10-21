# ADR-066: Adaptive-window write (W1): one structured call per window, three call slots

- **Status:** accepted (opt-in, `write.window_extract: off` by default; reads and writes are byte-identical when off)
- **Date:** 2026-10-11
- **Decision id:** W1; structure-plan register row D-101.
- **Sources:** `docs/ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN.html` (Part B, B8, "Decided design"); `docs/DEFERRED_INGEST_STUDY.md` (flush and cancel-on-forget rules); user approval 2026-10-11.

## Context

Write-time LLM work was either absent (a write makes no model call) or ran in the sleep cycle per
closed session. A per-turn call gives each call one turn of context (coreference and relative
dates fail) and costs one call per turn; a per-session call needs the session to close first.

## Decision

Three call slots, at most one call per turn on the user path (target about 0.2 to 0.3 per turn):

| Slot | When | What | State |
|---|---|---|---|
| Call 1, window extraction | per adaptive window, after the write returns | one structured `window_extract` call with separate per-layer sections: `facts`, `edges`, `episode` | built; rollout step 1 enables `facts` |
| Call 2, session close | when a session ends | summary, episode narrative stitched from the fragments, profile updates as guarded operations | interface and stub only (`Engine.close_session`, `workers.pipelines.session_close`) |
| Call 3, sleep | `sleep()` | entity resolution, bi-temporal invalidation, entity summaries | existing stages; `extract_graph` consumes the call-1 edges |

**Window boundary** = the first of: a topic shift (the similarity of two consecutive turns falls
below the store's own running `mean - k * sd`, per namespace, no LLM, `k = 2`), 6 turns, about
1,500 tokens, idle (`window_idle_s`) or session end. Keys: `write.window_extract`,
`window_max_turns`, `window_max_tokens`, `window_topic_k`, `window_idle_s`, `window_split`,
`window_async`, `window_cache`, and the LLM role `window_extract`.

**Per-section fail-closed.** The reply is parsed once, then each section is validated alone; a
malformed section is dropped and counted, the others still land. A fact needs a cited line and a
verbatim `span` in a cited turn (else dropped as ungrounded). `window_split: auto` splits the call
into facts+edges / episode after repeated failures of the joint call.

**Scenarios.** Live: call 1 runs in a background task after the write returns (idle debounced,
per-namespace FIFO). Eval and batch: call 1 runs at `Engine.flush_windows()` (also at
`close_session` and `sleep`), same results.

## Safeguards (the same rules as every derived record)

- Derived records keep `source.parents` = the cited turns (forget cascade, ADR-039), pass the
  firewall, have trust capped at the least trusted parent, role `assistant`.
- Hard-forget race (I72): before a fact is written, the cited parents and every other turn of the
  window must still be live under the namespace lock; a forgotten turn leaves the open and queued
  windows before any call, cancels a window whose call is running, drops pending call-1 edges that
  cite it and deletes the cached replies of windows that held it.
- Never crosses namespaces: one segmenter, queue and edge buffer per namespace; a window holds the
  turns of one session of one namespace.
- Quarantined and instruction-flagged turns never enter a window.
- No benchmark vocabulary in the prompt (the gold lint covers it); the topic level is learned per
  store, `k` is the two-sigma line, the other values are structural limits, none fitted to a dataset.
- Write-quality counters (G-W13) in `Engine.window_stats()`, `describe()["window_write"]`, the
  harness `meta["window_extract"]` and a `window_stats` row of `ingest.jsonl`.

## Consequences

- One more prompt role (`window_extract`, 22 roles) and one more output model (`WindowPayload`).
- The window queue is not durable: a crash loses unprocessed windows (counted at stop as
  `jobs_abandoned`); the raw turns are intact and `sleep` stages still derive from them.
- The in-memory call cache is per process; a durable cache backend makes re-runs free across runs.
