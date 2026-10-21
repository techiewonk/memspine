# Engine performance alternatives and adoption rules

Current implementation and evidence: [selected kernel work](PERFORMANCE_IMPLEMENTATION_RESULTS.md).
Bounded RRF prefixes, bounded MinHash updates and shared GLiNER model ownership
are implemented. Real embedding batch and SQLite startup kernels are evaluated;
their defaults remain unchanged.

Owner scope: evaluate fast standard components and use measured improvements in
analysis/execution. No single library is fastest for every input, operation or
deployment. This audit distinguishes existing optimized code, local measurements,
safe adoption and work that still requires model/workload evidence.

| Area | Current implementation | Alternative / decision | Evidence needed or available |
|---|---|---|---|
| Event/storage JSON | `orjson` in `store/events.py` and `services/storage/sql_base.py` | Keep existing optimized bytes path | Canonical payloads already feed stored fingerprints; do not globally replace their format |
| Typed JSON validation | Pydantic models, some stdlib encoding before JSON-mode validation | Benchmark native serialization and guarded `orjson` paths | [JSON audit](PERFORMANCE_ALTERNATIVES.md); preserve JSON-mode validation and error behavior |
| Provider metadata JSON | Nested config/change/receipt payloads | Only adopt a faster path after edge-value parity and guard cost are included | Nonfinite floats, large integers, key types, Unicode and unsupported objects matter |
| Cryptographic integrity | SHA-256 evidence/source/operation checks | Keep SHA-256 where collision resistance is required | xxHash is non-cryptographic; throughput alone cannot justify substitution |
| Content sketches | xxHash + datasketch; SimHash bit votes in Python | Evaluate bounded NumPy bit counting with identical output | [Primitive benchmark](PERFORMANCE_ALTERNATIVES_NON_JSON.md); exact signed-64 sketches must remain identical |
| Ranking/fusion | Portable vector/lexical RRF | Partial top-k can help large pools, but widened/protected tails must remain available | Benchmark parity is not enough to change every cutoff; retain until callers are checked |
| Vector search | Native LanceDB; optional pgvector/Weaviate | Choose backend/index for actual size, update rate and deployment | [Scaling/remote measurements](PROVIDER_PERFORMANCE_FOLLOWUP.md), not a universal backend ranking |
| Lexical search | Native Tantivy; optional OpenSearch/Weaviate | Keep native indexes; test analyzers/literal queries independently | [Retrieval acceptance](PROVIDER_RETRIEVAL_ACCEPTANCE.md) |
| Native hybrid | Portable fusion is shipped behavior | Experimental Weaviate comparison only | Same synthetic corpus/embeddings/filter/top-k; internal candidate budgets differ; no automatic activation |
| Embeddings | FastEmbed/ONNX or explicitly selected provider | Batch only where measured; preserve model/dimension/config identity | Hash-embedding batch timing is not an ONNX/model throughput result |
| Reranking / extraction | Optional local/remote model providers | Bound calls and concurrency; reuse explicitly owned models | [GLiNER Base/Small results](GLINER25_LOCAL_RESULTS.md); smaller can be faster but less precise |
| Compression | Reversible zstd cold storage; optional assembly compressor | Keep lossless/semantic compression metrics separate | [Compression acceptance](provider_verification/compression-acceptance.json); no lossy model-quality claim |
| SQL startup | SQLAlchemy/Alembic initialization and SQLite setup | Profile import, migration, validation separately | No optimization retained merely to move errors later; current startup regression remains explicit |
| I/O concurrency | Async clients, owned resource lifetime, configured deadlines | Reuse connections, bound work/queues; avoid CPU blocking on the event loop | Remote round trips and finite resource soak are measured separately from model work |
| Analytics / reports | JSON reports, raw samples and hashes; existing Arrow-backed vector data | Optimize measured parse/compute hotspots; leave small read-once tooling simple | Do not add a dataframe framework or dependency without an observed workload need |

## Standard for a retained optimization

1. Identify the actual code path and input distribution. Record a correctness
   oracle before measuring; include boundary sizes and malformed input behavior.
2. Compare equivalent operations, including validation, encoding/decoding, copying,
   cold imports and memory allocation where relevant. Report repeated trials and
   raw samples on the same host.
3. Preserve event/projection identities, isolation, source evidence, determinism,
   deadline behavior and optional-dependency boundaries. Bound temporary memory.
4. Run affected tests, independent review, Ruff and typing. Measure the final
   implementation, rather than retaining timings from an earlier prototype.
5. State kernel versus end-to-end gains. Keep current behavior when differences
   are noise or when a safety guard removes the apparent benefit.

Upstream [orjson documentation](https://github.com/ijl/orjson) and
[Pydantic performance guidance](https://docs.pydantic.dev/latest/concepts/performance/)
describe optimized JSON paths, but their benchmarks are not MemSpine results.
[xxHash documents its non-cryptographic scope](https://github.com/Cyan4973/xxHash).
Local evidence and compatible semantics decide adoption here.
