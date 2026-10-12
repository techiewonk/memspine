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
| RT-4 | Backpressure: coalesce + budget, never drop | Windows coalesce into larger batches, the oldest backlog is marked underived (raw stays searchable), writes never block; backlog-age metric and alert. |
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
