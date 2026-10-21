# Engineering-first context enrichment review

Date: 2026-10-18. Proposed design; no engine implementation or default changed.

## Policy and scope

Accepted: prefill source-cited candidates, persist after validation; unresolved ambiguous places stay unresolved; preserve dated history and change current state only with supported evidence. Requested direction: engineering and non-generative retrieval first, optional LLM interpretation later. This does not make existing embedding/GLiNER inference rule-based or disable it elsewhere.

## Benchmark inspection

Source: https://github.com/snap-research/locomo ; upstream raw main fetched on review date. SHA256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`, matching the recorded split revision. The opbench copy has a different byte hash; unit fixtures are synthetic.

Structural enumeration: 10 conversations, 272 sessions, 5,882 turns, 1,986 questions. Categories: multi-hop (1) 282; temporal (2) 321; open-domain (3) 96; single-hop (4) 841; adversarial (5) 446. Categories 1–4 total 1,540. Multiple parsed evidence IDs: 427 questions; cross-session evidence: 333 (overlapping counts). Two questions contain parsed evidence IDs absent from their conversation. Image URL fields: 910 turns; nonempty captions: 1,226; crawler queries: 888.

Local structural audit: `evals/runs/_analysis/enrichment_review/structural_audit.json`. This enumerates every QA structurally; it is not a complete semantic annotation or measured extraction coverage. Dataset wording remains in local outputs.

Reviewed cases:

- conv-26 D3:13 + D4:3: home-country binding to Sweden requires later evidence. Retrospective resolution must preserve when evidence became known; an earlier as-of query cannot use it.
- conv-26 D1:3: yesterday anchors to the 8 May 2023 session date, yielding 7 May. Missing timezone prevents claiming an exact UTC instant.
- conv-26 D12:15: last-year Pride reference yields 2022 at year precision, not an exact festival date.
- conv-30 D2:5 + D15:1: question names Jean/John, but cited speakers are Gina/Jon. Flag identity mismatch; never introduce production aliases to satisfy gold.
- Open-domain book/music/religion answers require inference beyond token matching. A genre preference does not prove liking every work. An absent title does not prove non-ownership.
- Captions, image URLs and crawler queries are distinct provenance classes. A search query is not visual evidence. No image downloads/OCR are selected here.

## Linking catalog

Examples are paraphrased/synthetic. Requirements combine reviewed cases, existing failure analysis and general ingestion needs; these are not per-type benchmark counts.

| Information type | Required link / example | Engineering method | Acceptance boundary |
|---|---|---|---|
| Sender identity | I → supplied sender | Envelope identity | Sender is not every mentioned subject |
| Named identity | Two people called Alex | Scoped stable IDs | Same name cannot prove merge |
| Aliases | Robert explicitly called Bob | Cited alias patterns | Fuzzy match proposes only |
| Kinship | My sister Anita | Named possessive relation | Unnamed sister remains unresolved |
| Pronouns | She moved | Unique reply/local antecedent | Competing referents → unresolved |
| Groups | We visited Rome | Explicit participants | All chat participants need not attend |
| Ellipsis | Especially with my children | Bounded adjacency candidates | Adjacency alone is not proof |
| Reported claims | John says Maria moved | Attribution patterns | Preserve source and subject separately |
| Residence | I live in Lisbon | Narrow predicate patterns | Work/visit/origin are separate |
| Residence change | Moved Porto → Lisbon | Explicit transition | Close only supported state |
| Multiple homes | Also a home in Porto | Additive assertion | Do not force single residence |
| Correction | I meant Lisbon | Explicit uniquely targeted correction | Correction is not a move |
| Origin / nationality | Home country Sweden | Separate predicates | No citizenship inference |
| Ambiguous place | Cambridge | Explicit qualifier/local candidates | No unsupported canonical place |
| Temporary presence | Madrid this weekend | Visit/presence pattern | Residence unchanged |
| Relative day | Yesterday | Source-local arithmetic | Unknown original clock → unresolved |
| Coarse date | Last year | Period interval + precision | No invented exact day |
| Historical import | Missing source timestamp | Supplied/generated flags | Ingest time is not event anchor |
| Event-relative time | Two days after conference | Unique cited event | Multiple conferences → unresolved |
| Duration | Studio start → opening | Same-event endpoint arithmetic | Intention is not actual start |
| Ordering | Before graduation | Explicit temporal edge | Absolute date can remain unknown |
| State history | Old/new residence | Valid time + known time | Late old history does not overwrite by arrival |
| Plans/completion | Want to open / opened | Modality/tense patterns | Plan is not completion |
| Negation | Never visited Paris | Polarity patterns | Similarity cannot erase negation |
| Lists | Hiking, picnic, birds | Explicit coordination | Preserve all supported members |
| Counts | Three trips | Distinct event IDs | Mention count is not event count |
| Intersections | Both visited Rome | Join validated tuples | Do not repair mismatched names |
| Preferences | Enjoy classical music | Explicit preference patterns | No automatic song-level conclusion |
| Activities/art | Paint and draw | Reviewed lexical mappings | Arbitrary paraphrase may stay text |
| Units | Five kilometres | Number/unit parser | Missing units remain unknown |
| Language | Original names/scripts | Lookup normalisation | Preserve original raw text |
| Attachments | Read this + image | Typed source pointers | No visual assertion without evidence |
| Cause | Business because lost job | Explicit causal patterns | Sequence does not prove cause |
| Conflict | Different opening dates | Competing cited claims | Similarity is not authority |
| Duplicate support | Three summaries of one turn | Source-family dedup | Not independent corroboration |
| Abstention | Unsupported detail | Coverage/ambiguity outcome | No gold answers or stereotypes |

