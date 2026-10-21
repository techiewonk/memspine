# Non-LLM enrichment, retrieval and answer gap register

Date: 2026-10-18. Design review; no engine implementation authorised by this document.
Inputs: [all-question feasibility inventory](LOCOMO_NO_LLM_FEASIBILITY.md), local source/evidence review, [accepted enrichment policies](INGESTION_CONTEXT_ENRICHMENT.md), current read/lexical code.

Rows are generic requirements, not a claim that every capability is absent. `V` means current source capability verified; `P` means proposed capability/gate; `D` means dataset/evaluation issue; `M` means measurement gap. No universal non-LLM accuracy ceiling is known.

## Source findings that prevent stale planning

- `services/lexical/base.py`: canonical namespace-scoped async index/index_many/search/delete/exists/clear/close port; index must be replay-idempotent upsert. Lexical scores are backend-specific.
- `services/lexical/projector.py`: WRITE uses optional event `index_text`; FORGET deletes; catch-up batch can defer and flush Tantivy commits. Source truth remains the event log.
- `providers/builtin/lexical.py`: Tantivy descriptor already supports generation-isolated durable placement, analyzer/date-token settings and lifecycle ownership.
- `services/lexical/opensearch.py`: namespace/owner filtering, bounded text query, deterministic secondary ID sort, bulk indexing and erase/existence checks exist. Initial index creation specifies one shard and zero replicas: reliability settings require deployment review rather than assuming HA by selecting a server.
- `read/pipeline/rerank.py`: static embedding prefilter and optional neighbour/date-context reranking already exist; unscored candidates remain distinguished from score zero. These options are not proof of quality for this workload.
- `read/tiers.py::fit_units`: whole bundles are retained/dropped; default continues after overflow. Do not carry forward the obsolete claim that the only packing implementation always breaks at the first large item.

## Generic gaps and solutions

