# ADR-090: Strict ingestion boundary, session-keyed windows and one durable write path

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-114

## Context

Several gaps were confirmed in the 2026-11-01 triage:
- DS-05: `Engine.write` only logs a failed `window.observe`.
- DS-06/DS-07: windows are kept one per namespace, and the restart draft stores no
  source version.
- DS-08: naive and date-only times silently get `read.naive_timezone`.
- DS-09: unknown message fields are accepted.
- RM-42: unresolved candidates are counted only.

## Decision

1. **One durable write path.** `Engine.write` uses the same atomic raw store + manifest +
   outbox path as the gateway. A failed observe is recovered by discovery on restart.
2. **Session-keyed windows.** Open windows are keyed by
   `(namespace, conversation_id or session_id)`. Interleaving never flushes or closes
   another session. Idle and close cuts apply per window. The restart draft carries the
   source version and content hash, and recovery is tested after each of turns 1 to 5, plus
   idle and close cuts.
3. **Strict message envelope.** Known fields are validated, and any other field raises an
   error naming it. Caller extras go in an explicit `metadata` dict that is stored verbatim
   and never interpreted.
4. **Civil time.** An aware timestamp resolves normally. A naive or date-only value is
   stored as source text with precision and no resolved instant, and never gets a guessed
   zone. This applies D-100 Q-S2, and `read.naive_timezone` is removed.
5. **Unresolved candidates persist.** Unresolved and unmapped fact candidates are stored as
   source-bound, unasserted annotations (reason, span, producer revision). They are never
   read as facts. They are visible to inspect, re-resolved on policy or registry activation,
   and erased with their source.
6. **Model-backed path at parity.** The optional `window_extract` path:
   - attaches annotations per source regardless of fact yield (DS-04);
   - gives known-entity context source, version, known-time and visibility (DS-11);
   - refreshes known entities with a bounded cursor and marks capped sets deferred (DS-12);
   - gets a non-generative GLiNER fact extractor behind the configured hook (DS-18).
   The default profile stays non-generative.

## Consequences

Callers that relied on silent extras or naive times get explicit errors or civil text.
Recovery has one story for all writes.

## Amendment 2026-11-01 (direction review)

- `write.stamp_default_tz` is removed together with `read.naive_timezone`. A zone counts
  only when the source or envelope declares it.
- Civil time uses the contract representation (`text_precision`, `precision=unknown`;
  ENRICHMENT_SCHEMA_CONTRACTS).
- The `metadata` bag is stored verbatim, erased with its source and scanned like content
  where a firewall scan exists. It is never interpreted for identity, time or role.
- Window key: `conversation_id` when present, else `session_id`, else the inherited parent
  conversation (RM-36), else a per-namespace default window.
- BI-04 is reopened under the ADR-087 closing rule: its isolation claim is not met until
  session-keyed windows land.

## Amendment 2026-11-02 (RM-163)

- Only a caller-given `conversation_id` or `session_id` keys a window. Message identity still
  assigns every message both ids. An assigned one is flagged in the envelope
  (`conversation_generated`, `session_generated`) and is identity only. Before this, every
  root message without a caller conversation got a fresh id and opened a one-turn window
  that only `write.window_idle_s` closed.
- A reply that inherited its parent's conversation joins the parent's window. The lookup
  follows at most 8 parents.
- Turns in the default window carry no session, so an assigned session never cuts the
  window. The session-closed bookkeeping keeps the message's session.
- The restart draft has one row per open turn, keyed by namespace, stage, window key and
  record id. A write upserts only its own turn and stores no vector. Vectors are
  recomputed from the record on restore. Membership is still published last.
