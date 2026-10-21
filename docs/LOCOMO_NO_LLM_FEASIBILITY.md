# LoCoMo: non-generative feasibility and retrieval audit

Date: 2026-10-18. Status: all-question automated inventory and scoped reviewer-agent reading of question/gold/cited raw text completed; surrounding-context/media adjudication and answer solver pending. No engine defaults changed.

## Scope and reproducibility

Audit all 1,986 questions against raw turns and cited evidence from SHA256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. Category 5 remains separate. Full-text outputs are local and ignored under `evals/runs/_analysis/enrichment_review/no_llm_audit/`:

- `index.html`: full-width filterable table, full-screen button, every question/gold/cited turn/source time/engineering hypothesis/known erratum/BM25 ranking.
- `questions.jsonl`: one record per question, including missing evidence IDs and media provenance flags.
- `summary.json`: counts and retrieval measurements.

Generator: `evals/audit_locomo_no_llm.py DATASET --out LOCAL_OUTPUT` (standard library only). No LLM, embedding model, live search or engine inference in this audit. Full-data design inspection precludes calling the existing split an untouched design holdout. Gold is used only for offline diagnostics after question-only ranking, never as an extraction feature.

## Measured baseline: retrieving all labelled evidence

Each query searches only its corresponding conversation. BM25 indexes speaker plus raw message text; no captions, derived summaries, stemming, window expansion or semantic models. Full-conversation search has no historical as-of cutoff. Denominators exclude questions with no resolvable cited turn, and partially broken labels are retained with visible missing-ID fields.

| Category | Evidence-evaluable questions | All evidence @5 | @10 | @20 |
|---|---:|---:|---:|---:|
| Single-hop | 841 | 481 | 541 | 588 |
| Multi-hop | 282 | 13 | 28 | 42 |
| Temporal | 321 | 181 | 205 | 217 |
| Open-domain | 92 of 96 | 13 | 17 | 21 |
| Total categories 1–4 | 1,536 of 1,540 | 688 | 791 | 868 |

These are retrieval measurements, not correct answers. At 20 turns the baseline retrieves all resolvable labelled evidence for 56.5% of evaluable questions. Multi-hop is the clearest retrieval weakness. Evidence-label completeness/correctness is not assumed; read these alongside errata and missing IDs.

## Per-question review routes, not solvability scores

| Automated review route | Questions | Interpretation |
|---|---:|---|
| Literal gold span in cited raw text | 479 | Span-selection candidate, not verified question understanding or correct extraction |
| Temporal rules/event binding | 347 | Candidate calendar/interval work; correct event selection still required |
| Structured extraction/paraphrase | 570 | Need entity/predicate/value interpretation or aggregation |
| Existing errata review | 48 | Known review entries attached; existing list is incomplete |
| Open-domain inference | 96 | Needs semantic/external-rule review; not automatically impossible without an LLM |
| Adversarial/abstention review | 446 | Category alone does not establish unanswerability; some known labels are questionable |

These routes partition questions by priority rules. Operator hypotheses overlap and can be wrong. A literal-span signal is neither a lower bound nor upper bound on achievable accuracy. There is no justified maximum non-LLM answer score yet.

## Scoped semantic review coverage

Two Codex reviewer agents directly read every supplied question, gold and cited raw-text evidence. These reviews are offline design work; they are not LLM calls in the engine being proposed. All 1,986 per-question records now carry a review overlay. Automated operator hypotheses retain their original provenance; reviewer reading does not turn them into certified labels.

| Conversation | Questions reviewed |
|---|---:|
| conv-26 | 199 |
| conv-30 | 105 |
| conv-41 | 193 |
| conv-42 | 260 |
| conv-43 | 242 |
| conv-44 | 158 |
| conv-47 | 190 |
| conv-48 | 239 |
| conv-49 | 196 |
| conv-50 | 204 |
| Total | 1,986 |

First partition has 157 specific addenda; second has 221 questions with specific curated observations. These are investigation notes, not counts of confirmed benchmark defects. Every item remains solver-unvalidated. The supplied citations can omit the answer, contain only a question or require surrounding context/media. Full-transcript adjudication is still needed for flagged cases; absence from cited text is not absence from the conversation.

