# ADR-034: LadybugDB is the graph engine for graph features; Kùzu retired

- **Status:** accepted (2026-10-05)
- **Date:** 2026-10-05
- **Decision id:** D-60. Amends D-26 and ADR-015 §5; does not change the `graph.provider` default.

## Context

- **Kùzu is frozen.** Its upstream repository was archived on 2025-10-10. memspine shipped a
  first-class `kuzu` adapter behind `[kuzu]`.
- **LadybugDB is the maintained fork.** It is MIT-licensed, on PyPI as `ladybug`: 0.18.0 on
  2026-07-01 was the first release, 0.21.2 on 2026-10-01 is the current one. It keeps Kùzu's
  embedded Cypher API and DDL dialect, including recursive patterns
  (`-[e:Rel* SHORTEST 1..k (r, _ | WHERE ...)]-`) and `ALTER TABLE ... ADD IF NOT EXISTS`.
- **The two cannot share a process.** `kuzu` and `ladybug` register the same native (pybind11)
  types; importing both fails with `generic_type: type "Database" is already registered`. With both
  in `all`, the Kùzu parity tests errored on every `--all-extras` install.
- **The graph plan needs more than the old port** (`GRAPH_REASONING_PLAN_2026-10-05`, KB-1…KB-7):
  - tenant isolation: `graph_edges` had no namespace, so PPR and Leiden scanned every tenant;
  - a multi-hop walk that is not one query per node (the SQLite BFS made one round-trip per
    visited node; the plan estimates ~550 for a depth-2 walk);
  - a per-node fan-out cap, so a hub cannot flood a walk;
  - a subgraph export for a future graph read leg.

## Decision

1. **LadybugDB is the graph engine that graph features build on.**
   - `[graph] = ["ladybug>=0.21,<0.22"]`. One minor at a time: the storage format and Cypher
     surface move between minors (a Kùzu file cannot be opened by LadybugDB at all).
   - Unbounded walks are one native variable-length Cypher query. Capped walks run one query per
     level.
   - `namespace` is a node and rel property; `weight` and `kind` are rel columns.
   - A database created before these columns existed is altered in place and backfilled from the
     properties blobs.
2. **`sqlite_adjacency` stays the zero-dep default and fallback.**
   - `graph.provider` still defaults to `sqlite_adjacency` (D-03 slim core, "profiles stay green").
   - Its walk is one recursive CTE with a per-node fan-out cap.
   - Migration `0003` adds `namespace`, `weight` and `kind` columns, with
     `(namespace, src, weight)` and `(namespace, dst, weight)` indexes, and backfills existing rows.
3. **Kùzu is retired.**
   - `graph.provider: kuzu` and `[kuzu]` remain for **one release** as a deprecated alias.
     `KuzuGraphStore` is now the LadybugDB adapter over a Kùzu connection and emits a
     `DeprecationWarning` on construction.
   - `kuzu` is removed from the `all` bundle.
   - To move: set `graph.provider: ladybug`, install `[graph]`, and run `engine.rebuild()`. The graph
     is a projection (D0.1).
4. **The `GraphStore` port gains:**
   - a `namespace` keyword on `upsert_node`/`upsert_edge`. It defaults to the `namespace` property,
     which is how the projector always tagged record nodes.
   - `namespace` and `max_degree` on `neighbors`;
   - an optional `namespace` on `edge_list`;
   - `subgraph(seeds, depth, namespace=, max_degree=)`.

   The cap rule is one function, `capped_neighbors`: a node's live neighbours, each ranked by its
   strongest edge (weight descending, then id), keep the first `max_degree`. `neighbors` returns
   nearest first, then by id, in every adapter.
5. **Namespace scoping is on everywhere.**
   - The projector stores LINK edges under the event's namespace.
   - `related` (PPR, BFS) reads only the seed namespace's edges. BFS takes an optional
     `related.max_degree`.
   - `reorganize` runs Leiden once per namespace.
6. **A parity suite** (`tests/unit/services/graph/test_parity.py`) holds SQLite and LadybugDB to one
   pure-Python reference walk. It covers:
   - a random graph, a hub, tombstones and several namespaces;
   - BFS at depth 1–3, with and without the cap;
   - the forget cascade and rebuild equality.

   Kùzu joins only in a process without LadybugDB; it passed the same suite in an isolated
   environment.

## Consequences

- **Default behaviour.** `template="core"`, `MemspineConfig()` and `profile="simple"` construct no
  graph store, so nothing changes for them.
- **With associative memory on:**
  - `related(strategy="bfs")` now orders results nearest first, then by id. Before, it used
    discovery order.
  - PPR rankings are unchanged, because links never cross namespaces.
  - With several namespaces, Leiden partitions can differ from the old global run, since the
    modularity null model depends on the graph's total weight.
- **Existing file databases** upgrade through Alembic `0003` (SQLite) or the in-place
  `ALTER`/backfill (LadybugDB). `engine.rebuild()` reaches the same state either way.
- **Pinning one minor** means a deliberate bump per LadybugDB minor. The parity suite is the gate.
- **The `kuzu` alias goes** in the next release, together with `clients/kuzu.py`.

## Alternatives rejected

- **Flip `graph.provider` to `ladybug`.** Rejected: a fresh `profile="simple"` install would then
  need a native extra, which breaks D-03 and "profiles stay green". Graph features opt in.
- **Keep Kùzu as a first-class alternative.** Rejected: it is archived, it cannot co-install with
  LadybugDB in one process, and it doubled the adapter surface for no capability.
- **Route `kuzu` silently to LadybugDB on the same path.** Rejected: LadybugDB refuses Kùzu database
  files, so the alias would fail at open. The alias keeps the Kùzu engine for one release and warns.
- **Per-node queries for the capped Cypher walk.** Rejected: one query per level gives the same
  answer as the reference with depth-many round-trips instead of node-many.