| ID | Type | Gap / failure mode | Recommended non-generative solution | Priority / gate |
|---|---|---|---|---|
| NL-01 | P | Sender conflated with mentioned subject | Envelope principal plus explicit subject binding; cited reported-speech roles | P0: no wrong-principal updates |
| NL-02 | P | Same-name/fuzzy identity merge | Stable IDs and evidence-bound aliases; similarity candidates only | P0: collision cases |
| NL-03 | P | Ambiguous pronouns | Unique explicit compatible antecedent or unresolved occurrence | P0: multiple-person hard negatives |
| NL-04 | P | We assumed to mean both speakers | Explicit group membership and source evidence | P0: joint-event false positives |
| NL-05 | P | Relationship objects mixed (pets, family, coworkers) | Typed subject/object roles with distinct occurrence/entity IDs | P1: ownership swaps |
| NL-06 | P | Prior facts enter without raw citations | Versioned context snapshot; expand to original support/binding refs | P0: no summary-as-proof |
| NL-07 | P | Later evidence leaks into historical views | Event-valid time plus evidence-known time; bounded reconciliation | P0: historical cutoff |
| NL-08 | P | Old history arriving late overwrites current | State/event distinction and effective-time reconciliation | P0: imported history |
| NL-09 | P | Visits/work/home-country overwrite residence | Typed location predicates and supported transitions; multiple homes | P0: residence examples |
| NL-10 | P | Ambiguous place resolved from popularity | Explicit disambiguation or unresolved candidate set | P1: Cambridge hard negatives |
| NL-11 | P | Relative time anchored to ingestion | Reliable source-local clock and supplied/generated flag | P0: missing historical timestamps |
| NL-12 | V/P | Existing time parser not connected to all source bindings | Reuse temporal.py; cite exact anchor source and rule version | P1: anchor integration |
| NL-13 | P | Coarse year/month turned into exact day | Half-open intervals with precision, approximate markers | P0: no invented precision |
| NL-14 | P | Wrong event supplies a date | Event identity, ownership, predicate and source matching before arithmetic | P1: recurring classes/trips |
| NL-15 | P | Intention/date mention used as start endpoint | Explicit lifecycle stages; start/open/plan distinct | P1: duration endpoints |
| NL-16 | P | Inclusive counts confused with elapsed duration | Explicit duration convention and interval answer; preserve uncertainty | P1: same-day/range tests |
| NL-17 | P | Recurrence treated as one event | Occurrence identity with date and participant scope | P1: repeated visits |
| NL-18 | P | Correction invents a transition | Explicit uniquely supported corrected claim, retained history | P0: correction vs move |
| NL-19 | P | One speaker silently overwrites another | Claim provenance and authority checks, competing claims retained | P0: multi-source conflict |
| NL-20 | P | Plans/wishes become completed events | Narrow modality and completion patterns | P0: no false completion |
| NL-21 | P | Negation lost in retrieval/trimming | Preserve polarity and its exact source span; hard-negative checks | P0: never/not/no longer |
| NL-22 | P | Explicit cause versus guessed motive | Causal connective patterns only; unknown otherwise | P1: cause/sequence contrast |
| NL-23 | P | Multi-item answer drops members | Source-bound member sets, dedup and completeness status | P1: list recall |
| NL-24 | P | Count mentions instead of entities/events | Dedup scoped identity keys; count validated complete set | P1: repeated narrative |
| NL-25 | P | Cross-person intersection lacks all branches | Typed set joins with per-subject candidate allocation | P1: both visited city |
| NL-26 | M/P | BM25 vocabulary mismatch | Analyzer alignment, reviewed lexical mapping, optional semantic candidates | P1: recall at fixed budget |
| NL-27 | M/P | Multi-hop secondary evidence absent from question words | Entity/predicate/reply expansion, bounded source dependencies | P1: multi-hop recall baseline |
| NL-28 | V/P | Neighbour windows add unrelated text | Existing window expansion with conversation/version/scope boundaries | P1: benefit and noise measured |
| NL-29 | V/P | One relevance score evicts binding evidence | Rank evidence bundles and protect required dependencies | P1: all-support retention |
| NL-30 | V/M | Static embedding prefilter removes lexical-only evidence | Measure prefilter recall loss; preserve exact/identity/date candidates | P1: no assumed free acceleration |
| NL-31 | V/M | Reranker cutoff loses secondary branch | Rank whole event/source bundle; diversity by branch; keep unscored separate | P1: cut-stage accounting |
| NL-32 | V/P | Budget packing fits text but loses reasoning inputs | Whole dependency bundles, coverage constraints, deferred/incomplete status | P1: fit_units integration |
| NL-33 | P | Trimming removes date/pronoun/negation | Source-addressable sentence/span windows preserving mandatory anchors | P0: offset/semantic integrity |
| NL-34 | P | Duplicate snippets consume budget | Dedup overlapping source spans with stable pointers | P1: tokens vs retained evidence |
| NL-35 | P | Summary/caption/search query treated as transcript | Typed provenance; crawler query never visual proof | P0: media boundary |
| NL-36 | P | Unsupported language/form silently guessed | Tested English rule packs; raw preservation and unresolved reason | P0: coverage declaration |
| NL-37 | P | Unknown units, currency or number interpretation | Explicit numeric/unit parser; retain ambiguous values | P2: no guessed units |
| NL-38 | P | Book/music/trait inference from topic similarity | Explicit validated curated rule if selected, otherwise unresolved/future LLM tier | P2: no stereotype inference |
| NL-39 | P | Source edits/erasure leave links/caches | Version/hash/visibility/erasure fence, descendant invalidation | P0: source lifecycle |
| NL-40 | P | Unbounded rescans or false uniqueness under top-k | Bounded resumable jobs; explicit incomplete search; no global uniqueness claim | P0: backlog/readiness |
| NL-41 | D | Gold conflicts with weekday/date/span | Separate known errata and reviewed adjudication; official/raw scores separate | P0: no benchmark-specific runtime fixes |
| NL-42 | D | Cited evidence points to question, misses answer | Inspect context and track evidence adequacy separately from answer gold | P1: label audit |
| NL-43 | D | Misspelled subject names in question | Mark identity mismatch; do not add production aliases to satisfy gold | P0: generic identity policy |
| NL-44 | D | Adversarial category treated as universal refusal | Evidence-based support/abstention; do not feed category as runtime cue | P0: answerable negatives |
| NL-45 | M | Backend speed confused with retrieval quality | Equal corpus/tokenizer/scoring/top-k; isolate index/query/update/memory costs | P1: kernel-only comparison |
| NL-46 | P | Batch static index mistaken for streaming store | Upsert/delete/exists/reopen/namespace/id-map parity before provider admission | P0: lexical contract |
| NL-47 | P | Server selection mistaken for HA deployment | Explicit authentication, durability, replicas, backups and readiness | P0: deployment contract |
| NL-48 | M | No measured non-LLM answer accuracy | Offline cited-answer prototype and independent semantic review | P1: never report retrieval as answer score |
| NL-49 | P | Fictional people, toys or pictured objects treated as real-world entities | Explicit entity context (fictional/symbolic/live/depicted); preserve unresolved type | P1: stuffed toy vs living pet |
| NL-50 | P | Modifier attached to wrong list member or event | Narrow coordination/attachment patterns; retain original phrase when ambiguous | P1: ingredients, multiple objects |
| NL-51 | P | Related predicates collapsed (allergy/intolerance, offered/completed) | Reviewed typed predicates and explicit assertion status; synonyms are candidates only | P0: do not invent a health claim |
| NL-52 | P | Remote discussion mistaken for travel or event participation | Explicit participation mode and completion evidence | P1: chat about a place vs visit |

