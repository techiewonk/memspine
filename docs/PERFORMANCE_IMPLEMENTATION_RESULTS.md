# Performance implementation results — 2026-10-18

> Later architecture update: [named model profiles](MULTI_MODEL_PROFILES.md)
> supersede this pass's single `models.gliner` configuration. The benchmark and
> verification files below retain their original historical source hashes;
> current profile routing has its own focused evidence and final-state record.

This pass implements the owner's selected performance work with parallel agents.
Measurements are kernels or standalone provider operations, never a complete
Engine performance test. The host is shared and timings are diagnostic.

| Work | Shipped behavior | Evidence and limits |
|---|---|---|
| Bounded top-k | RRF optionally selects an exact prefix after complete score accumulation; heap only for at least 1,024 IDs and a prefix under one eighth of the pool | [Ranking report](BOUNDED_TOPK_IMPLEMENTATION.md). At 2,585 unique IDs, three-item prefix: 2.144 ms full sort versus 1.844 ms production. Small pools keep full sorting; validation/branch overhead can still cost microseconds. |
| Safe retrieval integration | Two graph expansion seed callers pass their existing three-hit limit | Main router and associative ranking remain full because downstream logic needs the tail. No recall or answer-quality improvement is claimed. |
| Shared GLiNER ownership | One lazy model pool per Engine, injected into decision, semantic NER and write prepass | [Ownership report](GLINER_SHARED_RUNTIME.md). Three fake consumers load once rather than three times. This proves ownership behavior, not a measured RAM or inference-latency reduction. |
| Canonical GLiNER settings | `models.gliner`: model, revision, device, offline policy, task capabilities, inference concurrency | CPU, local-files-only, entities/classification, concurrency one. `decision.model` is removed without an alias. Independent Engines retain independent ownership. |
| GLiNER lifecycle | Bounded off-loop inference; shared load coalescing; cancelled callers do not dispose running models; close drains and clears references | Focused fake-resource tests plus one actual cached Base checkpoint load/release. No new automatic assertion writing or real Engine extraction-quality gate. |
| Batched MinHash | Below eight unique tokens keep scalar updates; otherwise use bounded datasketch batches | [MinHash report](MINHASH_BATCH_KERNEL.md). At 128 permutations/1,024 unique tokens: 5.733 ms to 0.792 ms; exact signature bytes, restored values and LSH fixture candidates preserved. |
| SQLite startup | Retain current SQLAlchemy/aiosqlite implementation | [Five-process kernel profile](SQLITE_STARTUP_KERNEL.md): median imports 197.72/12.28/77.54 ms (SQLAlchemy/aiosqlite/client), standalone client connect 22.10 ms. Moving imports defers required work; this does not measure Engine startup or Alembic migrations. |
| Real embedding batching | Retain provider defaults after bounded comparison | [Real ONNX kernel](EMBEDDING_BATCH_KERNEL.md): 64 texts took median 0.378 s at batch 1 versus 0.232 s at batch 32; identical vectors on these inputs. Normal offline snapshot resolution failed; local cached files were copied to a temporary model directory and individually hashed. This is not a clean standard-cache acceptance test or proof of artifact revision integrity. |

The GLiNER loader resolves the requested snapshot before SDK dispatch and checks
local config, encoder config, tokenizer assets and weights. Downloads require
`local_files_only: false`. A missing local revision fails. A caller-supplied local
directory is an explicit artifact source; checking its directory name against a
revision is not cryptographic attestation of its contents.

The prepass result cache now distinguishes synchronous rules from asynchronous
model work and includes namespace and producer settings. Existing learned-alias
state is a separate concern: this change does not establish complete namespace
isolation for every prepass learning/persistence path. Structured record/relation
grounding and real Engine ingestion acceptance remain PA-119/121.

> **Note (2026-10-22, RM-69):** since RM-18 (commit 6fba3c69) learned aliases are
> namespace-scoped (hints, KNOWN prompt context, unseen-entity check, flush, reload;
> `learned.alias.reset` on namespace erasure). The measurements above are unchanged
> and were taken before that fix.

See [ADR-074](adr/ADR-074-bounded-ranking-and-shared-gliner.md) and
[the todo register](PROVIDER_TODO.md) for decisions, verification status and
remaining deployment/quality work. No global serializer, database backend,
dedup threshold or default model was replaced.

## Final focused verification

The selected kernel, configuration, graph, adapter, prepass, known-source,
ingestion-window and assembly checks pass: **192 passed, 1 skipped**. The skip
is the explicitly excluded real-model ingestion test; it directs callers to a
separate offline smoke tool. The actual cached-model check here is load/release
only. Seventeen existing LanceDB cleanup warnings remain visible in the log.
No full suite or complete-Engine performance test was run.

Ruff check and format pass for 26 selected Python files; strict mypy passes
for 16 source files. Provider defaults, stage parameters and configuration
snapshots match the current schema. See the [focused log](provider_verification/performance-final-focused.log),
[reference check](provider_verification/performance-reference-check.json) and
[hashed final state](provider_verification/performance-final-state.json).