## Current code and proposed extension

- `src/memspine/derive/prompt_context.py`: known names/aliases, not a cited prior-fact snapshot.
- `src/memspine/ingest/windows.py::_known_from_store`: graph entity aliases, not general prior-value retrieval.
- `src/memspine/ingest/index_view.py`: optional deterministic search hints; hints are not grounded facts.
- `src/memspine/derive/temporal.py` and `temporal_stage.py`: reuse existing precision/relative/interval machinery.
- `src/memspine/derive/compile.py`: preserve support versus binding evidence and reject context-only assertions.
- `src/memspine/derive/integration_stage.py`: reuse existing integration/conflict machinery.

Proposed pipeline: preserve raw → bind source identity/time → narrow versioned patterns → scoped exact/alias/reply retrieval → BM25 candidates → typed identity/time/polarity/evidence validation → grounded effects or unresolved annotations → dated state/projections. Optional word vectors expand candidates only; retrieval score never proves a link.

Reconcile affected unresolved links when later evidence arrives, using bounded idempotent jobs. Preserve event time and evidence-known time; recheck source versions, visibility and erasure at commit. Summaries/index hints cannot become independent primary support.

Proposed context fields, pending schema review: `candidate_id`, `subject_entity_id?`, `predicate?`, `value_surface`, `resolution_status`, `support_refs[]`, `binding_refs[]`, `valid_interval?`, `known_since`, `ambiguity_reasons[]`, `rule_version`, `retrieval_reason`. A mention occurrence may exist without a canonical entity ID.

## Accepted decisions — 2026-10-18

E1 — A accepted: bind explicit identity and cited aliases only. Exact/fuzzy name matches retrieve candidates but do not prove identity. Sender identity resolves first-person references only in the sender's own assertion, not embedded quotations or reported speech.

R1 — A accepted: supported residence transitions, dated history and multiple homes. A visit/workplace does not change residence. Close an earlier residence only when supported; a new residence alone need not exclude another home.

T1 — A accepted: reliable source-local time, period precision and separate evidence-known time. Generated acceptance timestamps cannot stand in for missing historical anchors. Unknown timezone does not prevent a supported local calendar date but prevents inventing an exact UTC instant.

Authority: owner selected all three A options in chat; see ADR-078 / D-100. Contracts and implementation remain in review.

## Accepted second-round decisions — 2026-10-18

E2 — A accepted: resolve only uniquely evidence-bound references. A reply link narrows context but does not alone prove the antecedent. Do not infer gender from a name; require compatible explicit evidence. Group membership must be supported, not inferred from chat participation.
C1 — A accepted: corrections require an explicit or uniquely supported claim target; preserve correction history. A targeted message may contain several claims, so message identity alone does not select the corrected predicate. Apply source authority and visibility checks; one speaker cannot silently rewrite another speaker’s assertion.
L1 — A accepted: automatically revisit affected unresolved links using bounded background jobs, with event-time and evidence-known-time separation. Recheck source versions, scope and erasure; ambiguous later evidence cannot force resolution. No repeated full-history scans or self-reinforcing derived evidence.

