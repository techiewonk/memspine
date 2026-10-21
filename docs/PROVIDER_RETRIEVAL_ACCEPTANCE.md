# Provider retrieval and memory acceptance

This document records the behavioral boundary for PA-081–089. It separates
correctness gates (identity, temporal filters, namespace isolation, revocation,
repeatable work) from relevance or answer-quality measurements. The checked-in
integration test exercises one real local configuration: SQLite event/storage,
hash embeddings, LanceDB vectors, Tantivy lexical search, in-memory KV and SQLite
adjacency. LadybugDB runs only when its optional runtime is installed. This is
selected-path evidence, not a Cartesian provider matrix or production sign-off.

## Correctness acceptance

| Item | Acceptance case | Current local evidence and boundary |
|---|---|---|
| PA-081 | Associative `src -> dst` event orientation remains intact, while `related` traversal is undirected; a foreign namespace cannot seed or enter the walk; entity node IDs for the same canonical label differ by namespace; date filters exclude records outside the requested interval on hybrid retrieval. | `tests/integration/test_provider_graph_scope_acceptance.py` proves both link directions, namespace refusal, namespaced entity IDs and `valid_from_before` with SQLite adjacency. Graph edges themselves have no temporal fields/filter in `GraphStore`; dates filter records in retrieval. The test does not assert temporal edge history. Ladybug case is skipped when the `ladybug` package is absent. |
| PA-082 | A watch before its due instant is absent; repeated pulls at/after due return the same pending watch until acknowledge; acknowledgement is idempotent; another namespace cannot acknowledge it. | Same integration test verifies these behaviors through the real SQL/event log. Delivery is pull-based. It does not prove broker dispatch, concurrent worker claiming, or exactly-once external side effects; those require a separate worker protocol and idempotency key. |
| PA-083 | A grantee can retrieve a granted semantic record; after revocation, a fresh read must not return it even though the grantor's projections remain populated. Graph access remains namespace-scoped. | Same integration test warms owner hybrid (LanceDB + Tantivy), graph and reader hybrid search, then confirms `shared_search` loses the foreign record after revocation. The implementation adds foreign hits through the vector leg only; the reader's own results use hybrid search. `related` rejects the foreign owner record under the reader namespace before and after a grant. `shared_search` does not use the opt-in `Engine.read` result cache: read-cache keys carry namespace scope and store/erasure/consent generations, but do not establish a shared-search cache guarantee. The configured memory KV cache is not a retrieval-result cache. |

Run the selected local checks with the repository's pinned Python 3.13
environment:

```powershell
$env:PYTHONPATH = 'src'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
python -m pytest -p no:deepeval -o addopts='' tests/integration/test_provider_graph_scope_acceptance.py -q --tb=short -rs
```

## Relevance acceptance is a separate gate (PA-085)

Passing correctness cases says only that the system returns eligible records and
respects scope. It does not say the best evidence ranks first. For every provider
or fusion change, first run the correctness cases above; any wrong-namespace,
wrong-time, erased, revoked, quarantined or duplicate-ID result is a correctness
failure and must not be averaged into a relevance score.

Then evaluate relevance using a frozen, held-out set whose query and relevance
judgments are not present in indexing prompts or model context. Include exact
entity/fact lookups, paraphrases, temporal questions, two-hop graph queries,
lexical-only identifiers, and distractors with overlapping terms. Report each
case's expected record IDs and graded relevance, provider/config fingerprint,
ranked IDs, reciprocal rank/NDCG@k, empty-result rate, latency and token/call
counts. Compare vector-only, lexical-only, and portable hybrid under the same
corpus, query embeddings, `top_k`, filters and candidate budget. Use paired
per-query differences and confidence intervals; do not treat one aggregate score
or a deterministic hash embedding as evidence of model quality. This protocol
defines the acceptance shape; no such quality run is claimed here.

## Query language and literal input (PA-086)

The current Tantivy adapter tokenizes runs of Unicode alphanumeric characters,
lowercases terms, caps query characters/terms and builds term queries. Punctuation
is a separator, not syntax: quotes, `OR`, field names, parentheses and wildcards
must be treated as ordinary text and cannot broaden a query or escape the
namespace filter. The OpenSearch adapter builds a structured `match` request with
an explicit namespace and owner-scope filter; it does not pass user text as
Query DSL. Adapter analyzer differences can still change matches and rank.

Expected acceptance cases for each lexical adapter:

- Quotes, `*`, `:`, parentheses, braces and boolean-looking text never alter
  filters or cause a parser error; punctuation-only input yields no lexical hits.
- A literal identifier such as `build-42/a` follows the documented analyzer
  tokenization and returns only records in the requested namespace.
- English case variants are equivalent under the default lowercase tokenizer.
- Accented Latin text and non-Latin scripts are recorded as separate explicit
  cases; matching must be measured for the configured analyzer, not assumed.
  CJK quality is not promised by the current default tokenizer (D-34 dropped
  CJK support); a tokenizer/backend change needs its own held-out evaluation.
