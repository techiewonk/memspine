# ADR-043 — Community detection: graspologic-native, hybrid Leiden → LPA, canonical order

- **Status:** accepted
- **Date:** 2026-10-05
- **Amends:** [ADR-028](ADR-028-community-leidenalg.md) (library choice and algorithm)
- **Absorb-list rows:** #24 (KB-8), #82 (GR-LPA), #83 (KB-11), #84 (GR-26), #85 (KB-12), #86 (KB-13)
- **Evidence:** `paper_spine/evaluation/community/COMMUNITY_HYBRID_2026-10-05.md`
  (§5 compares against the code this ADR replaces) and the `bench_*.py` scripts beside it

## Context

ADR-028 moved `[community]` from graspologic to `leidenalg` + `igraph` to escape
graspologic's `numpy<2` pin. Three problems remained:

1. **Licence.** `leidenalg` is GPL-3.0-or-later and `igraph` GPL-2.0-or-later;
   memspine is Apache-2.0. ADR-028's KB-10 note documented the caveat.
2. **Edge order (KB-13).** `detect_communities` built the graph in store order
   (`edge_list` has no `ORDER BY`), and Leiden's result depends on vertex
   order. The same graph with its edges shuffled gave a different partition in
   every run of `bench_vs_current.py`, so the rebuild guarantee (D0.1) held only
   while the store happened to return edges in the same order.
3. **Churn.** Every sleep re-ran Leiden from scratch. On synthetic LFR-like
   graphs, about 3% edge change per sleep split 4–85% of co-memberships, and
   each split community is a summary rewrite.

`graspologic-native` 1.3.1 is the Rust core graspologic itself calls: MIT,
abi3 wheels (Windows/ARM included), and it depends on `numpy>=1.24` and
`scipy>=1.10` with no numpy ceiling, so it does not bring back ADR-028's
conflict with `ingest`. `hierarchical_leiden` takes `starting_communities`,
`iterations`, `randomness`, `seed` and `max_cluster_size`. A clique larger than
`max_cluster_size` comes back as one cluster (checked), which is the
termination rule ADR-028's `_split_oversized` implemented.

## Decision

1. **Canonical edge order (#86).** `canonical_edges` drops tombstones and
   self-loops, keys each pair `(min, max)`, sums parallel edges in sorted weight
   order and sorts by `(src, dst)`. Every partitioner sees only this list.
   This landed first, on the leidenalg code, as a standalone fix.
2. **Library (#83).** `[community]` = `graspologic-native>=1.3,<1.4`.
   `hierarchical_leiden(iterations=10, use_modularity=True, seed, randomness,
   resolution, max_cluster_size)` replaces `find_partition` + `_split_oversized`;
   each node's final-level cluster is its community. `randomness` is now wired
   (ADR-028 kept it advisory). `leidenalg`/`igraph` are no longer imported.
   `detect_communities` keeps its signature and `list[list[str]]` return.
3. **Built-in LPA (#82).** Pure Python, weighted, asynchronous over sorted
   nodes. A node keeps its label on a tie, otherwise takes the lowest tied
   label. Passes are capped, and a label at `max_cluster_size` accepts no
   newcomer. A **collapse guard** rejects any LPA-only result whose largest
   community holds more than 50% of a graph of at least 100 nodes. The previous
   partition is kept instead (no change) and a warning is logged.
4. **Hybrid (#85).** `community.algorithm: auto | leiden | lpa`, default `auto`.
   - `auto` and `leiden` run Leiden, warm-started from the previous partition,
     then up to `refine_passes` (10) LPA passes. Without the extra they are a
     no-op, exactly as before. `auto` does **not** fall back to LPA, because that
     would switch community summaries on for every user without the extra.
   - `lpa` runs LPA alone, without the extra, guarded.
   - The previous partition comes from the live summary parents' `community`
     links, which the graph projection rebuilds from the log. New nodes first get
     one neighbour-majority vote, because graspologic needs a start label for
     every node. Labels are renumbered by smallest member before every call, so
     tie-breaks never depend on an earlier run's numbering.
   - `community.incremental: true` (off by default) places only new nodes each
     sleep: a neighbour-majority label, then at most `incremental_passes` (3) LPA
     passes over new nodes and their neighbours. A warm full rebuild runs when
     incrementally placed nodes exceed `refresh_fraction` (0.10) of the graph or
     every `refresh_every` (5) sleeps. The partition and counters are appended as
     `community_partition` MARKER deltas (node → smallest member of its
     community), which `SessionIndex` folds, so a rebuild from the log
     reproduces them.
   - `resolution` stays 1.0. Resolution 2.0 scored +0.04 to +0.16 NMI on the
     synthetic graphs, but it waits for calibration on real memory graphs with
     the KB-8 benchmark (`evals/bench_graph.py`, #24).
5. **Summary economy (#84).** With `summary_keep_jaccard` < 1.0 (off by default;
   0.8 recommended), a detected community whose membership Jaccard against the
   member set a live parent was summarised from (its `derived_from` targets) is
   at least the threshold keeps that parent. No LLM call and no new record are
   made. Only its `community` links are moved to the new membership. Exact
   matches are claimed first. A newcomer less trusted than the summary forces a
   rewrite, so a summary never stands for a member less trusted than it claims
   (D-47 §5).
6. **Fix: no summary in its own community.** Before this ADR, the parents'
   `community` and `derived_from` edges were partition input, and an active
   parent counted as a member. A community therefore always contained its own
   summary, its fingerprint changed, and every sweep wrote a new parent and
   superseded the old one, even when nothing had changed (reproduced with a
   4-node clique: 1 new parent and 1 superseded per sweep, without end). Summary
   parents and `community` edges are now excluded from partition input, and
   parents never count as members.

## Consequences

- With the extra installed, partitions differ from ADR-028's, because the
  library, refinement and warm start all changed. First-build accuracy is a tie
  with leidenalg run to convergence (±0.01 NMI). Churn per sleep is 1–13%
  instead of 4–85%, NMI after 5 sleeps is equal or up to +0.12, and the hybrid
  is 2–4× faster from 5K nodes at `mu` 0.5. The partition is order-independent.
- Without the extra, defaults are unchanged: `reorganize` is a logged no-op.
- Item 6 changes default behaviour for `[community]` users: unchanged
  communities stop being rewritten every sweep.
- `[community]` now brings in numpy and scipy, which graspologic-native needs.
- An incremental-mode MARKER holds the whole partition on its first write
  (about 80 bytes per node). Later sleeps write deltas only.
- Not built: the modularity-drop refresh trigger, and consensus over several
  Leiden seeds for offline rebuilds. `uv.lock` is not tracked in this repo, so
  there was nothing to relock.

## Alternatives rejected

- **Keep leidenalg, add canonical order only.** This fixes KB-13 but keeps the
  GPL caveat and the per-sleep churn.
- **LPA alone as the default.** It collapses to one community at `mu` ≥ 0.5 and
  shatters at 20K nodes (NMI 0.000 / 1,877 fragments in the evidence file).
- **LPA seeding Leiden.** This was worse than Leiden alone (−0.19 NMI).
- **`auto` falling back to LPA without the extra.** This would silently turn on
  community summaries for every deployment without the extra.
