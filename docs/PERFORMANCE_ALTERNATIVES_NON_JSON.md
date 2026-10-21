# Non-JSON performance alternatives

> Follow-up implementation: [current results](PERFORMANCE_IMPLEMENTATION_RESULTS.md)
> supersede the earlier keep-current decisions below for bounded RRF prefixes and
> MinHash batching. Those kernels are now adopted at safe callsites with exact
> parity checks. Shared GLiNER ownership is implemented; real embedding batching
> and SQLite startup have separate bounded measurements, with defaults retained.

This audit checks existing engine hot paths against bounded alternatives that
are already available in the Python 3.13 runtime. It makes no retrieval-quality
claim. Timings are local microbenchmarks, not service-level latency guarantees.
The run used Windows 11, Python 3.13.5, xxhash 4.0.1, NumPy 2.5.4,
datasketch 2.0.0, orjson 3.13.0, zstandard 0.25.0, LanceDB 0.40.0,
Tantivy 0.26.2, ONNX Runtime 1.31.0 and FastEmbed 0.9.0. The host was not
isolated from concurrent work. Each comparison alternated which implementation
ran first, used repeated trials, and reports medians and individual trial
measurements in the [JSON evidence](provider_verification/engine-primitives-benchmark.json)
and [run log](provider_verification/engine-primitives-benchmark.log). The
[harness](../scripts/benchmark_engine_primitives.py) checks exact outputs before
timing.

## Measured change: SimHash voting

`DedupPolicy.simhash64` hashes each lowercase whitespace token with xxh64. The
64 vote totals become a signed 64-bit SimHash persisted on each memory record.
Replacing that loop must preserve tokenization, UTF-8 bytes, repeated-token
weight, strict positive-vote rule (ties remain unset), xxh64 values, and signed
SQLite-compatible output.

The implementation keeps the original Python loop below eight tokens. For
longer inputs it counts little-endian unpacked bits with NumPy in chunks of at
most 4096 tokens. A chunk's unpacked matrix is bounded to 256 KiB; input length
and total vote counts are accumulated across chunks. This preserves exact
results without a temporary proportional to an unbounded input. NumPy is
already installed as part of the current `datasketch` runtime and is used by
the existing MinHash adapter; no dependency was added.

The Python and production implementations returned identical integers for all
measured sizes, empty/repeated/Unicode fixtures, seeded inputs and a fixture
crossing the chunk boundary. The final run measured these medians:

| Tokens | Original loop | Production | Production / original |
| ---: | ---: | ---: | ---: |
| 1 | 5.66 µs | 5.30 µs | 0.94 |
| 2 | 8.21 µs | 7.83 µs | 0.95 |
| 4 | 14.14 µs | 13.96 µs | 0.99 |
| 8 | 27.02 µs | 8.64 µs | 0.32 |
| 16 | 52.62 µs | 10.07 µs | 0.19 |
| 64 | 215.18 µs | 17.65 µs | 0.08 |
| 512 | 1813.22 µs | 86.14 µs | 0.05 |
| 2048 | 7274.52 µs | 316.33 µs | 0.04 |
| 8193 | 28589.20 µs | 1237.47 µs | 0.04 |

The short-input crossover supports leaving inputs below eight tokens on the
Python implementation. The improvement changes only the computation of the
existing SimHash field. Content fingerprints, record identities, MinHash
signatures, and their algorithms remain unchanged. Regression coverage compares
the production result to the frozen vote algorithm for empty, short, repeated,
accented/non-Latin and deterministic 8193-token input, checks signed range and
tie behavior, and verifies the MinHash signature alongside annotation.

## Other hot paths and alternatives

| Area | Current path | Result of this audit |
| --- | --- | --- |
| Content/event fingerprints and cache/table keys | xxh64 over stable, explicitly encoded inputs | Keep current. These values participate in persisted fingerprints, cache lookups, and table names; changing digest or input encoding can break identity or require migration. Cryptographic trust/audit uses remain cryptographic and must not be replaced by xxh64. |
| JSON serialization | orjson is already used for canonical payloads and hot-path transport/storage | Keep current. Serializer alternatives belong to the separate JSON comparison; changing canonical bytes has identity implications. |
| MinHash dedup | datasketch MinHash and LSH, with persisted signature bytes | Keep current. `update_batch`/other signature paths were not adopted because signatures are persisted and parity plus candidate recall would need validation. |
| RRF candidate ordering | Engine helper accumulates per-record scores/rank tuples and fully sorts for its public full-ranking result | A bounded `heapq.nsmallest` implementation had exact top-k score/order parity. It was 11–19% faster only with 256 or 1024 candidates per leg; at 16–64 it was equal or slower. No production change: current callers use adaptive widening and may preserve protected candidates after the cut, and the helper exposes the complete order. A caller-specific top-k contract should be reviewed before applying partial selection. |
| Embedding batches | HashEmbedding already accepts lists; scheduler micro-batches model calls and isolates document/query executors | Keep current. Repeated 64-item deterministic HashEmbedding comparisons across request batch sizes 1/8/32/64 varied by at most 2.7%, within host/run noise. No FastEmbed/ONNX model was loaded, so this does not measure model batching throughput. |
| Vector and lexical stores | LanceDB for vectors; SQLite FTS5 or Tantivy for lexical search | Keep current. The audit did not replace native indexed search with an in-memory alternative. Native hybrid is a separate experimental evaluation and is not enabled here. |
| Blocking I/O | LanceDB, Tantivy and other synchronous adapter operations are offloaded with `asyncio.to_thread` where needed | Keep current. CPU primitive timings do not predict I/O scheduling behavior; changing offload boundaries requires adapter-level latency and cancellation testing. |
| Cold-tier compression | zstandard compression at configured level; assembly compression is optional and model-based | Keep current. A codec/level change affects storage ratio and CPU; lossy assembly quality needs a separately budgeted model evaluation. |
| Caches | semantic cache keys use stable xxh64 content digests; provider/API result caches are separate | Keep current. The benchmark did not find an alternative that preserves key identity and improves a meaningful cache workload. |

## Scope and limitations

The benchmark fixes inputs and measures CPU primitives in one Python process.
It does not test concurrent production load, remote storage latency, real
embedding model throughput, recall, relevance, multilingual answer quality,
compression quality, or memory behavior under arbitrary token counts. The
chunk size bounds the unpacked NumPy temporary; the token list and original text
are already resident as inputs. No hashes used for authorization, integrity or
audit were changed. The full raw trial values and environment inventory are in
the evidence files linked above.

The final kernel run includes the centralized ADR-073 tuning constants. The owner explicitly requested kernel-only performance measurements; no complete-engine performance rerun was added for this optimization.
