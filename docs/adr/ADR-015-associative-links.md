# ADR-015 — Associative links as LINK events (P6)

**Status:** accepted · **Date:** 2026-07-07 · **Register:** D-49

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

### 5. Default graph store: `sqlite_adjacency` (D-26 amended)

D-26 named LadybugDB the default embedded graph behind `[graph]`, but the
pinned ladybugdb fork is **not published on PyPI** — an extra that cannot
install is worse than no extra. For v0.1: the default graph provider is
**`sqlite_adjacency`** (zero-dep, rides the existing SQLite client and the
0007 migration), the `[graph]` extra ships **empty/reserved** until the
pinned ladybugdb fork publishes, the `ladybug` provider is **reserved** (its
stub raises `MissingServiceError` naming `[graph]`), and **kuzu stays the
first-class alternative** behind `[kuzu]`. Alternative rejected: promoting
kuzu to default — that would put a heavier native dependency in the zero-extra
associative path, and `sqlite_adjacency` already serves the shallow graphs the
link budget allows.

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
| **D-49** | **Associative links & graph projection** | `EventKind.LINK` (`memory.link`, payload `{src, dst, rel, weight, reason}`) — links are new information and ride the log; graph = rebuildable GraphProjector over WRITE/LINK/FORGET + derivation payloads · link budget enforced at creation (`ConflictError`), prune = weight-0 tombstone LINK (replay-deterministic; provenance/reorganize links budget-exempt) · `sqlite_adjacency` default graph (ladybugdb unpublished; kuzu `[kuzu]` first-class alt) · PPR pure-Python bounded · reorganizer writes consolidation-shaped community parents (min-member trust, D-47 §5), no-op without `[community]`. (ADR-015) |

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
