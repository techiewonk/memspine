# ADR-028 — Community detection: `leidenalg` replaces `graspologic` (D-40)

- **Status:** accepted
- **Date:** 2026-07-11
- **Decision id:** D-55 (amends D-40)
- **Phase:** v0.2 (post-hardening) · **Tier:** RG (optional `[community]` extra)

## Context

D-40 chose **graspologic** for the background reorganizer's Leiden community
detection (`workers/pipelines.py::reorganize` → `detect_communities`). graspologic
pins **`numpy<2`**, but the `ingest` extra (markitdown → magika) requires
**`numpy>=2.1`**. The two can never co-install, so `[community]` was declared in
`[tool.uv].conflicts` as mutually exclusive with `ingest`, `all`, and `local` —
which made `uv sync --all-extras` (documented in `just setup` / CLAUDE.md) fail
outright and forced `community` out of the `all` bundle.

The actual graspologic surface used was **one function**:
`graspologic.partition.hierarchical_leiden` — plus the `networkx` graph it rode
on. Everything else graspologic pulls (scipy, scikit-learn, pot, numpy) was dead
weight behind a hard numpy ceiling.

## Decision

Replace graspologic with **`leidenalg`** (Vincent Traag's reference Leiden
implementation) over **`igraph`**. `leidenalg` declares **no `numpy` dependency**
(`igraph<2,>=1.0` only), so `[community]` no longer conflicts with anything.

- `[community]` extra: `graspologic>=3.4` → **`leidenalg>=0.10`**.
- The `[tool.uv].conflicts` block is **removed** (all three entries were
  community-only) and `community` is **added back into the `all` bundle** —
  `--all-extras` is unblocked.
- `detect_communities` keeps its **exact signature and `list[list[str]]`
  return**. It builds a weighted, undirected, named `igraph` graph from the edge
  list (`Graph.TupleList`, dropping tombstone/self-loop edges as before) and
  partitions with `leidenalg.find_partition(RBConfigurationVertexPartition,
  resolution_parameter=…, seed=…, n_iterations=-1)` — modularity-with-resolution,
  the same objective graspologic optimized.
- **Hierarchical `max_cluster_size`** — graspologic's `hierarchical_leiden` gave
  a recursive size bound for free; `leidenalg` returns a *flat* partition, so
  `_split_oversized` reproduces it: any community larger than the bound that is a
  *strict* subset of the (sub)graph is re-partitioned on its induced subgraph and
  recursed. A community spanning the whole (sub)graph is indivisible by
  re-partition and accepted as the base case — which guarantees termination and
  matches graspologic's behavior (an indivisible dense community is never
  force-split).
- `mypy` overrides swap `graspologic`/`networkx` → `leidenalg`/`igraph`.

The `randomness` knob (`memories.associative.policies.community.randomness`) is
**retained for config/API compatibility** but no longer separately wired:
`leidenalg` governs refinement randomness internally, seeded deterministically by
`random_seed`. Determinism (rebuild guarantee, D0.1) is preserved via the fixed
`seed`, verified by test.

## Consequences

- Positive: `[community]` co-installs with `ingest` and rides in `all`;
  `uv sync --all-extras` works again; a lighter, faster (compiled C/C++) stack
  with no numpy ceiling; `leidenalg`+`igraph` ship cp313/cp314 wheels (no build
  toolchain — unlike graspologic's heavier tree). `networkx` is no longer pulled
  transitively for this path.
- Negative / cost: the hierarchical size bound is now ~20 lines of our own
  recursion rather than a library call — but it is explicit and unit-tested, and
  it is the only new code. `randomness` becomes advisory.
- Backward-compat: signature, return type, config knobs, policy, and the whole
  `reorganize` calling chain are unchanged; without the extra `detect_communities`
  / `reorganize` remain clean no-ops (logged once at INFO).

## Alternatives rejected

- **Keep graspologic, pin `numpy<2` everywhere** — rejected: it caps the entire
  project below numpy 2 and permanently blocks `ingest`; the whole point is to
  remove that ceiling.
- **networkx-native `leiden_communities()`** — rejected: exists in networkx ≥ 3.6
  but **requires an external backend** (e.g. GPU `nx-cugraph`); there is no
  built-in CPU implementation, so it is not a drop-in.
- **`igraph` alone (`Graph.community_leiden`)** — viable and one dep lighter, but
  seeding runs through igraph's *global* RNG, which is clumsier and riskier for
  the D0.1 rebuild-determinism guarantee than `leidenalg`'s explicit `seed=`.
  Kept as a fallback, not chosen.
- **`cdlib`** — rejected: meta-library pulling many backends; violates slim-core.
