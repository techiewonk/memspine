# ADR-073: Bounded, output-identical SimHash acceleration

**Status:** Accepted
**Date:** 2026-10-17
**Deciders:** Owner requested fast standard alternatives and kernel-only measurements

## Context and decision

The dedup policy's persisted SimHash spends Python work on 64 bit votes per
token. Existing datasketch functionality already requires NumPy. Use NumPy
one-bit counting for inputs of at least eight tokens; retain the short-input
loop below the measured crossover. Process 4,096-token chunks, bounding each
unpacked-bit temporary to 256 KiB. These are implementation tuning constants,
not new provider settings or public knobs.

Preserve lowercase whitespace tokenization, UTF-8 xxh64 input, repeated tokens,
strict positive votes, explicit little-endian bit order and signed-64 output.
No hash algorithm, stored identity, event encoding, MinHash signature or
cryptographic primitive changes. Exact parity tests cover empty/small inputs,
Unicode, repetition, ties, seeded cases and chunk boundaries.

## Alternatives and consequences

| Option | Decision |
|---|---|
| Retain Python voting for every size | Correct but slower in measured longer-input kernels |
| Allocate an unbounded token-by-bit matrix | Rejected; temporary memory scales unnecessarily |
| Chunked NumPy votes with a short-input branch | Selected; identical results and bounded temporary |
| Switch hashes or alter vote semantics | Rejected; would change persisted sketches |

The benchmark measures this CPU kernel only. It makes no full-engine latency,
model-quality, network or production throughput claim. Per the owner's steering,
no complete-engine performance test is required for this optimization. Independent
review, focused parity tests, Ruff and typing remain applicable.

See [microbenchmark and audit](../PERFORMANCE_ALTERNATIVES_NON_JSON.md) and
[adoption standard](../PROVIDER_DELIVERY_STANDARD.md). JSON alternatives were
evaluated separately; faster ordinary-payload encoding did not justify changing
provider metadata or canonical serialization semantics.