## Staged plan before runtime implementation

1. Finish evidence review with exact per-question coverage and unresolved adjudication flags; keep benchmark content local.
2. Finalise outstanding retrieval, conflict, temporal coverage, rank/trim and backend options with owner.
3. Specify ContextBundle, unresolved annotation, typed relation/event and dependency-bundle contracts. Reuse accepted provider architecture and current temporal/read capabilities.
4. Build offline generic operators first: explicit span extraction, time arithmetic, typed sets/counts/joins. Gold stays outside operator inputs.
5. Compare BM25/analyzer/window/metadata expansion at equal source-token budgets; optional semantic ranking only if it fixes measured losses.
6. Focused synthetic tests for identity, time, privacy, source lifecycle, retry and budget behaviour. Optional learned models have explicit profiles and limits, no hidden generative fallback.
7. Only after owner review, implement canonical background ingestion integration. No parallel intake API or provider-specific source truth.

## Questions still requiring selection (all resolved)

> **Resolved 2026-10-18, later in this file:** LX1, TR1, C2, T2, CT1 and K1 were accepted as
> option A ("Owner selections"); R2/S1 were confirmed as engineering with optional measured
> non-generative models ("R2/S1 resolved"). The list below is the original proposal record.

- S1: initial exact/alias/reply + BM25, optional vectors/reranking after measurement (recommended).
- C2: competing claims retained, explicit authorised correction/transition required for supersession (recommended).
- T2: tested relative day/week/month/year and explicit date/range forms first; unsupported references remain unresolved (recommended).
- R2: deterministic ranking initially with optional measured learned reranker profile (recommended).
- TR1: source-preserving dependency bundles/sentence windows, no arbitrary character chopping (recommended).
- LX1: existing Tantivy embedded and OpenSearch server; evaluate BM25S offline and gate any new runtime provider on parity (provisional recommendation pending backend review).
- CT1: return observed lower bounds unless count scope is demonstrably complete; distinguish an exact count in retrieved evidence from a lifetime total (recommended).
- K1: conversation-only assertions initially; optional versioned local catalogues/gazetteers supply separately attributed knowledge after validation (recommended).

Additional reviewed examples refine existing rows: identical product models can denote different physical instances (NL-02/24); recommendations do not establish adopted action (NL-20); maker, donor, owner and commissioner are separate roles (NL-05). Plans must not silently become completed attendance. These require typed relation/event contracts, not another similarity threshold.

Questions describe proposed choices, not accepted defaults. Exact numeric budgets remain to be measured.

## Owner selections — 2026-10-18

Accepted LX1-A, TR1-A, C2-A, T2-A, CT1-A and K1-A. Corresponding proposals above are now accepted policy (not implemented). R2/S1 was answered A+B+C; configurable-mode interpretation and default remain pending clarification. Numeric budgets and concrete model choices remain unmeasured/unselected.

## R2/S1 resolved — 2026-10-18

Owner confirmed engineering with optional measured non-generative models as default; BM25 is included in engineering. Top-level distinction: non-LLM engineering versus separately selected generative LLM fallback. Earlier pending-default notes are historical. Concrete adapters must be classified by their execution; an LLM-based reranker still counts as an LLM. See the final selection in INGESTION_CONTEXT_ENRICHMENT.md.

## Selected contract representations — 2026-10-18

Q-S1..5 = B/B/A/A/B: coordinated envelope replacement, aware-only resolved times with unresolved calendar text, typed values, explicit domain events and persisted excerpt copies with cascade erasure. NL-11..16 must account for deliberately unresolved timezone-free dates; NL-33/35/39 require source pins and reverse copied-excerpt tracking. These are accepted design choices, not implemented gates.

## Operating selections accepted — 2026-10-18

O1..O4 accepted A/A/A/A. Budget, vocabulary and all-copy erasure operating decisions are settled for initial implementation. Coverage mapping is design coverage only; NL behavioural and runtime checks remain open until actually executed.
