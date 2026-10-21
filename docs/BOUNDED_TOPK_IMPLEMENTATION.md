# Bounded RRF prefixes

`rrf_fuse` now accepts an optional keyword-only `limit`. The default remains
the full sorted result. A limit is validated as a non-negative built-in integer
(booleans are rejected); zero returns no results after the original score
accumulation has run. Input legs are never truncated. Duplicate contributions,
weights, extra legs, score addition order and rank-tuple tie breaks are unchanged.

For limited calls, the helper retains the C-backed full sort for fewer than
1024 fused record IDs and when the requested prefix is at least one eighth of
the fused set. Otherwise it uses `heapq.nsmallest` with the same `(-score,
leg-rank-tuple)` key. If any fused score is non-finite (NaN or either infinity),
it uses the original full sort: Python's sort order around NaN is not a total
order, and heap selection could change that observable behavior. The crossover
values are implementation constants in `config/constants.py`, not public
retrieval settings.

The main read router still requests the full ranking. It uses the tail for
date-prior boosting, protected per-leg/source-family hits, balanced reranking,
and forensic cut reporting. Associative RRF also remains full because it
removes the seed and returns every other graph neighbor. Two graph helper paths
only use the first `GRAPH_LEG_FALLBACK_HITS` (three) fused IDs to seed expansion;
they pass that limit directly, preserving their previous seeds exactly while
avoiding a full ordering for sufficiently large/widened pools.

Focused tests compare limited outputs with a frozen full-sort reference over
seeded duplicated IDs, ties, weighted and extra legs, negative and zero weights,
NaN/infinite scores, and signed floats. They cover empty, zero/full/oversized
limits, reject invalid limits, and verify that a zero limit does not suppress
errors raised during the existing score accumulation. No relevance or
end-to-end retrieval claim is made.

The bounded benchmark compares the full-sort reference to the final production
helper over repeated small and large synthetic pools, with exact output checks
before timing. See [JSON results](provider_verification/bounded-topk.json),
[run log](provider_verification/bounded-topk.log), and
[benchmark script](../scripts/benchmark_bounded_topk.py). The host was not
isolated from concurrent work; individual trials and runtime details are in
the evidence JSON. On the final root-review run, a three-item prefix measured
5.91 µs vs 6.43 µs for 16 hits per leg (10 unique IDs), 408.43 vs 417.41 µs
for 1024 hits per leg (639 unique IDs), 886.81 vs 832.47 µs for 2048 hits per
leg (1299 unique IDs), and 2144.37 vs 1843.89 µs for 4096 hits per leg (2585
unique IDs), full-sort then production. At 4096 hits per leg, a 32-item prefix
measured 2146.75 vs 2025.25 µs. Root review also removed an unnecessary
full-list slice on the default full-ranking path. Small and medium pools were mixed and noisy;
the conservative 1024-candidate crossover avoids heap selection there. The
large-pool result supports bounded ordering for unusually wide/widened search
pools, but is a kernel measurement, not an end-to-end speedup claim.
