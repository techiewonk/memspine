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

The public `Engine.unresolved_annotations(namespace, viewer=...)` read exposes only
validated annotations backed by a current, live, visible source snapshot in that
namespace. It reads the durable effect artifact, including after restart, and does not
invoke extraction or depend on the observability inspector. A completed empty effect
replaces the prior unresolved set for its sources.

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

## Amendment 2026-11-07 (RM-153 known-time horizon)

- The record projection stores `fingerprint.source_seq`, derived from the canonical WRITE
  event's assigned `seq`. Projector replay derives it again; an incoming record value is
  ignored. A metadata-only WRITE preserves the earlier sequence when content, version and
  canonical source binding are unchanged. A changed source snapshot takes the new WRITE
  sequence. If no sequence is provable, the value stays unknown; valid time, draft order,
  derivation `covers_seq` and the current log head are not substitutes.
- Historical window KNOWN reads use the maximum `source_seq` of the live source snapshots
  actually used by that window, inclusive. Entity and alias index rows are filtered at that
  sequence before ranking or capping. The cursor only advances through a projection
  watermark it has proven; an unproven/capped set stays deferred.
- A batched prompt uses the minimum of each member window's maximum source sequence. If any
  source sequence is unknown, the shared cutoff is zero. A shared older cutoff is reported
  as deferred. Live resolver entries have no canonical known-time provenance and are omitted
  from historical KNOWN; pre-pass hints remain bound to the window's NEW text.

## Amendment 2026-11-08 (RM-151 backpressure handoff)

A cut window leaves the open segmenter before its outbox admission. Its ordered payload,
including window key, conversation, session, source versions and content hashes, is first
written to a separate `window.staged` outbox stream. A session-close marker uses the same
handoff. Only then may the open draft rows be deleted and source membership published. A
hard-cap rejection leaves the staged item durable; ordinary retries use normal admission in
stream order as the runnable backlog clears. Explicit flush and session close retain their
existing force semantics.

Each cut has a stable staging ID. Promotion atomically appends the canonical `window` item
and marks its staged row with the canonical offset. A lost promotion acknowledgment is
reconciled from the canonical item by the runner, without appending another item. Promotion
uses the retained item, progress, and job high-water marks when allocating an offset. Draft
restore skips a source snapshot already held by a staged cut, so interruption between the
handoff and draft deletion cannot replay the cut. Forget scrubs staged source IDs and
snapshots under the erasure fence, including a cut staged before membership existed; a
missing membership gets an inactive tombstone to fence later promotion. Promoted staging
markers are pruned after the canonical contiguous watermark passes them and affected draft
rows have been removed.

Staging, promotion and erasure serialize from their first database read. SQLite takes the
write lock with `BEGIN IMMEDIATE`; PostgreSQL takes transaction advisory locks for the scope
and ordered source IDs, plus row locks on present membership. A delayed stage insertion
checks inactive tombstones, and promotion re-reads its staged row after acquiring the locks.
Either interleaving removes the erased source ID and hash while retaining live members.
Window membership publication takes the same locks before its first read, including when
the membership row is absent. Erasure's tombstone insert and a simultaneous acceptance
therefore have one serial order; acceptance after erasure is refused without a primary-key
race or a missed staged-copy scrub.
