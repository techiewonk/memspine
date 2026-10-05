# ADR-015 — Associative links as LINK events (P6)

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-49
**Amended:** 2026-07-10 (v0.2 E1) — `related()` gains a configurable traversal
**strategy** (`memories.associative.policies.related.strategy`): `ppr` (default,
byte-identical to v0.1), `bfs` (breadth-first neighbors within `depth` hops,
wiring the shared `walk_neighbors` primitive), and `rrf` (reciprocal-rank fusion
of the PPR graph rank with a vector-similarity rank of the seed — surfaces
records that are both graph-close and semantically similar; degrades to `ppr`
when no vector store/embedder is bound). A `strategy=` argument on `related()`
overrides the policy. The E1 gate (ACTIVATED · not quarantined · same namespace)
and RETRIEVE-event reinforcement are unchanged across all three.

## Context

P6 ships associative memory (M13.6/D-40): explicit links between records,
PPR recall, bounded A-MEM evolution, and the D-42 background reorganizer. A
new link is new information, and the golden rule (D0.1) says the graph store
is a rebuildable projection — so links must ride the log, not be written into
the graph directly. This ADR records the event shape, the budget mechanics,
and the boundaries that keep replay deterministic.

## Decisions

### 1. `EventKind.LINK = "memory.link"` carries every association

Payload: `{"src": record_id, "dst": record_id, "rel": str, "weight": float,
"reason": str}`. The associative `GraphProjector` (registered only when
associative memory is enabled) projects WRITE → node, LINK → edge, FORGET →
`delete_node` cascade (M7), and derivation provenance
(`consolidation`/`reflection` member ids on WRITE payloads) → `derived_from`
edges. `rel`/`reason` are short slugs stored as edge properties: they never
enter a context window, so the E1 content gate does not apply to them —
endpoint *records*, however, are firewall-gated at their own write, and a
quarantined endpoint refuses new links outright (held content gains no graph
reach until corroborated).

### 2. Budget enforced at creation; pruning is a weight-0 tombstone

