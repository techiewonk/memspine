# ADR-026 — Multi-call / graphiti-style write pipeline

- **Status:** accepted
- **Date:** 2026-07-10
- **Decision id:** v0.2 C2 (C3 extends)
- **Phase:** v0.2 Phase C · **Tier:** DF (opt-in)

## Context

The v0.1 write path is single-pass: content → one entity/fact extraction →
conflict ladder → semantic records. It never asks an LLM to extract the
*relationships between* entities, so the association graph only grows from
explicit user `link()` calls and reorganize's community membership. graphiti and
similar systems get much richer graphs by making a *second* class of LLM call —
edge extraction — and writing those edges as first-class graph structure.

The constraint (D-03/profiles-stay-green): this must be **opt-in** and must not
perturb `profile="simple"`, must not require an LLM in core, and must preserve
event-sourcing (D0.1) and anti-lock-in (D-17: no runner imports in pipeline
code).

## Decision

An **async `extract_graph` pipeline** (C2), slotted in the sleep cycle **after
`consolidate` and before `reorganize`** so Leiden communities form over the
fresh LLM edges. It is a plain idempotent step function in `workers/pipelines.py`
(D-17); the engine injects an `extract_edges` callable into `PipelineContext`.

- **Gating.** The callable is built only when
  `memories.semantic.policies.extract_graph` is truthy **and** an `extract_edges`
  LLM role is configured (C1). Otherwise the field is `None` and the stage
  self-skips — `profile="simple"` runs the same seven sleep stages it always did,
  one now reporting `skipped`.
- **Edges → records.** Each extracted edge `(src_entity, rel, dst_entity, fact)`
  becomes a semantic `MemoryRecord` keyed `(entity=src, attribute=rel)` with the
  fact as content — reusing the existing fact-keyed model, not a new node type.
  Written via a `WRITE` event through `ctx.append_event` (the write door).
- **Edges → graph.** An `asserted` LINK connects the source record to the new
  fact record. `asserted` is **non-reserved**, so the M13.6 link budget applies
  (unlike system `community`/`derived_from` edges): a saturated source keeps the
  fact record but skips the extra edge (logged). Communities then form over
  these edges the same cycle.
- **Reflexion.** The engine's `extract_edges` callable runs up to
  `max_rounds` extraction passes and merges unique `(src, rel, dst)` edges — a
  later pass recovers what a stochastic first pass missed. One round by default.
- **Idempotency + provenance.** An edge's `(namespace, src, rel, dst)`
  fingerprints its record's `message_id`, so a re-run skips already-written edges
  and never re-extracts from its own `channel="extract_graph"` output (no
  feedback loop). Derived trust never exceeds the source's (E1/D-47 §5); echoed
  injection framing stays flagged via `instruction_shaped`.

## Consequences

- Positive: opt-in relationship graph from unstructured content; richer
  communities; rides existing WRITE/LINK/budget/firewall machinery; rebuild-
  deterministic (every mutation is a logged event).
- Negative / cost: N extra LLM calls per source record per sleep; a background
  latency knob (`max_rounds`, `min_confidence`) deployments must tune.
- **C3 (landed):** an optional **synchronous** `WritePipeline` in the semantic
  write door (`memories.semantic.policies.write_pipeline: single|graph`).
  `graph` mode extracts edges at write time and writes each as a fact-keyed
  record **through the same `_write_locked` door**, so dedup (M5) and the
  conflict ladder (M4) apply — edge *invalidation* is thus the existing ladder
  (using the `judge` LLM when configured), not a separate mechanism. Edge
  records carry `channel="write_pipeline"`; the memory guards on it, so an edge
  record never re-triggers the pipeline (bounded depth-1 recursion). E1
  provenance inheritance (derived trust ≤ source, injection framing preserved).
  Default `single` is byte-identical to v0.1. The `resolve_entity` role (C1)
  has a hook in `GraphWritePipeline` but is not wired by default — LSH+cosine
  dedup already canonicalizes at the record level; full coreference is future
  work.

## Alternatives rejected

- **Entity nodes as a new graph node type** — rejected: the fact-keyed semantic
  record already models `(entity, attribute, value)`; a parallel node type would
  duplicate lifecycle/firewall/erasure machinery.
- **Reserved `derived_from` rel for the edges** — rejected: these are
  associations that should count against a node's budget, not budget-exempt
  provenance; the plan chose a non-reserved `asserted` rel deliberately.
- **Schema-default-on** — rejected (D-03): would make a bare install issue LLM
  calls on every sleep. Opt-in via policy + role only.
