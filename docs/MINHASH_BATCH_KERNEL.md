# Bounded MinHash update batches

`DedupPolicy.minhash` preserves its existing normalization and unique-token
set semantics. It still creates `MinHash` with the configured permutation count
and default datasketch seed, scheme, hash function and permutations. Inputs with
fewer than eight unique lowercase whitespace tokens retain the original
per-token `update` loop. Larger sets use datasketch's `update_batch` in bounded
chunks; the minimum hash across chunks is the same commutative reduction as
updating each unique token individually.

Each update batch contains at most
`max(1, MINHASH_BATCH_MAX_CELLS // num_perm)` tokens, with the current
`MINHASH_BATCH_MAX_CELLS` set to 65,536. For supported permutation counts at or
below 65,536, each token-by-permutation matrix is therefore at most 65,536
cells (512 KiB at 64-bit values); datasketch may hold several matrices of that
bounded shape at once. For an unusually large permutation count above the cap,
one token is the minimum batch, so a single matrix may exceed the cell cap by
the permutation dimension. This bounds update temporaries without imposing a
new limit on accepted `num_perm` values.

The acceptance tests compare exact little-endian persisted signature bytes,
scheme, seed, permutations and restored hashvalues against the per-token
reference at 64, 128, 256 and 512 permutations. Cases include empty input,
single/repeated tokens, accented and non-Latin terms, and more than two batch
boundaries. They build reference and production LSH indexes from those
signatures and compare candidate sets. The benchmark performs the same exact
signature check before every timing and records individual repeated trials.

On the refreshed local run, the bounded path was substantially faster for
larger unique-token sets: at 128 permutations, 64 raw / 32 unique tokens
measured 227.25 µs per-token vs 95.07 µs batched; 512 / 256 measured 1.461 ms
vs 0.250 ms; 2,048 / 1,024 measured 5.733 ms vs 0.792 ms; and 8,193 / 4,096
measured 22.786 ms vs 3.347 ms. At 512 permutations and 8,193 / 4,096 tokens,
it measured 24.602 ms vs 4.638 ms. The eight-unique-token case varied across
runs and permutation counts, while one- and two-token cases were generally
slower with batching; the implementation retains per-token updates below
eight. No relevance, LSH recall, or end-to-end ingestion claim is made beyond
the exact candidate-set parity fixtures.

See [JSON evidence](provider_verification/minhash-batch.json), [run log](provider_verification/minhash-batch.log),
and [benchmark script](../scripts/benchmark_minhash_batch.py). This host was
not isolated from concurrent work; the JSON includes runtime, trial samples,
permutation counts and the chunk matrix-cell cap.
