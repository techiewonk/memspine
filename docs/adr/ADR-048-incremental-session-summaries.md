# ADR-048: Incremental session summaries with a periodic full rebuild

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** master absorb list #56 (MS-4). Builds on M2 consolidation (D-42/D-43) and
  ADR-039 (a summary's parents are its member turns, so erasure cascades).

## Context

Consolidation writes one summary per *closed* session and rewrites it from all turns whenever
the membership changes. A long, still-open thread therefore has no session-level summary at all,
and a session that keeps growing pays a full-transcript call per sweep. The usual fix, a running
summary, drifts: each update rewrites the previous summary, and errors compound.

## Decision

`memories.episodic.policies.consolidation.session_summary: {incremental, rebuild_every}`,
default `{false, 8}`. Off, consolidation is unchanged.

1. With `incremental`, open sessions are summarised too. A summary whose session gained turns is
   updated with **one** call to the new `summarize@incremental` prompt (a variant of the
   `summarize` role) over the previous summary plus **only the new turns**: one call per update
   batch.
2. Each summary carries `summary_since_rebuild:<n>`, the turns folded in since its last full
   rebuild. When `n` would reach `rebuild_every`, or there is no previous version, or a member left
   the session (its parents are no longer a subset), the summary is **rebuilt** from every turn
   with the base `summarize` prompt. Without a `summarize` role the deterministic extractive summary
   is rebuilt each time (it cannot drift).
3. Every version is a new record: parents = all member turns, trust = their minimum, instruction
   flag inherited (B9). It supersedes the previous version (`evolve_to` chain). Flagged turns, and
   a flagged previous summary, reach the model wrapped as data.
4. A summary of an open session is tagged `summary_open`, and its CONSOLIDATE event carries
   `open_session: true`. The derived stages (`mine_facts`, `anticipate`, `reflect_profile`,
   `predict_calibrate`) ignore such events. When the session closes with no new turns, the summary
   is re-stamped without a call (`summary_mode: close`), and the derived stages then see the
   session once. CONSOLIDATE events record `summary_mode` (`rebuild`, `incremental`, `extractive`
   or `close`).

## Consequences

- LLM budget for a session of T turns arriving in B batches: about B incremental calls plus
  T / `rebuild_every` rebuilds, instead of one full-transcript call per membership change.
- Erasure needs nothing new: forgetting any turn cascades to every live version through parents.
- Off by default; `tests/unit/test_simple_profile_golden.py` pins the off value. Tests:
  `tests/unit/workers/test_session_summary_incremental.py` (counting fake LLM, exact call budget)
  and `tests/unit/test_predict_calibrate.py::test_engine_incremental_summary_wiring`.
