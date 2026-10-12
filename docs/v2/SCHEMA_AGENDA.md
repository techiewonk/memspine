# Schema discussion agenda (after the semantic contracts)

Handoff request 5: settle meaning before fields. The owner has now decided every semantic contract (ER-01..ER-40,
POL-1, POL-2). This agenda orders the schema sessions so no illustrative field is frozen before its meaning is fixed.
Each session ends with a reviewed Pydantic model in `memspine.schemas` (MOD-01) and its JSON Schema export.

| # | Session | Settled semantics it must encode | Objects | Questions to settle in the session | Package |
|---|---|---|---|---|---|
| 1 | Versioning policy (**answered 2026-10-13**, `schemas/SESSIONS_01_02_2026-10-13.md`) | ER-33: durable event and public API versions are strict; internal step types are typed but narrower | all | Which objects are persisted or public (strict versions, upcasters) and which are internal (typed, no promise) | MOD-01 |
| 2 | Trust boundary (**answered 2026-10-13**, same file) | ER-32: the model never authors scope, ids, trust, verified state, fencing tokens or generations | ItemV1, AssertionEnvelope | Exact split of model-authored vs system-authored fields; the compiler's validation and rejection rules | MOD-01, W2-06a |
| 3 | Identity (**answered 2026-10-13**, `schemas/SESSIONS_03_04_2026-10-13.md`) | ER-13: stable memory scope; session as provenance; separate retry, mention, assertion and occurrence identities | Scope, AssertionKey, MentionId, OccurrenceId | Which scope levels key entities (tenant/user/agent policy); how occurrence ids are formed from cited turns | F-01, F-16c |
| 4 | Evidence and lineage (**answered 2026-10-13**, same file) | ER-11 binding evidence role; POL-2 lineage-aware admission; ER-17 source families | Evidence, LineageEdge | Roles (support, binding, context), source-family id, how erasure invalidates dependants | F-16a, W2-08 |
| 5 | Assessment (**answered 2026-10-13**, `schemas/SESSIONS_05_06_2026-10-13.md`) | ER-14 separate fields | Assessment | Field list (source trust, support strength, binding certainty, time precision, modality); allowed combinations for supersede, graph and tentative evidence | F-02a, W2-10 |
| 6 | Time (**answered 2026-10-13**, same file) | I-2 and ER-18: anchors with basis and precision; intervals; relations without dates; no fabricated exact dates | TemporalRef, Interval, TemporalRelation | Precision enum; anchor basis enum; how relative phrases and durations are stored; event time vs source time vs recorded time | F-02b, W2-14 |
| 7 | Receipt and readiness | ER-06: durable acceptance; per-capability readiness; bounded waits | Receipt, Readiness, WaitTarget | Capability names; consistency target; timeout and partial-read behaviour; ephemeral-mode weaker guarantee | W2-01, F-04 |
| 8 | Jobs and progress | ER-07 epochs; ER-09 contiguous watermark + holes; ER-10 tracked jobs; ER-26 immutable job snapshots; OF-1 handles | OutboxRow, JobLease, Progress, JobHandle | Epoch semantics; hole representation; snapshot target; handle states | F-04, RT-01, O4-01a |
| 9 | Overload and accounting | ER-08 quotas and rejection; ER-15 recovery budget; ER-39 cost classes | QuotaError, AttemptBudget, CostRecord | Error shape distinguishing rejected from accepted; budget units; cost classes (generative, embedding, rerank, tool) | RT-02, W2-12 |
| 10 | Read and cache | ER-24 bundles and exact budget; ER-31 cache keys; ER-29 viewer; POL-1 revalidation | ReadRequest, EvidenceBundle, ContextBlock, CacheKey | Bundle completeness rules; exact vs estimated accounting flag; cache key fields; viewer binding from the principal | R3-01, R3-09a, R3-15 |
| 11 | Erasure and retention | ER-30 manifest and generations; ER-27 retained-history boundary | ErasureManifest, Generation, RetentionBoundary | Logical invisibility vs physical purge vs backups; what remains for rebuild; capability advertisement | T5-04, F-16b |
| 12 | Plug-in manifest | ER-34: discovery is not enablement | CapabilityManifest | Pinning format; duplicate and lifetime-violation errors | MOD-02 |

Order: sessions 1 and 2 first (they constrain every object), then 3-6 (record content), then 7-9 (runtime), then
10-12 (read, lifecycle, plug-ins). Proposed improvements stay separate from measured results; no dataset-specific
examples in any schema.

## Change log
- 2026-10-13: sessions 1 and 2 answered (three tiers, int versions + upcasters, extra fields allowed, content-only cited model output, open kind set with safe defaults).
- 2026-10-13: sessions 3 and 4 answered (owner-scoped entities, occurrence and retry ids, evidence roles, source family = asserting principal, hide-then-re-derive).
- 2026-10-13: sessions 5 and 6 answered (five assessment fields, supersede rule, tentative edges, granular precision, three time axes, typed relations).
