# Lexical backend options: source review

Reviewed 2026-10-18. Planning only: no dependencies installed, services started, engine code changed, or performance measured. Package metadata was fetched directly from PyPI; source was inspected from upstream. Main-branch code can differ from the published package.

## Recommendation

Keep **Tantivy for the embedded mutable projection** and **OpenSearch for a dedicated server lexical deployment**. Existing Weaviate remains an option when the deployment also needs its vector service. Treat **BM25S as an optional static-corpus/audit accelerator**. Treat **BM25x as a gated experiment**, not a replacement: Python 3.13 packaging, stable identities, persistence and concurrency need resolution first.

A faster BM25 implementation changes scoring throughput, not the semantic coverage of entity resolution, time binding, evidence joins or extraction. Do not infer improvements to answer accuracy from throughput claims.

## Comparison

| Requirement | Existing Tantivy | Existing OpenSearch | Existing Weaviate | BM25S | BM25x |
|---|---|---|---|---|---|
| Placement | Embedded | External server | External server | Embedded library | Embedded Rust library with Python binding |
| Current MemSpine adapter | Yes | Yes | Yes, lexical and vector adapters | No | No |
| Mutable content | Stable-ID upsert/delete | Stable-ID upsert/delete | Stable-ID upsert/delete | Eager corpus index; changes require rebuilding affected snapshot | Add/update/delete APIs |
| Stable external identity | `record_id` | `record_id` | Deterministic object identity | Adapter must map positional corpus IDs | Adapter must map numeric IDs; deletion shifts later IDs |
| Namespace boundary | Exact structural filter | Owner plus namespace filters | Owner plus namespace filters | Separate scoped indexes recommended; score mask is not sufficient isolation | Build authorized subset before search or use separate indexes; maintain mapping across mutations |
| Persistence | Durable local index when configured | Server-managed index | Server-managed collection | Save/load arrays; optional mmap | Automatic save; mmap load; mutation materializes mapped data |
| HTTP service included in reviewed implementation | No | Yes | Yes | Not established; library/CLI is not a production search cluster | No server implementation found in reviewed tree |
| Concurrency relevant to integration | Existing writer locking, threaded blocking work | Async client plus backend concurrency | Async client plus backend concurrency | Batch query threads; immutable publication needed around rebuild | Rayon batch work; Python mutable object access needs serialization and cancellation tests |
| Best fit now | Normal local ingestion/search | Dedicated remote lexical service | Existing vector plus lexical deployment | Read-mostly audit/index snapshot | Experimental batch workloads after gates |

“Existing adapter” means code exists, not that live deployment acceptance has been rerun here.

## BM25S: verified details and limits

