# ADR-069: remote projection providers and explicit administrative recovery

Date: 2026-10-17. Status: accepted by the owner's instruction to complete the
remaining provider work. Extends ADR-068; no compatibility formats are added.

## Decision

Implement OpenSearch lexical, Weaviate lexical/vector, and pgvector vector
adapters using the existing descriptor/settings/lifecycle contracts. Native
libraries remain optional and lazy. Lexical/vector legs stay independently
selectable, including when one remote product supports both. Portable read
fusion remains the baseline; native hybrid results must not be silently counted
as two independent evidence sources. Never copy authoritative databases or
silently re-embed existing data when selecting an adapter.

Recover an abandoned cleanup fence only through a public typed API with an
application-supplied quiescence guard. The proof binds the deployment, operation,
revision, fence epoch and expiry. Recovery uses a compare-and-swap transaction
and durable audit evidence; elapsed time and an operator boolean are not proof
that a previous owner stopped. Production integration supplies the actual
process/worker verifier.

Provide an executable HTTP control-plane adapter for non-projection service
changes. An authenticated borrowed client carries typed requests under an
explicit total deadline and response-size limit. Activation requires a matching
typed receipt. The remote controller owns actual admission fencing, restart,
cache generation reset and worker handover. No endpoint, credentials, working
deployment controller or native handover is invented by the library.

Improve first-start snapshot construction by deferring redundant frozen-type
validator/serializer creation until needed. Original input validation remains
eager, and the snapshot stays recursively immutable. Serialization and explicit
revalidation are regression-tested. This uses Pydantic's documented
[`defer_build`](https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.defer_build)
option; measure startup and first-use behavior rather than claiming removed work.

## Consequences

New dependencies are isolated in the existing OpenSearch, Weaviate and Postgres
extras. Remote calls require explicit timeouts and scoped credentials. Backend
contract results apply to the versions and operations actually exercised; local
containers cannot establish production capacity, failover or access policy.

`inspect_deployment` inspects an already running Engine without ingestion or
model quality calls. It never reports full production approval from readiness
alone. Actual production validation requires the deployment and its authorized
workload/access instructions.

## Evidence

The follow-up implementation/test evidence is recorded in
[`provider_verification/README.md`](../provider_verification/README.md).
No source settings, secrets or memory content belong in diagnostic reports.
