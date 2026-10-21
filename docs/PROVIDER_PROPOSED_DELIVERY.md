# Selected provider follow-ups: delivery and local review packet

> This packet retains historical delivery results and status names. Current
> status is authoritative in [PROVIDER_TODO.md](PROVIDER_TODO.md); accountable
> roles and next gates are in [PROVIDER_NEXT_MILESTONES.md](PROVIDER_NEXT_MILESTONES.md).
> PA-119/121 subsequently integrated grounded GLiNER mentions into Engine
> message/window processing. The typed record/relation adapter remains a
> separate candidate API; mention integration does not enable automatic relation assertions.

All 31 former proposals were selected by the owner. This register records the
result for every item, rather than treating a plan, partial fixture or unavailable
environment as completed execution. **25 local gates completed; 6 remain open or
need environment/destination details.** New PA-124/125 cover the additional fast
library/kernel request. The [delivery standard](PROVIDER_DELIVERY_STANDARD.md)
explains correctness, quality, performance and publication boundaries.

| Item | Status | Work | Evidence | Remaining boundary |
|---|---|---|---|---|
| PA-047 | DONE | Profile the final configuration-admission code | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-048 | DONE | Evaluate SQLite migration/import startup cost | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-049 | DONE | Evaluate LanceDB first-connect/import cost | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-050 | DONE | Measure first serialization separately from startup | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-051 | NEEDS_ENV | Repeat timing on a controlled representative host | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Dedicated representative host and accepted startup/workload budget are not supplied. |
| PA-052 | DONE | Measure vector and lexical scaling with larger synthetic corpora | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-053 | OPEN | Measure concurrent ingest/search and queue backpressure | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | Concurrent raw ingest/search measured; derivation queue lag and an agreed budget remain unverified. |
| PA-054 | DONE | Measure remote-provider tail latency and timeout behavior | [Performance profile](PROVIDER_PERFORMANCE_FOLLOWUP.md) | None for the stated local gate; documented scope limits remain. |
| PA-055 | DONE | Profile long-running memory and connection growth | [30-cycle resource soak](provider_verification/provider-resource-soak.json) | None for the stated local gate; documented scope limits remain. |
| PA-075 | DONE | Check semantic facts across selected backend combinations | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-076 | DONE | Check working-memory overflow and persona behavior | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-077 | DONE | Check episodic chronology, historical ingestion and sessions | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-078 | DONE | Check resource/document chunk lineage and retrieval | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-079 | DONE | Check procedural promotion and plan-cache validity | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-080 | DONE | Check reflective memory provenance and depth limits | [Memory-type acceptance](provider_verification/memory-types-acceptance.json) | None for the stated local gate; documented scope limits remain. |
| PA-081 | DONE | Check associative graph direction, time and entity ownership | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | None for the stated local gate; documented scope limits remain. |
| PA-082 | NEEDS_ENV | Check prospective watches and worker execution | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Pull/ack idempotency passes; actual consumer/worker execution protocol and handover environment are not supplied. |
| PA-083 | DONE | Check shared-memory grants and revocation | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | None for the stated local gate; documented scope limits remain. |
| PA-085 | DONE | Define cross-provider retrieval relevance acceptance cases | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | None for the stated local gate; documented scope limits remain. |
| PA-086 | DONE | Review multilingual and literal-query behavior | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | None for the stated local gate; documented scope limits remain. |
| PA-087 | DONE | Review compression and token budgets by memory layer | [Compression acceptance](provider_verification/compression-acceptance.json); lossy model quality remains PA-088 | None for the stated local gate; documented scope limits remain. |
| PA-088 | NEEDS_ENV | Run clean model-quality evaluations when explicitly budgeted | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) | Local answer-model endpoint/name and call budget requested; no responding Ollama endpoint observed. |
| PA-089 | DONE | Evaluate native hybrid search as a separate future capability | [Native hybrid diagnostic](provider_verification/native-hybrid-evaluation.json) | None for the stated local gate; documented scope limits remain. |
| PA-093 | DONE | Check defaults/runbook/examples for drift during release | [Read-only release reference gate](provider_verification/release-reference-check.json) | None for the stated local gate; documented scope limits remain. |
| PA-094 | DONE | Prepare a focused release summary and evidence index | [Selected-work delivery and review packet](PROVIDER_PROPOSED_DELIVERY.md) | None for the stated local gate; documented scope limits remain. |
| PA-095 | OPEN | Prepare branch review and PR when requested | [Local review packet; PR pending destination](PROVIDER_PROPOSED_DELIVERY.md) | Local review/evidence packet is prepared; PR destination remains unspecified and no PR was created. |
| PA-096 | NEEDS_ENV | Publish or push only through the requested release workflow | [Release boundary](PROVIDER_PROPOSED_DELIVERY.md) | Release workflow/destination/revision authorization remains unspecified; nothing published. |
| PA-108 | DONE | Validate GLiNER2 local inference on Python 3.13 | [Real GLiNER runtime](GLINER25_LOCAL_RESULTS.md) | None for the stated local gate; documented scope limits remain. |
| PA-111 | DONE | Expand installed optional-extra/platform coverage | [Expanded optional runtime matrix](PROVIDER_PLATFORM_MATRIX.md) | None for the stated local gate; documented scope limits remain. |
| PA-120 | DONE | Add GLiNER2.5 typed record/relation extraction | [Typed source-cited adapter](GLINER25_STRUCTURED_EXTRACTION.md); [real runtime gate](provider_verification/gliner-structured-real.json) | None for the stated local gate; documented scope limits remain. |
| PA-122 | DONE | Compare GLiNER2.5 Base and Small for ingestion | [Base/Small bounded comparison](GLINER25_LOCAL_RESULTS.md) | None for the stated local gate; documented scope limits remain. |

