# ADR-080: Typed graph edge qualifiers, filtered graph reads and a sensitivity layer

**Status:** Accepted; implemented 2026-10-22 (RM-41, RM-82..RM-102). Open follow-ups are
listed under Consequences.
**Date:** 2026-10-22
**Deciders:** Owner (RM-41 recheck decisions, 2026-10-22)
**Relates to:** ADR-077 (background grounded ingestion, BI-08 graph projection), ADR-078
(context enrichment, D-100 aware-only times), ADR-068 (no compatibility aliases),
ADR-079 (shared resources; the GLiNER bindings reuse its model profiles).

## Context

Graph edges carried untyped properties. Searches could not ask for "asserted facts that
held in May and are not about health", and the read path walked every graph link the same
way. The 2026-10-22 vocabulary survey (BI01_CONTRACT_REWRITE_PLAN.md, "Edge and claim
qualifiers for filtered traversal") listed the context a claim edge needs: time, known
time, modality, lifecycle, claim state, provenance, trust, scope, role, pack, domain,
sensitivity and support.

## Decision

1. **Typed qualifiers on compiled claim edges.** `EdgeQualifiers`
   (`services/graph/qualifiers.py`) is stored in the edge properties under `qualifiers`.
   It holds valid and known time (timezone-aware only), precision, modality and polarity,
   lifecycle status and outcome, and claim state. It also holds provenance (source,
   `SourceOrigin`, lineage root), trust and its tier, scope and grants, erasure state,
   event role and occurrence, vocabulary pack, topical domain and its source, sensitivity
   labels and check state, binding status, claim and evidence ids, and independent
   support. `edge_compiler_v2` fills them. Nothing is inferred: a value the compiler does
   not know stays unknown. Lifecycle, sensitivity and origin values are the
   `memspine.semantics` vocabularies; there is no second definition.
2. **Unknown never passes an allow-filter.** Erased and erasure-pending edges never
   pass any filter, including the empty one.
3. **Exclusions.** `exclude_*` fields drop known matching values, and an unknown value
   passes a non-sensitivity exclusion. Excluding a domain excludes its descendants.
4. **Sensitivity layer with three states.** A separate deterministic labeller
   (`derive/sensitivity.py`) runs these detectors in order: vocabulary defaults, rule and
   pattern detectors, then opt-in GLiNER (offline only). Each edge records its labels
   and a check state: complete, incomplete, or not checked. A complete check with no
   labels is *checked_none*. For edges not fully checked, the caller decides with
   `unchecked_sensitivity` (pass or fail, default fail). Labels may only tighten
   policy (RM-52).
5. **Filtered reads.**
   - **Where it is accepted:** `Engine.search` / `read`, the `Memory` handle,
     `POST /search` and the MCP `memory_search` tool take `graph_filter`. MCP also has
     presets.
   - **What it filters:** the compiled-edge graph leg admits only the edges that pass.
     Graph paths over links without claim qualifiers are skipped for that read, so no
     graph result bypasses the filter. Those paths are the association walk, entity and
     node legs, causal walk, proximity rerank and communities.
   - **Caller visibility, two layers:** the edge's scope must be the caller's or carry an
     active grant, and its source record must pass the viewer check.
   - **Cache:** the read cache key includes the filter.
6. **Write validation.** Every graph provider validates qualifiers on write. An invalid
   set is dropped with a `graph.qualifiers_dropped` warning, and the write does not fail.
7. **Evidence erasure.** The erased id is removed from the edge's evidence, and the edge
   becomes contested. An edge left with no evidence is tombstoned.
8. **Time precision.** A bound covers its whole precision period. An approximate bound
   makes boundary comparisons unknown.
9. **Domains.** Domains are hierarchical ids. Each edge takes the first source that
   gives one: a caller tag, then the vocabulary key or predicate default, then the
   pack's top domain, then opt-in GLiNER topic tags. The edge records which source set
   it.
10. **Providers.** SQLite adjacency, LadybugDB and Neo4j all run one shared filtered walk
    and pass one contract.
    *2026-10-23 note, one port (owner decision):* because every graph provider supports
    qualifier filters, there is a single graph port, `GraphStore`
    (`services/graph/base.py`). `neighbors(..., qualifier_filter=None)` and
    `subgraph(..., qualifier_filter=None)` take the filter directly, and
    `frontier_edges(nodes, *, namespace, rel_type)` is a port method. `None` is the
    unfiltered walk, unchanged. Any `QualifierFilter`, including an empty one, runs the
    shared qualified walk, which still never traverses erased or erasure-pending edges.
    The separate optional protocol (`QualifiedGraphStore`, `neighbors_qualified` /
    `subgraph_qualified`, `supports_qualifier_filters`, `require_qualifier_support`,
    `UnsupportedQualifierFilterError`) and the `qualifier_filter` descriptor capability
    were removed with no aliases (ADR-068).
11. **Backfill.** Old edges get qualifiers on the next sleep cycle, or through
    `memspine graph requalify`.
12. **New model binding roles.** `gliner_sensitivity` and `gliner_topic` join
    `models.bindings`. Both are off unless bound, and offline only.

## Consequences

- No graph result reaches a filtered read without passing the filter. The cost is that
  unqualified graph paths contribute nothing to that read.
- Measured scale (RM-96): at 100k edges the read-path graph leg's p95 is 1.7–4.4 s,
  against a 50 ms budget. The leg loads every compiled edge of the namespace and checks
  qualifiers in Python. The fix (RM-104) is to walk out from the seed nodes and push the
  common predicates down to the store.
- **Follow-ups:**
  - RM-52: sensitivity enforcement in the firewall and retention.
  - RM-55: writing the grantee merge edges.
  - BI-08: lifecycle from the envelope instead of record tags.
  - BI-02: origin as a record field.
  - Wiring of the GLiNER topic tagger.
  - Top domains for the built-in packs.