- Multilingual relevance, transliteration and stemming are quality measurements,
  not security/correctness assertions. Queries must remain bounded by the
  character and term limits for every adapter.

The selected integration test executes literal/operator-like and punctuation-only
queries against both the Tantivy lexical leg and public hybrid search, proving
they do not escape the namespace or trigger query-parser syntax. It also verifies
exact accented-Latin terms (`niño`, `pidió`, `café`, `Málaga`) against the
configured default tokenizer in both paths. These are deterministic correctness
checks for this analyzer, not multilingual relevance-quality claims or evidence
for other lexical adapters.

## Compression and evidence budgets (PA-087)

The source distinguishes two operations. Cold-tier zstd stores a reversible
encoding and preserves the content fingerprint, so identity and original text
can be recovered. Assembly compression is opt-in and lossy for a single prompt;
it acts on inflated content. Protected persona, instruction-flagged and disputed
blocks are not compressed/dropped. E5 supports configured assembly rates,
placement budgets and per-block targets, but exact output tokens and semantic
preservation depend on tokenizer/model and input.

An acceptance run must be stratified by memory layer and record, for each input:
record ID, namespace, source/parent IDs, valid-time interval, token count before
and after each stage, protected spans retained, result hash/fingerprint, latency,
model/config version, and whether the chosen output still supports the evidence
facts and dates. Include boundary cases for near-zero budget, long identity names,
contradictory facts, quotes/source citations, date ranges and multilingual text.
Report compression ratio together with evidence-retention judgments and budget
overruns. Never count reversible zstd as a semantic compression gain. No
LLMLingua model-quality/token-budget run was made for this delivery; that requires
an explicitly budgeted local model and held-out inputs.

## Held-out model-quality protocol (PA-088)

No model call is implicit in these tests. Before a quality run, pin a local
model path/revision, tokenizer, hardware/device, prompt version, seed where
supported, concurrency, maximum input/output tokens, maximum call count and
wall-time limit. Use synthetic or licensed held-out cases, keep gold answers and
relevance labels out of prompts, and log only case IDs plus bounded outputs.
Define an abstention policy and score evidence-supported answer correctness,
citation/record coverage, unsupported claims, date/identity preservation,
latency, tokens and failures. Include a no-model baseline and evaluate paired
cases. Stop at the declared budget. The current environment did not authorize or
provide this model budget, so PA-088 remains unexecuted and no quality result is
asserted.

## Native hybrid as a future capability (PA-089)

Portable engine-controlled fusion remains the baseline: lexical and vector ports
are independent and RRF combines their ranked record IDs. The current provider
contracts do not advertise a native hybrid capability or route one. A future
native path must be a separately selected capability with explicit score and
filter semantics, readiness and health, and a single contribution identity.
Evaluation must compare native hybrid against portable fusion on the same corpus,
queries, filters, `top_k`, candidate budget and latency conditions. If the native
result is fused with a vector or lexical leg from the same backend, deduplicate
by canonical record ID and prove one evidence contribution per backend/result;
also test namespaces, time bounds, revocation, tombstones and pagination. Do not
infer native hybrid support from a backend that offers a product-level feature.

The code/contract review found no native hybrid descriptor or capability in the
current selected provider surface; this document leaves implementation and any
ranking claim as future work.

### Bounded external experiment

`scripts/evaluate_native_hybrid.py` ran one bounded comparison using the
digest-pinned Weaviate 1.32.4 image and installed `weaviate-client` 4.23.1
`CollectionAsync.query.hybrid` implementation. The same 26 fixed synthetic
records, hash-64 vectors, five queries, namespace/owner filters and final top-5
were used for native Weaviate hybrid (`alpha=0.5`) and portable RRF over real
Tantivy and LanceDB legs. The harness made 18 search calls including a
pre-deletion tombstone control, verified that the record was present in all
three paths before hard erasure, then verified its removal from native and both
portable projections. A duplicate sentinel in another namespace checked all
three query paths for isolation. Each fused ranking contains unique record IDs.

The four positive synthetic diagnostic queries placed their expected fixture
record first in both methods; the post-erasure query returned no erased ID.
These are small fixture diagnostics with hash vectors, not evidence that native
hybrid improves relevance. The SDK exposes a fused `limit` but no equivalent
internal candidate-pool control: portable RRF requests five candidates per leg
(at most ten unique IDs), while native hybrid returns up to ten fused
candidates. Therefore the external result budget is matched, while internal
candidate work is not measurable or directly matched. See the [JSON report](provider_verification/native-hybrid-evaluation.json),
[run log](provider_verification/native-hybrid-evaluation.log) and
[bounded harness](../scripts/evaluate_native_hybrid.py). This experiment does
not add native hybrid routing or a capability to Memspine.
