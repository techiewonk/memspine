# ADR-013 — Memory-Firewall & M7 governance amendments to the event-sourced contract

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-47
- **Phase:** P4 (re-review consolidation) · **Tier:** DF

## Context

P4 shipped the Memory Firewall (E1) and M7 hard erasure. A post-merge multi-agent
review found that four implementation choices silently amend architectural
invariants that ADR-001/the structure plan state absolutely. All four are the
*right* choices — this ADR makes them explicit instead of leaving them in code
comments and commit messages.

## Decision

1. **The log is append-only for *structure*, content-mutable only under M7.**
   `FORGET(hard)` triggers `redact_event_payloads()`: one shared walker
   (`core/erasure.py`) scrubs every content carrier of the erased record —
   snapshots, `history[*]` archived versions, merge-absorbed duplicates,
   cold-tier `set` deltas — in place. Event count, seqs, kinds, actors and the
   audit chain are never touched. `verify_forget` uses the *same* walker as its
   proof, so redactor and verifier cannot develop disjoint blind spots.
   Rejected: crypto-shredding (per-record keys — key store becomes a second
   erasure surface), segment rewriting (breaks seq stability), tombstone-only
   (content survives; fails GDPR erasure).

2. **The firewall gate is deterministic-only.** Trust matrix + regex
   instruction-shape + embedding-outlier/MINJA heuristics; no LLM in the loop,
   so the defense itself cannot be prompt-injected. The shipped
   `firewall_flag.yaml` prompt is a *reserved optional second-stage* escalation
   (never the sole gate); it is intentionally unwired in v0.1.

3. **Quarantine has one canonical representation.** `RecordStatus.QUARANTINED`
   is canonical; the `quarantined` boolean column is its projected index (fast
   filters, P0 DDL). Writers must set both together — the only sanctioned
   writers are `FirewallVerdict.apply` (in) and the corroboration promotion
   path (out). `corroborations` (migration 0005) joins the E1 column set.

4. **Lifecycle deltas ride `DECAY_TRANSITION` behind an allow-list.**
   Quarantine promotion, skill-stage transitions (P5) and page-outs reuse the
   P3.1 delta mechanism rather than minting new event kinds. The record
   projector enforces an allow-list of patchable lifecycle fields; `trust` and
   identity/content fields (except the M6 compress pair) are dropped loudly —
   a delta can never launder a record past the firewall.

5. **Trust policy config lives at `memories.semantic.policies.trust`** (the
   D-14 channel) even though the gate covers every memory type — one binding
   point, documented here. All trust options are bounds-validated (`[0,1]`
   floats, promotion floor ≥ 1); an out-of-range override is a startup error,
   never a silent cap defeat. Derived writes (consolidation summaries,
   reflections, ingest chunks) inherit/re-earn trust through the same gate —
   a summary is never more trusted than its least-trusted member.

## Consequences

- Positive: the four invariant amendments are now contract, testable and
  reviewable; `verify_forget` provability extends to history/merge/cold-tier
  carriers; delta events have a typed blast radius.
- Negative / cost: hard deletes mutate stored bytes (append-only purists must
  read this ADR); the delta allow-list must grow deliberately when new
  lifecycle fields land (a forgotten entry fails loudly in tests).
- Follow-up: E3 persistent caches (LMDB, D-09) must join the `verify_forget`
  proof surface when they land (reported *unproven*, never clean); a dedicated
  quarantine-lifecycle event kind may replace the `DECAY_TRANSITION` reuse if
  M11 consumers need to distinguish them.
