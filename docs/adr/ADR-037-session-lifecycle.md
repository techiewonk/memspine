# ADR-037 — Session lifecycle: PASSIVE archival of idle sessions

- **Status:** accepted
- **Date:** 2026-10-05
- **Decision id:** register row to be assigned at merge (master absorb list #53, MS-1)
- **Phase:** P7 · **Tier:** DF

## Context

A long-lived user accumulates hundreds of conversations. Every default read searches
all of them, so old sessions compete with current ones for the context budget, and the
hot set grows without bound. Deleting or summarising them away loses evidence; the
decay tiers (M3) move individual records by access, not whole conversations.

## Decision

- Opt-in key **`memories.episodic.policies.sessions.passive_after`**: `null` (default,
  off), a number of seconds, or `"<n><s|m|h|d|w>"` (`"30d"`). Validated at `start()`.
- A *session* is the explicit conversation id a writer stamps on its turns
  (`write_messages(session_id=...)`, `write_episode`), stored as the episodic record's
  `source.message_id`. Records without one are never passivated.
- A new sleep stage, **`session_lifecycle`** (after `check_watches`, before
  `decay_sweep`), marks a session PASSIVE when its newest `recorded_at` is at least
  `passive_after` old, and reopens a passive session that saw a write since. Each
  change is one **`memory.session`** event (`EventKind.SESSION`):
  `{"session_id", "state": "passive"|"active", "record_ids", "reason"}`. The record
  projector sets `scoring.passive` on the listed records. The wall-clock decision is
  made once and logged, so a rebuild replays exactly the same passive set.
- A write to a passive session reopens it at once (`state: active`,
  `reason: new_write`) under the namespace write lock. The engine keeps a per-namespace
  cache of passive session ids, loaded on the first episodic write and dropped after
  every sleep and rebuild.
- Reads: `search`, `assemble`, `read` (all modes, including `full` and replay
  neighbours) and `retrieve` leave PASSIVE records out by default. They are included
  with `include_passive=True`, when the read's `session_id` names the session, or when
  `group_id` names their group. The scope is a context variable set by the public
  verbs, so the internal read paths need no new parameters. `timeline()`,
  `sessions()` and the sleep stages (consolidation, mining) still see them.
- `scoring.passive` is omitted from a record's dump while false, so records serialize
  and fingerprint as before.

## Consequences

- Positive: a bounded hot set for long-lived users with nothing deleted; archival and
  reopening are auditable events; default behaviour is unchanged (the stage reports
  `skipped` and no record is ever passive).
- Negative / cost: passivating or reopening a session costs one event plus one row
  update per member. With the lifecycle on, the first episodic write per namespace
  after a start or sleep lists the namespace's episodic records once. Two processes on
  one file can disagree briefly on a reopened session; the next sleep reconciles it
  (the stage also reopens sessions with a recent write).
- Follow-up: compress passive sessions into the cold tier (M6) in the same stage, and
  a per-namespace horizon.

## Alternatives rejected

- **A PASSIVE record status** — every `status is ACTIVATED` gate (conflict ladder,
  find_active_fact, firewall) would treat archived turns as dead, and reopening would
  have to restore each status.
- **Add-only tags** — tags can never be removed (B-8), so a session could not reopen.
- **Deciding at read time from the clock** — reads would not be reproducible and a
  rebuild could not reproduce what a past read saw.
