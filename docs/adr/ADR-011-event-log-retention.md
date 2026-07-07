# ADR-011 — Configurable event-log retention: full / rolling / ephemeral + zstd

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-45
- **Phase:** P0 · **Tier:** QW→DF

## Context

The append-only `memory_events` log (ADR-001) grows without bound; high-write deployments (voice transcripts, tool-heavy agents) asked for a way to bound or disable it. But "off" must not silently break the event-sourced contract — rebuild and `audit taint` (E1) depend on the log.

## Decision

`event_log.mode` with three explicit trade-offs:

- **`full`** (default): append-only forever; full rebuild + audit.
- **`rolling`**: `prune_events()` deletes only events that (a) every registered projector has applied — never past the *minimum* high-water mark — and (b) are older than `retention_days`. Rebuild is limited to the window; `Engine.rebuild()` raises outside it.
- **`ephemeral`** ("off"): events receive a monotonic in-memory seq and flow to projectors, but are never persisted. Projections remain the durable state; rebuild/audit are unavailable and startup logs a hard warning.

Plus `event_log.compress`: zstd (level 3) payload compression at rest, reusing the D-32 core dependency — the regulated template combines `full` + `compress` (audit intact, storage bounded); the voice template ships `rolling` + `compress`. Guardrails: `regulated_financial` pins `full`; `StorageService.can_rebuild` exposes the capability so callers fail loudly, not subtly.

## Consequences

- Positive: storage cost is an explicit, per-profile dial; the architecture's rebuild guarantee degrades *loudly* (typed `RebuildUnavailableError`), never silently; prune provably cannot outrun a slow projector (tested).
- Negative / cost: two more states to reason about in every future pipeline touching the log; taint audits are window-limited in rolling mode. Ephemeral-mode seqs and projector offsets are in-memory only (never persisted), so an ephemeral run cannot poison a later full/rolling run on the same file — but records projected during an ephemeral run have no backing events, and a later full-mode `rebuild()` will drop them (inherent to the mode; switching modes on a shared database file is a deliberate operator act).
- Follow-up: P3 schedules `prune_events` in the sleep cycle; P4 `audit taint` reports its effective window; snapshot/checkpoint support could later extend rolling-mode rebuilds.

## Alternatives rejected

- **A boolean `event_log: on|off`** — collapses three distinct trade-offs into a foot-gun; "off" would silently destroy rebuildability.
- **External log shipping (S3/Kafka) before pruning** — real, but a post-v0.1 concern; the port leaves room.