The M13.6 budget (`LINK_BUDGET`, bounded A-MEM) is enforced in the memory
layer at link-creation time — never in the projector, which must reproduce
exactly what the log says or rebuilds become order-sensitive. Over-budget
links are refused with `ConflictError`; `prune_weakest` frees a slot by
emitting a compensating LINK event with `weight: 0.0` (the `GraphStore` port
has no single-edge delete, and a tombstone replays deterministically —
weakest-edge selection tie-breaks on `(weight, src, dst, rel_type)`). Every
reader (`live_links`, PPR, communities, `neighbors()` walks) treats
`weight <= 0` as absent. Provenance edges (`rel: "derived_from"`) and
system-written community-membership links (`rel: "community"`) are
budget-exempt and never prunable (`prune_weakest` cannot select them): they
record facts, not associations, and a 20-member community must keep all 20
membership links. Because exemption would otherwise be forgeable into
unbounded fan-out, those rels are **reserved**: `AssociativeMemory.link()`
(and therefore `Engine.associate()`) refuses caller-supplied `rel` values in
the reserved set with `ConflictError` — only system writers (the projector's
derivation edges, the reorganizer's membership links) emit them. When the
reorganizer supersedes a stale community parent (membership drift), it also
emits weight-0 tombstone LINK events (`reason: "reorganize_supersede"`) for
each live member→old-parent `community` edge alongside the archive
transition, so the archived parent loses its graph reach
replay-deterministically instead of accumulating garbage edges. Edges are
undirected for dedup too: re-linking `(dst, src)` re-weights the stored
`(src, dst)` edge rather than materializing a mirror.

### 3. Links never cross namespaces; recall mirrors the search gate

Both endpoints must exist in the caller's namespace; missing, foreign, and
deleted records share one error (ADR-014 shape — no cross-namespace existence
oracle). `related()` ranks with pure-Python personalized PageRank
(`PPR_DAMPING`/`PPR_ITERATIONS`, D-40; undirected, weighted, deterministic)
and returns only ACTIVATED, never-quarantined records in the namespace —
exactly the E1 gate `engine.search` applies.

### 4. Evolution and reorganization are deterministic log writers

Bounded A-MEM evolution (D-42) proposes links after a non-quarantined
semantic/episodic write: vector similarity above
`EVOLUTION_LINK_MIN_SIMILARITY`, at most `EVOLUTION_MAX_LINKS_PER_WRITE`, no
LLM in v0.1 (LLM proposal is a later opt-in). Best-effort and loud: failures
log a warning, never fail the write. The D-42 reorganizer (sleep-cycle stage
after consolidate; `[community]`-gated, no-op logged once when absent) writes
one consolidation-style summary parent per Leiden community of ≥ 3 members —
consolidation-shaped WRITE provenance (so `audit taint` and `derived_from`
projection ride existing machinery), min-member-trust inheritance (D-47 §5),
membership-fingerprint idempotency, stale parents archived — plus
member→parent LINK events (`rel: "community"`).

### 5. Default graph store: `sqlite_adjacency` (D-26 amended; ladybug now real, 2026-07-09)

D-26 named LadybugDB the default embedded graph behind `[graph]`, but at the
time the pinned ladybugdb fork was **not published on PyPI** — an extra that
cannot install is worse than no extra. For v0.1: the default graph provider
was set to **`sqlite_adjacency`** (zero-dep, rides the existing SQLite client
and the 0007 migration), the `[graph]` extra shipped **empty/reserved**, the
`ladybug` provider was **reserved** (its stub raised `MissingServiceError`
naming `[graph]`), and **kuzu stayed the first-class alternative** behind
`[kuzu]`. Alternative rejected: promoting kuzu to default — that would put a
heavier native dependency in the zero-extra associative path, and
`sqlite_adjacency` already serves the shallow graphs the link budget allows.

**Update (2026-07-09):** the fork — published as `ladybug` on PyPI
(first release v0.18.0, 2026-07-01; MIT-licensed, actively maintained) — is
real and installable. `[graph]` declared `ladybug>=0.18` (now pinned
`>=0.21,<0.22`, ADR-034) and
`services/graph/ladybug.py` is a genuine adapter (verified against the
installed package: the fork kept Kuzu's embedded-Cypher Python API and DDL
dialect — including `CREATE NODE/REL TABLE IF NOT EXISTS` — unchanged, so the
adapter mirrors `kuzu.py` line-for-line). The config default deliberately
**stays `sqlite_adjacency`** — flipping it to `ladybug` would make a fresh
`profile="simple"` install hard-fail without `[graph]` installed, breaking
"profiles stay green." Promoting `ladybug` to the config default is left as
an explicit follow-up decision for a future ADR, not made here.

**Update (2026-10-05, ADR-034):** release history corrected — Kùzu's upstream
repository was archived on 2025-10-10 (the earlier text gave no date and
described it only as "closed"); LadybugDB is its maintained fork (0.18.0 on 2026-07-01 through 0.21.2 on
2026-10-01). ADR-034 makes LadybugDB the graph engine for graph features,
keeps `sqlite_adjacency` as the zero-dep default and fallback, and turns
`kuzu` into a deprecated alias for one release. The graph tables gain
`namespace`/`weight`/`kind` (KB-1), so PPR, BFS and Leiden stay inside one
namespace.

## Consequences

- The M11 vocabulary gains `memory.link` (`EVENT_LINK` in
  `observability/logging.py`, added alongside the other kind-derived names).
- `PipelineContext` gains an optional `graph` handle; it is populated only
  when the associative projector is registered — reorganizing a graph no
  projector maintains would summarize stale state.
- Known limit: a `DECAY_TRANSITION` that changes `memory_type` (working →
  episodic page-out) does not relabel the node; labels are advisory, never a
  retrieval gate.
- E4 seams land with P6 (plan §E4): `EmbedderManifest` gains
  `matryoshka_dims`/`quantization` (both undeclared in core embedders) and
  `VectorStore.search_rescore()` falls back to plain search in both shipped
  adapters until a quantized adapter exists.

## Register row (D-49)

| # | Decision | Ruling |
|---|---|---|
| **D-49** | **Associative links & graph projection** | `EventKind.LINK` (`memory.link`, payload `{src, dst, rel, weight, reason}`) — links are new information and ride the log; graph = rebuildable GraphProjector over WRITE/LINK/FORGET + derivation payloads · link budget enforced at creation (`ConflictError`), prune = weight-0 tombstone LINK (replay-deterministic; provenance/reorganize links budget-exempt) · `sqlite_adjacency` default graph (ladybug published 2026-07-01, real `[graph]` adapter since 2026-07-09, config default unchanged pending a follow-up ADR; kuzu `[kuzu]` first-class alt) · PPR pure-Python bounded · reorganizer writes consolidation-shaped community parents (min-member trust, D-47 §5), no-op without `[community]`. (ADR-015) |

## Alternatives rejected

- Writing edges into the graph store directly — breaks D0.1 (a projection
  becomes a second source of truth; rebuild loses links).
- Enforcing the budget in the projector — replay would drop different edges
  depending on delivery order; rebuild parity dies.
- Evicting over-budget links automatically on `link()` — silent data loss on
  the write path; an explicit refusal plus `prune_weakest` keeps the caller
  in charge and the log explainable.
- A `delete_edge` port method for pruning — widens every adapter for one
  internal need the tombstone already serves deterministically.

## Amendment (2026-10-05): entity layer, graph read leg, trust caps on graph paths

Opt-in throughout; with the flags off the engine is byte-identical
(`tests/unit/test_graph_leg_off_golden.py` compares `read()` contexts against a
snapshot recorded before the change). No decision above is reversed: entity
edges are a projection of WRITE payloads, so D0.1 and rebuild parity hold.

- **Entity nodes (GP-2, `memories.associative.policies.entity_nodes`, default
  off).** The `GraphProjector` adds an `ent:<namespace>:<canonical>` node per
  entity a record names (its `entity` field and the `dst:` tag edge facts carry;
  canonical = NFKC, casefolded, whitespace collapsed) and a `mentions` edge
  record -> entity. The namespace is part of the node id because node ids are
  global keys in every adapter: two tenants naming one person get two nodes, and
  one tenant's forget never deletes the other's node. Junk names (pronouns, day
  words, "luck"; one character; digits) and, when an `allowed` list is set, any
  other name are rejected; a self-edge adds no second mention. A re-projected
  record whose names changed tombstones its stale mentions (weight 0, §2); a
  FORGET deletes the record's mentions with its node, and an entity left with no
  live mention is deleted. `mentions` joins the **reserved** rels: budget-exempt,
  never prunable, refused from callers. `related()` (PPR and BFS) and the
  reorganizer's Leiden input drop `mentions` edges, so they see the association
  graph exactly as before.
- **Trust caps on graph paths (GP-10).** A `mentions` edge weighs the record's
  trust; an extract_graph `asserted` LINK weighs min(confidence, source trust,
  fact trust). A graph walk never enters a record that is quarantined, erased,
  taint-rolled-back, below `read.graph_min_trust` (default the quarantine
  threshold, 0.25) or, under integrity, below admission; a refused node is a
  dead end. Graph-sourced hits then pass the ordinary search gates.
- **Graph read leg (GP-3, `read.graph_leg`, `graph_depth` ≤ 3, `graph_leg_k`).**
  Seeds are the entities the query names (n-gram match on entity names; the
  decision provider's optional `entities` hook, GLiNER2) or, failing that, the
  entities of the best three hits of the other legs. The walk
  (`AssociativeMemory.seed_expand`, depth in entity hops) yields facts and their
  source turns, fused as one more RRF leg in `engine._search`.
- **Facts block (GP-5, `read.cards_include_edges`)** renders the reached edge
  facts with validity ranges and source counts in the cards allowance.
- **Edge provenance (GR-9).** A verbatim duplicate edge in `extract_graph`
  (same (src, rel, dst) key and kind) adds an `edge_source:<episode>` tag
  through an add-only lifecycle delta instead of a new fact; no model call.
  Background `extract_graph` facts now go through the semantic write door
  (firewall, dedup, conflict ladder), so a `state` edge supersedes there too.
