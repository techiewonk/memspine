# ADR-016 — Prospective watches & shared-memory grants (P7)

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-50

## Context

P7 ships the last two memory types: **prospective** (M13.8 — "fire when this
becomes relevant": at a due time, or when the M4 conflict ladder invalidates
a watched fact) and **shared** (R2 — namespace A lets namespace B read A's
records). Both must ride the existing machinery: the event log as the only
source of truth (D0.1), the firewall gate on free-text writes (E1), delta
lifecycle events behind the projector allow-list (D-47), and the ADR-014
no-existence-oracle error shape. This ADR records how each concept maps onto
existing columns and events — **no new DDL, no new EventKind** — and the
trust rule that keeps grants from becoming an injection bridge.

## Decisions

### 1. A watch is an ordinary record; its due time rides `valid_from`

`memory_type="prospective"`, `content` = what to do/remember. Exactly ONE
trigger per watch (both-or-neither refused loudly): a **due watch** carries
its due time in `valid_from` (bi-temporal reuse — "becomes relevant at"; the
column already exists, so no migration 0008); a **target watch** carries the
watched fact key in the existing `entity`/`attribute` columns
(`attribute=None` = every attribute of the entity). `due_at` must be
timezone-aware. Watch content is caller free text: `Engine.watch()` routes
through the full firewall gate — instruction-shaped watches are quarantined
and can never fire.

### 2. Firing is pure and pull-based; invalidation reads M4 CONFLICT events

`triggers.py` is clock-free pure functions: `due_watches(records, now)`
(explicit `now`, always) and `invalidation_watches(records, conflict_events)`
— a target watch fires when a CONFLICT event in its namespace matches its
key, its action **changed the current truth** (`updated` / `invalidated`;
`rejected` and backfill `added` leave the active fact standing), and the
event is at-or-after the watch's `recorded_at` (history predating the watch
never fires it). Only live watches fire (ACTIVATED, never quarantined — E1).
Delivery is **pull-based in v0.1**: `Engine.due(namespace, now=None)` computes
the fired set read-only; the `check_watches` sleep-cycle step only logs fired
counts. Push delivery is deferred to the taskiq build. Known limit
(deterministic, documented): in `event_log.mode: ephemeral` no CONFLICT
events are readable, so invalidation watches never fire there; due-time
watches are unaffected.

### 3. Acknowledge = delta `DECAY_TRANSITION`, `reason="watch_fired"`

A fired watch leaves the pending set only via
`Engine.acknowledge_watch(record_id, namespace)` (under the namespace write
lock): a delta event `{record_id, set: {status: archived, valid_to}}` —
allow-listed fields only, never a snapshot (P3.1/D-47). Idempotent; missing,
foreign, and wrong-type records get the ADR-014 error shape (one message for
missing and foreign — no cross-namespace existence oracle).

### 4. `find_active_fact` is semantic-only (hardening forced by watches)

Watches reuse the `entity`/`attribute` columns, and `find_active_fact` had no
`memory_type` filter — a semantic write sharing a watch's key would have
found the WATCH as the M4 incumbent and archived/superseded it instead of
firing it (the same latent hazard existed for procedural skill names). The
storage query and the port contract now restrict the M4 incumbent to
`memory_type="semantic"`; correspondingly, corroboration's incumbent-join on
promotion applies only to semantic held records (procedural resumes its
ladder status per ADR-014; every other type returns to plain ACTIVATED).

### 5. A grant is a WRITE event of a bookkeeping record — no new EventKind

Grant record: `namespace` = grantor (a grant lives with what it exposes),
`memory_type="shared"`, `entity` = grantee namespace, `attribute="grant"`,
`content` = canonical JSON of the scope (`memory_types: [...] | null`) — the
machine-readable scope IS the human-readable documentation. Grant content is
engine-built, schema-validated JSON (grantee validated by the namespace
grammar, scope validated against the registry), never caller free text — so
it takes the stated firewall exemption shared with prompt sync and persona
(ADR-014 §2); routing deterministic JSON through the embedding-outlier gate
could spuriously quarantine a legitimate grant and silently sever access.
One live grant per (grantor, grantee): identical re-grant is idempotent;
scope change supersedes (old grant archived with `evolve_to`). Revocation
archives via delta `DECAY_TRANSITION` (`reason="grant_revoked"`) and raises
when nothing is live. `grant`/`revoke` hold the **grantor** namespace write
lock.