Next rounds: corrections, pronouns/groups, retrospective rebinding, language coverage, candidate budgets, optional vector method. No pending choice is accepted implicitly.

## Validation

Synthetic paraphrases, same-name collisions, contradictory places, imported history, missing timezone, calendar boundaries, plans/negation, multiple homes, source edits/erasure and retries. Measure candidate recall separately from accepted-link precision and unresolved rate. No LLM judge required for these checks. Gold QA remains offline evaluation input only. Full-data design inspection means the old held-out split is not an untouched design holdout; independent data is needed for generalisation claims. No runtime score or speed measurement claimed.

## Accepted third-round decisions — 2026-10-18

N1 — A accepted: tested English semantic patterns first; preserve all original languages and language-independent identity/explicit links. Unsupported semantic forms remain unresolved, including unsupported English constructions. Do not silently translate or assume language from a name.
B1 — A accepted: configurable bounded candidate retrieval and reconciliation, with explicit partial/deferred outcomes and resumable cursors. Numeric limits require focused measurements before promotion. Partial candidate coverage cannot establish that an antecedent is globally unique; uniqueness requires a complete relevant scope or explicit identity evidence.
F1 — A accepted: persist source-bound unresolved annotations with reason, scoped candidate pointers and retry eligibility; raw text remains searchable. No guessed facts or automatic LLM fallback. Candidate pointers inherit visibility restrictions and do not assert candidate identity.

## Pending fourth-round decisions

S1: exact IDs/explicit aliases/reply candidates plus BM25 initially; optional word vectors later behind measured recall/precision gates (recommended).
C2: retain competing source-bound claims; only authorised explicit correction/transition can supersede applicable claims (recommended).
T2: allowlist tested calendar expressions; preserve coarse precision and unresolved temporal relations when unsupported (recommended).

## Full-question offline inventory

See [non-LLM feasibility audit](LOCOMO_NO_LLM_FEASIBILITY.md): 1,986 automated question records, cited evidence and question-only BM25 retrieval measurements. Semantic review remains pending; no maximum answer score claimed. The owner permits considering word-vector/embedding/reranker methods for candidate ranking, trimming and extraction support; no model/provider selected. Preserve support/binding/date/negation spans when trimming.

## Accepted fourth-round selections — 2026-10-18

- LX1-A: retain Tantivy embedded and OpenSearch server providers. Evaluate BM25S offline; gate BM25x on Python 3.13 packaging, stable identities and crash-safe persistence. No speed superiority is established.
- TR1-A: source-preserving evidence dependency bundles; exact source spans/sentences may be selected only while required identity, dates, negation and support remain intact. If complete support does not fit, expose incompleteness.
- C2-A: retain competing source-bound claims; supersession requires an authorised supported correction or transition.
- T2-A: reuse existing tested explicit/relative calendar forms and evidence-bound event anchors; preserve precision and unresolved forms.
- CT1-A: scoped counts with explicit completeness/lower bounds; repeated retellings are not distinct events.
- K1-A: optional validated versioned local catalogues with separate provenance; ambiguous mappings unresolved and no automatic web/LLM call.

R2/S1 response was “A + B + C”. Interpreted as interest in supporting configurable engineering-only, optional-model and model-required modes, not as simultaneous contradictory defaults. This interpretation and default selection remain pending confirmation. No model requirement, provider name, download, automatic escalation or runtime profile is finalised by that response. Recommended default is engineering-first with optional measured non-generative models. Model-required mode, if selected, must fail clearly when required services are missing rather than silently degrade.

## Final retrieval selection and terminology — 2026-10-18

R2/S1 confirmed: default is engineering with optional measured non-generative models (mode 2). BM25 is explicitly engineering. For this design, the top-level policy distinction is non-LLM engineering versus generative LLM interpretation/fallback. Non-LLM engineering includes rules, exact identity/alias/reply lookup, BM25, temporal arithmetic, typed joins, optional static word vectors, sentence embeddings, discriminative NER and non-generative reranking. Learned components still require explicit configuration, dependency/model availability, resource budgets and validation; calling them engineering does not make them deterministic, free or evidence-authoritative.

Supported configuration intents remain: engineering-only without learned components; default engineering with optional measured non-generative components; and engineering requiring configured semantic retrieval/reranking. These are design intents, not new runtime schema keys. Generative LLM fallback is disabled unless separately selected and configured. No automatic downloads or generative fallback follow from an optional component being unavailable. Optional degradation must be observable; required components must fail clearly.

