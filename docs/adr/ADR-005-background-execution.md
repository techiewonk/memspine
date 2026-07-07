# ADR-005 — Background execution: TaskRunner seam, pipelines stay plain functions

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-16 / D-17 / D-18
- **Phase:** P0 contract, P1/P3/P7 runners · **Tier:** DF

## Context

Consolidation/decay/compression must run in the background, but binding the engine to one worker framework is lock-in. The MemOS Redis-Streams scheduler (per-scope queues, claim recovery) validates the brokered design (D-42).

## Decision

Background pipelines are plain, idempotent step functions in `workers/pipelines.py`. Runners *decorate* them: `inline` (default), `dbos` (durable, SQLite→Postgres), `taskiq` (Valkey Streams with per-scope keys + priority labels). No runner imports inside pipeline code. Dead-letter severity: consolidation = warning, M7 hard-delete cascade = alert (D-18).

## Consequences

- Positive: `pip install memspine` works with zero infra; scaling is a config change, not a rewrite.
- Negative / cost: pipelines can't use runner-specific niceties directly; the seam must be tested per runner (crash-resume, flush-on-exit).
- Follow-up: P1 inline runner; P3 dbos; P7 taskiq with per-scope streams.

## Alternatives rejected

- **celery / apscheduler** — heavyweight, wrong grain (anti-decision).
- **Runner-decorated pipeline code** — the exact lock-in D-17 forbids.