### 6. The trust rule: foreign reads are capped, live, and non-mutating (E1)

Records read across a grant are foreign content:

- surfaced with `trust = min(record.trust, TRUST_RETRIEVED_CAP)` — never
  their home-namespace trust (retrieved content must not masquerade as
  operator input);
- quarantined and non-ACTIVATED records **never cross** (same gate as
  `search`);
- results are **live views** into the grantor namespace — never copied, no
  second source of truth; provenance is the differing `record.namespace`;
- `shared` bookkeeping records (grants, subscriptions) never cross — a
  grantee must not enumerate the grantor's other grants;
- foreign reads append **no events**: a reader must not mutate grantor state
  (no RETRIEVE reinforcement across the boundary).

The yes/no decision lives in **one place**:
`core/namespace.grant_allows(reader, record_namespace, memory_type, grants)`
— `memories/shared` builds the grant mapping; no reader reimplements the
check.

### 7. Subscriptions are v0.1-minimal standing queries

`attribute="subscription"`, `content` = the query verbatim. Caller free text
⇒ full firewall gate (unlike grants). Pull-based: `Engine.subscriptions()`
lists them and callers feed each query to `Engine.shared_search()`; running
subscriptions in the background and delivering hits is deferred to the
taskiq build (P7's worker leg).

## Consequences

- No migration 0008: both types reuse P0 DDL columns end to end.
- `PipelineStorage` gains `read_events` (a read — the "reads only" contract
  holds); `check_watches` joins `PIPELINES` and `SLEEP_CYCLE_ORDER` after
  `reorganize` (read-only, trivially idempotent).
- Grant and watch records are ACTIVATED rows, so they appear in their OWN
  namespace's `retrieve()`/`search()` surfaces like any record (a watch is a
  memory; a grant is small factual JSON). Only the cross-namespace surface
  filters `shared` bookkeeping.
- `SharedMemory.needs_any = ("semantic", "episodic")` documents the ≥1-of
  disjunction; `core.registry._REQUIRES_ANY` (already present) enforces it,
  auto-enabling `semantic` as the deterministic default (D-13).
- `Engine.describe()` reports `prospective` and `shared`.

## Register row (D-50)

| # | Decision | Ruling |
|---|---|---|
| **D-50** | **Prospective watches & shared grants** | Watch = `memory_type="prospective"` record, ONE trigger: due time rides `valid_from` (bi-temporal reuse, no new DDL) or watched fact key rides `entity`/`attribute` · firing = pure functions (explicit `now`; invalidation from M4 CONFLICT events with truth-changing actions at-or-after the watch) · pull-based v0.1 (`due()`, read-only `check_watches` sleep step; push deferred to taskiq) · acknowledge = delta `DECAY_TRANSITION` `reason="watch_fired"` · `find_active_fact` semantic-only (a watch/skill key must never be the M4 incumbent) · grant = WRITE of a `shared` bookkeeping record (grantor ns, entity=grantee, attribute="grant", canonical-JSON scope; firewall-exempt like prompt sync), revoke = delta archive, grantor-lock · cross-grant reads: trust capped at `TRUST_RETRIEVED_CAP`, quarantined/non-ACTIVATED never cross, live views (never copied), `shared` bookkeeping never crosses, no events appended for foreign hits · enforcement = `core.namespace.grant_allows` (ONE point) · subscriptions = standing-query records, pull-based. (ADR-016) |

## Alternatives rejected

- A new `memory_watch` column or migration 0008 — `valid_from` already means
  "relevant from"; a new column would duplicate bi-temporal semantics.
- A new `EventKind.GRANT` — a grant is new information, which is exactly what
  WRITE carries; a new kind would force every projector to learn it for zero
  read-model difference.
- Copying granted records into the grantee namespace — a second source of
  truth; revocation would have to chase copies, and taint would cross tenants.
- Routing grant JSON through the firewall gate — the embedding-outlier
  heuristic can spuriously quarantine deterministic JSON in a busy namespace,
  silently severing access; the content is engine-built and schema-validated,
  which is the ADR-014 §2 exemption test.
- Firing invalidation watches from record state (superseded_at) instead of
  CONFLICT events — works in ephemeral mode, but ties firing to read-model
  internals rather than the M4 audit vocabulary; the log is the invalidation
  source of truth (D0.1).
- Push delivery in v0.1 — needs a broker/worker story; that is the taskiq
  leg of P7, not the memory-type leg.
