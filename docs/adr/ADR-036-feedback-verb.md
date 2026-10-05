# ADR-036 — Feedback verb: like / dislike / note as events feeding utility

- **Status:** accepted
- **Date:** 2026-10-05
- **Decision id:** register row to be assigned at merge (master absorb list #54, MS-2)
- **Phase:** P7 · **Tier:** QW

## Context

The M1 utility modifier (`read.scoring.utility_weight`) has only one input today:
reinforcement-on-read (each RETRIEVE adds a small, capped step). That measures what
the engine served, not whether it helped. Users and host applications have a direct
signal (thumbs up / down, a correction note) and no door to give it to memory.

## Decision

- **`Engine.feedback(record_id, signal, *, note=None, actor="user", namespace=...)`**
  with `signal` in `like | dislike | note` appends one **`memory.feedback`** event
  (`EventKind.FEEDBACK`): `{"record_id", "signal"}` plus `"content"` holding the note
  when one is given. REST: **`POST /feedback`** (`FeedbackRequest`).
- The record projector keeps per-record counts in `scoring` (`likes`, `dislikes`,
  `notes`); like RETRIEVE, counting is at-least-once. The fields are omitted from a
  record's dump while zero, so unrated records serialize and fingerprint exactly as
  before.
- Utility becomes `scoring.utility + tanh((likes - dislikes) / FEEDBACK_UTILITY_SCALE)`
  (scale 3, `config/constants.py`). The feedback term is bounded in (-1, 1) and zero
  without feedback, and `utility_weight` multiplies it as before: the `base` template
  pins `utility_weight: 0`, so the default read path is unchanged; with a weight > 0 a
  disliked record sinks and a liked one rises, but repeated likes saturate.
- The note is screened like message content (`firewall.redact_secrets` / `pii`), cut
  to `FEEDBACK_NOTE_MAX_CHARS` (2000) and stored in the event only. It sits under the
  `content` key so the erasure walker (`core/erasure.py`) scrubs it on a hard forget of
  the record, and `verify_forget` proves it.
- Namespace-scoped: a missing, foreign or forgotten id raises the same
  `ConflictError` (no existence oracle).

## Consequences

- Positive: a real usage signal into ranking, through the log, so a rebuild replays the
  same counts; bounded, so a feedback flood cannot outrank relevance by more than
  `utility_weight`.
- Negative / cost: feedback is unauthenticated beyond the namespace seam (ADR-017);
  under `utility_weight > 0`, a caller who can reach the namespace can nudge its own
  ranking within the bound. Counts are approximate across crashes (at-least-once).
- Follow-up: a per-principal weight (B7 reputation) for feedback, and a sleep-stage
  consumer that turns notes into corrections (they are only stored today).

## Alternatives rejected

- **Writing likes straight into `scoring.utility`** — mixes two signals and makes the
  transform unrecoverable; counts keep both raw and replayable.
- **A separate feedback table** — needs a migration on both backends and a second
  projection; the counts are per-record state and fit the existing `scoring` blob.
- **Unbounded linear utility** — a spammed like count would override relevance.