## What changed and what it proves

- Six memory-type checks run under two local cache configurations, with real
  SQLite/LanceDB/Tantivy: provenance/history, working overflow/personas, episodic
  event-time/session links, resource chunks, procedural gates and reflection
  trust/depth/scope. Resource erasure is explicit per returned chunk; no automatic
  source-file deletion or universal backend cross-product is claimed.
- Graph checks cover SQLite adjacency and installed Ladybug namespace ownership;
  record-time filtering is separate from unsupported temporal-edge history. Shared
  revocation is checked on current public read paths. The current shared-search
  API adds foreign vector hits and rechecks live grants; it exposes no foreign
  graph or result-cache leg to claim. Pull watches are not an external worker
  exactly-once delivery protocol.
- The optional GLiNER adapter maps typed records/relations with strict score/span
  checks and immutable source binding. It is explicitly constructed with a
  caller-owned model, not enabled in Engine ingestion. Real SDK inference and
  Base/Small measurements have fixed small synthetic scopes; they are not a
  published answer-quality benchmark.
- Config/import/serialization/scaling and six remote round-trip/failure probes
  are measured. Native hybrid uses a disposable Weaviate diagnostic; internal
  candidate budgets differ from portable RRF, so no equivalence/adoption claim
  is made. The 30 measured lifecycle cycles observe zero open DBAPI connections
  after stop and stable handle counts within this finite run; that is not proof
  of unlimited leak freedom or production acceptance.
- The fast-library audit retains existing `orjson` event/storage paths. Guarded
  replacement in provider control metadata was slower and unguarded encoding
  changed edge values. Chunked NumPy SimHash voting is adopted with exact output
  parity and bounded temporary memory. See [all performance alternatives](ENGINE_PERFORMANCE_AUDIT.md).

## Verification and review

The affected regression recorded 411 passes / one explicit live-Temporal skip
before the final source-binding and SimHash changes. Final focused checks record
45 passes, source typing and Ruff. The final kernel benchmark compares the actual
production function with the frozen old reference, alternates order and retains
raw trials. Per the owner, no complete-engine performance run was added for the
kernel optimization. Reference generation remains read-only and current.

See [the verification index](provider_verification/README.md),
[baseline and current limits](PROVIDER_OPERATIONS.md),
[breaking configuration guide](PROVIDER_UPGRADE_GUIDE.md),
[release-reference check](provider_verification/release-reference-check.json), and
[final hashed wave manifest](provider_verification/proposed-wave-final-state.json).
Code remains in the working tree. No PR, push, release or deployment was performed.
The release owner still needs to choose a destination/workflow and a release
revision; unrelated evaluation migration remains deferred. Database copying and
re-embedding remain excluded.
