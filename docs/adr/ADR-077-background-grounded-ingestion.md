# ADR-077: Raw-first ingestion with durable grounded derivation

**Status:** Accepted policy; implementation design in review
**Date:** 2026-10-18
**Deciders:** Owner accepted background grounded facts/annotations, graph mention
edges, and recoverable raw data with lossless compression and separate summaries.

## Context

Current ingestion persists raw episodic data and search projections. Window
extraction/outbox/graph are optional. GLiNER annotations do not automatically
feed graph entities and depend on fact envelopes on the window path. Raw
acceptance, derivation readiness and external projection completion differ.
See [current trace](../INGESTION_STORAGE_TRACE.md).

## Decision

Retain one canonical gateway. Raw policy-approved source remains recoverable;
background processing creates grounded facts and independent source-bound
annotations. Graph mention occurrences are unasserted, separate from factual
relations. Lossless encoding may change raw storage bytes; lossy summaries are
separate derived memory. Authoritative events and durable required-work state
support replay and recovery; transport is not a second source of truth.

Accept these policies without claiming any new behavior is implemented. Exact
contracts, new annotation event vocabulary, limits and activation are reviewed
in [the implementation plan](../BACKGROUND_INGESTION_PLAN.md).

## Options considered

| Option | Assessment |
|---|---|
| Raw plus search only | Cheap but does not meet selected facts/annotation target |
| Synchronous full extraction before ingestion returns | Couples availability/latency to generation; not selected |
| Durable raw-first plus background grounded derivation | Selected; more explicit readiness/recovery work |
| Treat mentions as asserted relationships | Rejected: occurrence is not factual support |
| Lossy replacement of retained raw source | Rejected; summaries remain separate |

## Consequences

Source-version/erasure-aware jobs, fenced idempotent effects, complete annotation
persistence and occurrence-preserving graph projection are required before
activation. Raw success cannot masquerade as all-layer readiness. Optional model
bindings and queue quotas must fail visibly when the target policy needs them.
Retention, authorized erasure and legal hold remain authoritative. No provider
copying, historical re-embedding, alternate ingestion path or compatibility layer.

## Action items

- [ ] BI-01 strict contracts/configuration.
- [ ] BI-02/03 durable acceptance/discovery before default activation.
- [ ] BI-04..09 isolated windows, grounded outputs, fenced commit, graph and readiness.
- [ ] BI-10/11 recoverable encoding and separate summaries.
- [ ] BI-12 focused integration/recovery gates and application-profile promotion.
