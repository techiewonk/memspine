# ADR-091: Temporal drains the ingestion outbox; the outbox stays the truth

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-115

## Context

The durable ingestion outbox (BI-01..BI-12) runs on the inline and taskiq runners.
`TemporalRunner` registers only `memspine.run_pipeline` (DS-17).

## Decision

The outbox remains the single durable queue and state. A Temporal Activity claims an
outbox offset, runs the registered stage, commits through the same fenced, idempotent
commit (erasure-generation check) the inline runner uses, and acks the watermark. Temporal
adds retries, timeouts and visibility, and never owns ingestion state. Switching runners
loses nothing. Directive §8 tests cover crash between run and commit, a duplicate
delivery, erasure during the Activity, and a runner switch mid-queue.

## Consequences

One adapter to maintain, and no second durable path.
