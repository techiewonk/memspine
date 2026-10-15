# MemSpine pipelines

The three pipelines of the memory loop, each specified step by step with typed inputs and outputs, triggers, failure
behaviour, config, scenarios and the owner's decisions. These documents are maintained: every change to a pipeline's
behaviour updates its spec in the same PR.

| Pipeline | Operators | Spec | Steps | Scenarios |
|---|---|---|---|---|
| Insert (data in) | D | [insert.md](insert.md) | 23 | 54 |
| Offline (time, event, context pressure) | K_C, K_P | [offline.md](offline.md) | 22 | 47 |
| Retrieve (read and compose) | R, C | [retrieve.md](retrieve.md) | 20 | 40 |

## Runtime and structure shared by all three (owner, 2026-10-13)

| Id | Decision | Reading |
|---|---|---|
| RT-1 | Outbox = truth, taskiq = transport (owner) | Every job is written to the SQL outbox in the same transaction as its event; taskiq carries job ids to workers (InMemoryBroker in library mode; Redis/NATS/RabbitMQ broker and separate worker processes in server mode); a worker marks the row done; unfinished rows are re-sent on start. |
| RT-2 | Async core + dedicated executors | All orchestration async (TaskGroup, timeouts, cancellation); GPU encoders (embedder, Jina) each in one dedicated thread executor with micro-batching; blocking DB/index I/O in a bounded thread pool; model calls async over HTTP behind a global semaphore; a process pool only for CPU-heavy parsing. |
| RT-3 | Four priority classes, reads first | 1 interactive read, 2 write hot path, 3 online extraction (calls 1 and 2), 4 sleep and maintenance; higher classes go first, lower classes keep a guaranteed minimum share. |
| RT-4 | Backpressure: coalesce + budget, never drop | Windows coalesce into larger batches, the oldest backlog is marked underived (raw stays durable; readiness reported per index), derivation backlog does not block writes; backlog-age metric and alert. Accepted work is never dropped; at storage or quota limits new writes are rejected before acceptance (ER-08). |
| ER-07 | Claim epoch + conditional commit (owner) | Each lease takeover bumps a monotonic epoch; a deposit commits only if epoch, source versions and erasure generation still match; a stale worker cannot write, acknowledge, advance watermarks or release the new lease. See `offline.md` step 2, `insert.md` step 22. |
| ER-08 | Quotas + explicit rejection (owner) | Accepted work is never dropped; at storage or quota limits new writes are rejected before acceptance with a distinguishable error; emergency space is reserved for erasure and recovery. See `insert.md` step 8. |
| ER-09 | Contiguous watermark + holes (owner) | Progress is the highest complete sequence plus explicit holes; projector progress, derivation coverage and source membership are tracked separately; a wait captures a fixed target. |
| ER-10 | Tracked jobs + bounded drain (owner) | Every job is tracked; `close()` drains within a deadline and persists unfinished jobs; a cancelled job cannot commit; per-tenant fair shares. |
| ER-11 | Context lines as binding evidence (owner) | NEW lines support the assertion; CONTEXT lines that resolve references are recorded as reference-binding evidence; context never counts as a new occurrence. See `insert.md` step 13. |
| ER-12 | Coverage check + targeted re-extraction, screened (owner) | Raw text kept; a cheap check flags likely uncovered names, quantities and list members; targeted re-extraction only when flagged; extra calls counted. |
| ER-13 | Stable memory scope; session as provenance (owner) | Entities and assertions keyed by stable scope; retry, mention, assertion and occurrence identities are separate. |
| ER-14 | Separate assessment fields (owner) | Source trust, support strength, entity-binding certainty, time precision and modality are separate fields. |
| ER-15 | One call + bounded recovery budget (owner) | One extraction call per window plus a capped recovery budget counted by purpose; partial output never counts as full coverage. |
| POL-1 | In-flight erasure: revalidate before returning (owner) | Generation and permissions rechecked just before a read returns; erased items dropped; already-delivered content cannot be recalled. See `retrieve.md` step 19. |
| POL-2 | Derived visibility: lineage-aware admission (owner) | A derived record is admitted only if the viewer may see its supporting lineage; numeric trust is not access control. See `retrieve.md` step 8. |
| MOD-1 | Typed registries + entry points | StageRegistry, LegRegistry, BackendRegistry, ProviderRegistry, PromptRegistry with Protocols; built-ins register at import, third parties via Python entry points; config names parts; the LayerRegistry decides what runs. |
| MOD-2 | dishka for dependency injection (owner) | Async-native providers with scopes app / job / request (engine lifetime, one background job, one read); finalisers close DB and model clients; tests override providers with fakes; the clock is a provider. |
| MOD-3 | import-linter contracts in CI | Layered contract: api -> (ingest, derive, read, compose) -> (store, models, trust, runtime) -> schemas/core types; store never imports read or derive; read never imports derive. |
| MOD-4 | Versioned Pydantic schema package | memspine.schemas holds every step's typed input and output with a schema version; JSON Schema exported to docs/v2/schemas/ so docs and code never drift; upcasters for old versions. |

## Layers on by default (Q1 as amended by I-7, 2026-10-13)

With no model configured: L0 raw only (no model call anywhere). With a worker model configured: L0 raw, L1 facts and
L3 graph (compiled from facts, stored, read leg routed). L2 episodes and summaries, L4 observations and profile, and L5
standing answers, procedures and plans stay opt-in per template.

## Change log
- 2026-10-13: index created; owner decisions on 23 pipeline questions and 11 runtime and modularity questions recorded.
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