- Official design uses eager sparse scores. It supports five BM25 variants, array save/load and memory mapping. Its benchmark compares selected BEIR workloads; this does not establish a speedup over our current Tantivy provider. [README](https://github.com/xhluca/bm25s)
- The reviewed source exposes `index`, `retrieve`, `save`, `load`, `n_threads` and `weight_mask`; it does not expose a streaming stable-ID upsert/delete API. `weight_mask` multiplies scores, and top-k can still return zero-scoring excluded positions. BM25L/BM25+ non-occurrence adjustments also occur after masking. Therefore masking must never be treated as an authorization boundary. Prefer scoped corpus snapshots, with a final identity/scope check. [Scoring and retrieval source](https://github.com/xhluca/bm25s/blob/03eac5a8fb3aa4958ce37415a4cf2220d35157f7/bm25s/__init__.py)
- Observed PyPI release **0.3.13**: `requires-python >=3.8`, universal `py3-none-any` wheel and source archive. This makes package distribution compatible in principle with Python 3.13; it is not a tested runtime guarantee for all extras. Base dependency is NumPy; Numba, SciPy and stemming are extras in the reviewed metadata. Do not install the full extra merely for lexical search. [PyPI metadata](https://pypi.org/pypi/bm25s/json), [build configuration](https://github.com/xhluca/bm25s/blob/03eac5a8fb3aa4958ce37415a4cf2220d35157f7/setup.py)
- MIT licensed. Source snapshot: `03eac5a8fb3aa4958ce37415a4cf2220d35157f7`. [License](https://github.com/xhluca/bm25s/blob/03eac5a8fb3aa4958ce37415a4cf2220d35157f7/LICENSE)

Suggested offline design: build one immutable generation per authorized corpus, persist stable record-ID mapping and tokenizer configuration with it, validate generation hashes, then publish atomically. Rebuild after changes and erasure. This is a proposed adapter design, not native transactional mutation support.

## BM25x: verified blockers and caveats

- Upstream exposes streaming mutations, subset search, five scoring variants and CPU/GPU batch paths. Its largest advertised GPU advantage uses four H100s and 8.8 million documents. That setting is unlike small interactive conversation queries; no local speed claim follows. Deletion compacts positional IDs. Apache-2.0 licensed. [README](https://github.com/lightonai/bm25x)
- Observed PyPI release **0.3.1** lists `requires-python >=3.9`, but the actual release files are **only CPython 3.12-specific wheels** for Windows x64, macOS x64/ARM64 and Linux x64/ARM64. No CPython 3.13 wheel or source archive is listed for that release. Thus the published release is not a normal pip-install choice for our Python 3.13 runtime. The publishing workflow explicitly selects Python 3.12. A custom source build would be a separate, unverified path. [PyPI metadata](https://pypi.org/pypi/bm25x/json), [publish workflow](https://github.com/lightonai/bm25x/blob/287066f8c855c42793917b4462b3b126777f3e61/.github/workflows/publish.yml), [Python build configuration](https://github.com/lightonai/bm25x/blob/287066f8c855c42793917b4462b3b126777f3e61/python/pyproject.toml)
- `add`, `delete` and `update` materialize mmap-backed postings into memory before mutation. Automatic persistence calls full `save`; an incremental in-memory scoring design does not imply a small incremental disk write. [Index implementation](https://github.com/lightonai/bm25x/blob/287066f8c855c42793917b4462b3b126777f3e61/src/index.rs)
- Reviewed save code writes header, posting arrays and vocabulary directly to their target paths. No atomic generation swap, WAL or fsync protocol was found in that save path. A process interruption can leave a partially published set. Raw MemSpine events would remain truth, but a production adapter still needs detection/rebuild or atomic generation publication. Multiple processes must not write the same directory without a proven ownership protocol. [Persistence implementation](https://github.com/lightonai/bm25x/blob/287066f8c855c42793917b4462b3b126777f3e61/src/storage.rs)
- Python search takes mutable access to the wrapper. Indexing releases the GIL; that alone neither makes async calls nonblocking nor establishes safe concurrent object access. Use an adapter-owned execution boundary and test cancellation, lifecycle and overlapping calls. A search subset is an explicit list of positional IDs, not a metadata-query engine. [Python binding](https://github.com/lightonai/bm25x/blob/287066f8c855c42793917b4462b3b126777f3e61/python/src/lib.rs)
- Source snapshot: `287066f8c855c42793917b4462b3b126777f3e61`. Code findings apply to that reviewed snapshot; do not silently assume exact parity with release 0.3.1.

## Fit with our provider contract

The canonical [LexicalStore](../src/memspine/services/lexical/base.py) requires namespace-isolated search, idempotent stable-ID indexing, deletion, erasure verification through `exists`, rebuild through `clear`, and resource closure. `index_many` is explicitly not an atomic batch. Backend scores have different scales; existing RRF can combine ranks without assuming identical raw BM25 scores.

Relevant implemented paths:

| Component | Evidence |
|---|---|
| Tantivy factory, settings validation and projection generations | [Factory](../src/memspine/providers/builtin/lexical.py) |
| Tantivy writer lock, threaded blocking operations, batched commits and exact namespace query | [Adapter](../src/memspine/services/lexical/tantivy.py) |
| OpenSearch lifecycle, health probe, stable deployment identity and owned index | [Factory](../src/memspine/providers/builtin/opensearch.py) |
| OpenSearch owner/namespace filter and literal full-text input | [Adapter](../src/memspine/services/lexical/opensearch.py) |
| Weaviate separate lexical/vector projections | [Factory](../src/memspine/providers/builtin/weaviate.py) |
| Weaviate BM25 filter | [Adapter](../src/memspine/services/lexical/weaviate_store.py) |

Neither candidate should introduce a second write pipeline or a special case in retrieval. Any accepted candidate must register through the existing configurable lexical provider catalog and own its settings/resources.

For remote lexical search, OpenSearch is the straightforward existing route; its full-text match interface is documented upstream. Weaviate also documents keyword search, but our existing integration uses separate lexical and vector collections plus MemSpine fusion: it is not proof of native single-call hybrid execution. [OpenSearch match](https://docs.opensearch.org/latest/query-dsl/full-text/match/), [Weaviate keyword search](https://docs.weaviate.io/weaviate/search/bm25)

## Acceptance gates before any new provider

1. Python 3.13 Windows and intended Linux deployment wheels, import and reopen checks; explicitly pin the reviewed version.
2. Stable record-ID mapping through repeated indexing, changed content, deletion and reopen; no positional-ID corruption.
3. Scope isolation before ranking and after hydration, including all-zero queries, no-match queries and top-k larger than authorized corpus.
4. Immediate erasure verification, including mapped files, stored text, corpus manifests and inactive generations.
5. Crash-safe publication or corrupt-generation detection plus event-log rebuild; projection watermark must not overclaim durability.
6. Serialized mutation, read/mutate lifecycle, cancellation and multi-process ownership tests; no blocking the event loop.
7. Matching tokenizer policy, stopwords, stemming, parameters, corpus and top-k for comparison. Preserve negation/time/entity tokens needed by extraction; lexical stopword removal must never change raw evidence.
8. Kernel-only measurements: single-query p50/p95, batch throughput separately, mutable update/delete latency, reopen, memory and index size. Include current Tantivy, not only rank-bm25. Keep cold/warm and CPU/GPU results separate.
9. Retrieval quality: evidence recall and complete evidence-set coverage, including secondary binding turns. Faster search with lower coverage is not an acceptance result.

## Decisions still required

| Decision | Recommended option | Alternative |
|---|---|---|
| Production embedded lexical | Keep Tantivy while evaluating candidates offline | Replace only after contract and measured gates pass |
| Dedicated lexical server | Existing OpenSearch provider | Weaviate when consolidating vector and lexical infrastructure is the explicit deployment goal |
| BM25S scope | Offline/static audit experiment | Mutable provider with generation rebuild complexity |
| BM25x scope | Defer promotion pending Python 3.13 distribution and durability/identity gates | Maintained custom source build plus adapter engineering |
| GPU lexical | Off initially; CPU latency first | Separate large-batch experiment after CPU evidence |

No selection or benchmark superiority is implied by this document.

## Owner selection — 2026-10-18

LX1-A accepted: retain Tantivy embedded and OpenSearch server; evaluate BM25S offline first and defer BM25x runtime promotion until compatibility/identity/persistence gates pass. This selects a direction, not a measured performance result or new provider implementation. Existing Weaviate remains an available configured alternative; no new combined hybrid API is selected.
