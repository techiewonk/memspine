# ADR-071: Replace DBOS with Temporal and embedded Resonate

**Status:** Accepted — maintenance runner implemented and locally verified (live Temporal Server + local Resonate gate: 9 passed; see [provider verification](../provider_verification/README.md)). Still pending: ACD-04/07 ingestion outbox-to-Temporal integration (the ingestion outbox remains its own inline/taskiq transport) and ACD-12 deployment canary ([directive §7](../TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md)).  
**Date:** 2026-10-17  
**Decider:** Repository owner  
**Supersedes:** ADR-005 durable DBOS implementation; D-16 durable runner choice

## Context

The owner selected an external Temporal Server for macro-orchestration and
strictly embedded Resonate for local micro-orchestration. The inspected
`resonate-sdk==0.8.1` uses in-memory promises in embedded mode; it does not expose
a native SQLite promise store. The owner therefore selected an application-owned
SQLite step-result journal. Backward compatibility and automatic database/history
copying are explicitly outside this change.

## Decision

- Remove the DBOS dependency, extra, selector, runner implementation and active
  configuration field. Reject old configuration; do not translate or retain aliases.
- Add optional `memspine[temporal]`, containing Temporal and Resonate SDKs.
  `inline` remains the embedded default and `taskiq` remains an independent brokered
  option. The durable `temporal` runner always combines both selected frameworks.
- Temporal Workflow code receives only a versioned serializable job identity.
  Live storage/LLM clients, locks, model objects and `PipelineContext` stay in
  Activities and are reconstructed from the Engine's context factory.
- Register maintenance pipelines before starting the Temporal Worker. Each
  invocation has a unique workflow identity; Temporal retries preserve that identity.
  A later sleep invocation must execute again, rather than reuse a previous cycle.
- Each Activity runs a strictly local Resonate coordinator with ambient remote
  Resonate settings disabled. The application journal stores committed JSON step
  results, rejects conflicting inputs and fences stale journal owners.
- Journal persistence is not native Resonate durability and not an exactly-once
  external side-effect guarantee. An interrupted leaf may run again. Existing
  pipeline idempotency/event-source rules remain required.
- Resonate deadlines, journal lease and concurrency settings are explicit;
  Temporal connection, namespace, queue, retry and execution bounds are explicit.
- The existing ingestion durable outbox remains authoritative for accepted work.
  Its inline/taskiq transport is a separate seam from the maintenance TaskRunner;
  this replacement must not claim it routes ingestion through Temporal implicitly.

## Alternatives considered

| Option | Outcome | Reason |
|---|---|---|
| Keep DBOS | Rejected | Owner requires full replacement |
| Temporal only | Rejected | Owner selected a local dynamic processing framework too |
| Embedded Resonate RAM only | Rejected as recovery policy | Cannot reuse completed micro-step outputs after process death |
| Local Resonate server/sidecar | Rejected | Adds a second server contrary to embedded constraint |
| Temporal + embedded Resonate + application journal | Selected | Separates durable remote history from locally committed step outputs |

## Consequences and deployment constraints

Temporal requires an external Server; a local-only install does not gain a free
remote control plane. The `local` bundle omits the remote orchestration extra;
server templates explicitly opt into Temporal. Historical DBOS ADRs and test
reports remain historical evidence, not executable compatibility code.

Temporal queues do not provide persistent-volume affinity. To reuse SQLite
results, retrying Activities must reach the same durable journal volume and
application store. Multiple hosts with independent local files cannot share that
recovery promise. Queue/deployment isolation and source/config fingerprints prevent
unrelated Engine configurations executing one another's work; storage affinity
still needs deployment configuration and live failure testing.

A journal completion and an external effect are not one distributed transaction.
Uncertain timeouts retain claims until lease expiry; re-execution still requires
idempotency keys, source/event identity or reconciliation. Local journal tests
cannot establish external API exactly-once behavior.

No DBOS system database or history is converted. Operators must stop old workers
before deploying this breaking change and explicitly resolve any old in-flight
work from application-owned checkpoints. No simultaneous old/new job ownership.

## Verification gates

1. New typed settings reject legacy selectors/fields and unsafe deadline/lease values.
2. Default core imports and inline execution work without orchestration SDKs.
3. Real Temporal dispatch executes a registered pipeline through local Resonate.
4. Local journal survives process exit; stale/overlapping owners cannot commit.
5. A distinct later invocation executes again; conflicting retries cannot reuse output.
6. Workflow replay/sandbox, cancellation, worker startup/shutdown and failure paths pass.
7. Dependency lock has no DBOS package; active source contains no DBOS runner.
8. Deployment acceptance separately proves volume attachment, worker replacement,
   service authorization and external-effect reconciliation.