Classify adapters by actual execution: a small discriminative cross-encoder is non-generative engineering; a reranker that calls a generative LLM remains an LLM call even if named a reranker. Embedding/NER inference is distinct from generative extraction and must retain its own model/task identity. Concrete model/provider selections, numeric budgets and focused promotion checks remain pending implementation specifications.

## Exact contract proposal — 2026-10-18

[Schema and contract review](ENRICHMENT_SCHEMA_CONTRACTS.md) now specifies target entity/mention/claim/domain-event/time/evidence/bundle/context/effect records, exact JSON types and NL-01..52 coverage. Accepted policies above remain final; technical representation choices Q-S1..5 were subsequently selected (see final selection below). No runtime implementation, activation or compatibility path is introduced. Historical pending-default notes above were resolved by the final R2/S1 selection.

## Final schema selection — 2026-10-18

Q-S1 B: coordinated replacement of affected envelope shapes; one path/no compatibility. Q-S2 B: aware datetime values only; timezone-free dates remain unresolved source text (no inferred UTC). Q-S3 A: typed claim values/predicate policies. Q-S4 A: explicit domain event records. Q-S5 B: persist full selected excerpts with source pins, copy manifests and cascade erasure. These supersede earlier representation recommendations. Context parsing may retain original date wording and its text precision, but cannot promote it to a resolved civil-date value under the selected policy. Contracts/examples are updated in ENRICHMENT_SCHEMA_CONTRACTS.md. Runtime implementation remains not started.

## Selected evidence budget and operating proposal — 2026-10-18

Owner selected 4,096 evidence tokens. ENRICHMENT_OPERATING_POLICY.md defines the other proposed numeric limits, initial vocabulary and all-copy erasure verification boundaries. These are not measured throughput/latency promises. Unknown source timezone remains unresolved text under Q-S2-B; copied excerpts retain cascade-erasure obligations under Q-S5-B.

## Operating selections accepted — 2026-10-18

Operating choices O1..O4 are accepted A/A/A/A. ENRICHMENT_OPERATING_POLICY.md is the current authority for balanced 4K limits, 43 gated predicates/nine attributes, per-surface asynchronous erasure and development-off/production-seven-day backup policy. Earlier pending/proposed operating-choice statements are historical. Runtime implementation and backend verification remain pending.

## Generic vocabulary review — 2026-10-18

See [complete generic catalog](GENERIC_PREDICATE_ATTRIBUTE_CATALOG.md): accepted 43 predicates/nine keys preserved; 10 predicates and 35 keys proposed for broader reuse. New review findings: measurement/count/frequency property keys, domain-constrained biological roles, multi-party/comparison qualifiers and catalogue provenance. Current schema restrictions remain unchanged until these refinements are selected. No runtime registration or extraction implementation is claimed.

## Semantic structure and usage guide — 2026-10-18

[SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md](SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md) describes the proposed pure semantic registry, one-pack ownership of all 43 starter predicates, property categories, role handling, ingestion/read flow and memory-layer mapping. Existing accepted policies remain unchanged. Proposed key-category/role/type refinements and extra 10/35 vocabulary items are not activated; target schema changes await review.

## Vector-assisted non-generative extraction — 2026-10-18

[BM25_VECTOR_ASSISTED_EXTRACTION.md](BM25_VECTOR_ASSISTED_EXTRACTION.md) specifies two distinct candidate indexes (semantic definitions versus prior evidence), optional vector-assisted labels/slots, source-grounded validation and unresolved outcomes. Existing static read prefilter is not an ingestion extractor; current model loader can download, so offline enforcement must be implemented before reuse. No Word2Vec library/model is selected and no generative call is introduced. Proposed per-segment label caps remain review values.

## CPU non-generative alternatives review — 2026-10-18

[CPU_NON_GENERATIVE_MODEL_OPTIONS.md](CPU_NON_GENERATIVE_MODEL_OPTIONS.md) compares sparse classifiers, static vectors, fastText, spaCy, small GLiNER extraction, sentence encoders and tiny rerankers with current code/packaging. Recommendations are research candidates, not model switches or measured speedups. Reuse named task profiles; no downloads, training, runtime changes or generative ingestion calls introduced.
