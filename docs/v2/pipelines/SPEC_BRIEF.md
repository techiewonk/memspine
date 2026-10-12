# Pipeline spec brief (for the three drafting agents)

Goal (owner, 2026-10-12): a permanent, maintained reference for MemSpine's three pipelines, so anyone can understand
every step, its data, and every scenario. Pipelines: **insert** (data in, operator D), **offline** (processing
triggered by time, events or other logic: K_C consolidate, K_P personalise), **retrieve** (R retrieve and C compose).
Describe the DECIDED design (`docs/v2/ARCHITECTURE.md`, `docs/v2/areas/*.md`, `docs/v2/VERIFY.md` section 0 for the
owner's answers Q1-Q9, `docs/v2/PLAN.md`), and for every step say what today's code does (file:line in
`src/memspine/`) and what changes. No code changes; write only your file.

Principles that must hold everywhere: the event log is the truth; layers L0 raw, L1 facts (default on), L2 episodes and
summaries, L3 graph, L4 observations and profile, L5 standing answers, procedures and plans (L2-L5 opt-in, Q1); no
model call by default; derived records carry lineage, trust capped at their weakest parent, and a derivation
fingerprint; durable outbox drained by an inline runner (Q2); Neo4j for the server graph (Q4); Jina rerank on every read
where installed (Q5/Q6); undeclared attributes coexist unless extraction marks a change (Q7).

## Required structure (Markdown, British spelling, plain language; invented names only, never benchmark text)

1. `# <Pipeline> pipeline` and a one-paragraph purpose; a Mermaid flowchart of the steps.
2. `## Entry points and triggers`: every way the pipeline starts (API calls, events, timers, thresholds, manual,
   recovery on start), with the exact condition.
3. `## Steps`: one `###` per step, numbered in order, each with:
   - **Purpose**; **Runs when** (always / config flag / layer present); **Model call?** (never / optional role);
   - **Input** and **Output** as typed schemas in a fenced block (field: type, required or optional, meaning), using
     the shared types in section 5 by name;
   - **Writes** (which event kinds, tables, projections, layer) and **Reads**;
   - **Failure and retry** behaviour; **Idempotency**; **Ordering and locking**;
   - **Config** keys (current name -> decided name if it changes) and defaults;
   - **Today vs decided** (file:line now; the decision ids that change it; work package id from PLAN.md).
4. `## Scenarios`: a table of at least 25 scenarios this pipeline must handle, each with expected behaviour, the step
   that handles it, and how it is tested (existing test file or "to add"). Cover normal, edge, failure, concurrency,
   security and scale cases (for insert, e.g. retries with the same id, edited document, out-of-order timestamps,
   unknown time zone, multi-party chat, assistant echo, injection attempt, forget during a pending window, crash after
   inference, no model configured, budget spent; for offline, e.g. trigger storms, overlapping manual and scheduled
   runs, partial stage failure, restart mid-stage, very large namespace, contradictory facts, homonym entities, erasure
   during a run; for retrieval, e.g. empty store, no answer present, as-of query, multi-hop, temporal relative phrase,
   lists and counts, multi-party viewer, low-trust evidence, a layer not built, reranker absent, budget overflow).
5. `## Shared data types used here`: the schemas this pipeline relies on (for example SourceEnvelope, Receipt,
   MemoryEvent, Record and AssertionEnvelope, ItemV1, OutboxRow, LineageEdge, StageReport, ReadRequest, QueryContract,
   LegHit, ContextBlock, ReadResult), field by field, marked with the decision that defines each field. Mark fields that
   exist today versus new.
6. `## Open questions for the owner`: each with 2-4 options, the recommended option first and why. Only real choices
   the decisions did not settle.
7. `## Change log`: one dated line.

Length: as long as needed to be complete, but no repetition; prefer tables and schema blocks to prose.