Important follow-ups by local question ID: `0-5` weekday conflict; `1-9` subject-name mismatch; `1-31` duration endpoints and lifecycle; `3-50` symbolic object versus live pet; `4-7` remote interaction versus travel; `5-41` competing attribute claims; `6-31` elapsed-day arithmetic; `7-78` unrelated citation; `8-54` body-part mismatch; `9-151` wrong-person citation. Do not silently correct official gold or alter runtime aliases from these observations.

## How far engineering can go

| Layer | Engineering / non-generative work | Verification gate |
|---|---|---|
| Raw integrity | Source IDs, timestamp provenance, scope, recoverable storage | Exact source roundtrip and indexing coverage |
| Candidate recall | BM25, explicit IDs/aliases, reply expansion, date filters, neighbour windows | Labelled evidence recall, missing-label review |
| Semantic candidates | Optional static word vectors or sentence embeddings | Additional recall versus noise/latency on independent cases |
| Ranking | Lexical/entity/time feature scoring; optional learned reranker | Relevant-event retention, negation/identity hard negatives |
| Grounded extraction | Tested patterns for subject/predicate/value, dates, negation, plans, explicit cause | Exact cited spans, accepted-link precision and unresolved rate |
| Linking | Evidence-bound aliases/pronouns, place roles, event identity, later binding | No wrong-person/place links; preserve known-time boundaries |
| Computation | Time arithmetic, distinct counts, list union/intersection, state intervals | Correct typed inputs, completeness and calendar tests |
| Assembly/trimming | Deduplicate, source-addressable snippets, bounded budgets | Retain all required support, date anchors, negation and binding spans |
| Final fallback | Unresolved outcome; future bounded model-assisted interpretation | Model cannot bypass grounding/source policy |

LLMs are last resort for unsupported semantic interpretation, not automatically required for every fact. Non-generative methods still cannot prove arbitrary commonsense, intended motive or missing visual content. External curated knowledge could support some open-domain rules but is not selected and must have its own provenance and coverage limits.

## Word2vec, embeddings and rerankers

The owner permits considering non-generative models for reranking/trimming/extraction support. This is not selection of a concrete model/provider.

| Method | Useful role | Cannot establish alone |
|---|---|---|
| Static word2vec | Related-word expansion; pooled-vector candidate similarity | Word sense, sentence negation, event dates or identity |
| Sentence embeddings | Paraphrase candidate retrieval/ranking | Truth, source authority or exact event match |
| Cross-encoder reranker | Joint question/passage relevance ranking | Factual extraction correctness; it has inference cost |
| Sequence tagger / NER | Mention spans/types for an extractor | Relations, identity merges and state transitions |
| Deterministic extractor | Narrow validated relations and temporal forms | Arbitrary unseen paraphrases |

Recommended sequence: BM25 + explicit metadata baseline → neighbour/entity/date expansion → feature scoring → optional semantic retrieval/reranking if measured useful. Do not add word2vec as a generic substitute for every stage. Trimming must preserve source IDs and required support/binding/negation/date spans; a top-ranked incomplete snippet is not safe evidence.

## Next concrete validation work

Companion plans: [generic gaps and solutions](NON_LLM_GAP_REGISTER.md),
[source-reviewed lexical alternatives](LEXICAL_BACKEND_OPTIONS.md).
Question/evidence semantic review is tracked separately from the automated routes
in the local `semantic_review_*` files; read their coverage manifests before
claiming completion. Reading cited text is not adjudication of full conversation
context, original media or an executed non-LLM answer solver.

1. Validate operator/route labels and evidence adequacy per question; prioritise cross-session joins, wrong-name cases, media and existing errata. Track reviewed versus automated explicitly.
2. Add generic neighbour/entity/date candidate expansion; compare fixed-budget retrieval to this baseline without gold-aware ranking.
3. Implement a separate offline prototype of supported relation/date/list/count operators, returning cited answers or explicit unresolved status. No gold-driven runtime patterns.
4. Measure retrieval success, grounded-answer correctness and abstention separately. Use independent synthetic/paraphrased cases and another corpus for generalisation.
5. Select optional semantic models only after candidate recall and ranking failure analysis; kernel-only cost checks, no automatic model download or broad evaluation migration.

The canonical ingestion schema and runtime changes remain subject to the accepted [enrichment policies](INGESTION_CONTEXT_ENRICHMENT.md) and [background plan](BACKGROUND_INGESTION_PLAN.md). This offline audit does not complete BI implementation or migrate the evaluation harness.
