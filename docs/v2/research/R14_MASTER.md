# R14 master problem list (2026-10-14)

Package R14-SYN. Tag `R14` = new problem; `R14-confirmed` = strengthens an existing gap (named). Source of every item: the five R14 inputs (`OBSERVATIONS_GAP_ANALYSIS.md`, `R14_RETRIEVAL_PROBLEMS.md`, `R14_READER_JUDGE_PROBLEMS.md`, `R14_LITERATURE_SURVEY.md`, `R14_ENGINE_SURVEY.md`) clustered into 21 benchmark-agnostic problems. Data: `docs/v2/data/r14_master.json`. No dataset text appears here: question ids and counts only; numbers are measured or hand-read on the reference full run `clean-jina-full` (1,540 questions, 1,253 correct, 81.4%) and labelled as such. Effects are hypotheses until a screen reports.

Screen ids S79-S100 are proposals and are **not queued** (OBS-GAPS owns the provisional S68-S78; `docs/v2/SCREENS.md` and `evals/v2_queue.tsv` end at S67). Slices: `fail_list`, `fail_retrieval`, `fail_fusion`, `fail_temporal`, `fail_reader`, `fail_mix_120`, cat5 (`sample_slice_loc_cat5`).

## Honest-headline note (judge-error band)

A hand read of all 287 WRONG rows and 271 CORRECT rows (201 rule-flagged, 70 random) finds judge errors in both directions: false negatives central 16.6 (clear 10, likely 4, borderline 9; range 10-23) and false positives central about 29.9 (clear 17, likely 12, borderline 5; range 17-45 including 3 unseen). Net: the **81.4% raw headline becomes about 80.5% (range 79.1-81.7)**; excluding 26 errata rows it is 82.6% raw (about 81.7% adjusted). The shift (-0.9 central) is inside the bootstrap CI (+-2.2) but is concentrated in multi-hop: **66.7% raw becomes about 61.1% central**, while cat2 and cat4 barely move (83.2% to 82.9%, 90.0% to 90.7%). The judge is nearly deterministic (4 flips in 1,696 identical-answer groups across 6 runs), so the errors are systematic conventions, not noise; repeating runs does not remove them. Rule: report raw, convention-adjusted and strict-list numbers per category with CIs, and treat multi-hop deltas under 5 points as uninterpretable on the raw column (M-17, M-18). Sources: R14-JUDGE-1..5.

## Master list (summary)

| id | tag | layer | problem | LoCoMo evidence (q) | prio | effort |
|---|---|---|---|---|---|---|
| R14-M-1 | R14-confirmed (confirms B1, I4, I28, I56, R01) | retrieval | Multi-evidence recall: fixed depth, no coverage objective, weak aggregation trigger | 55 | P1 | S |
| R14-M-2 | R14-confirmed (confirms B8, SOL-EPISODE, SOL-GRAPH) | retrieval | Cross-session spread: turn-level, session-agnostic retrieval; summaries and episodes as legs | 43 | P2 | M |
| R14-M-3 | R14-confirmed (confirms B7, B9, B10, I75, I78, R02) | retrieval | Fusion and rerank cut loss: ranked gold discarded when legs are fused into a small pool | 68 | P1 | S |
| R14-M-4 | R14-confirmed (confirms B2, B5, B6, B15) | retrieval | Lexical-zero and paraphrase gold: surface form is the strongest predictor of a miss | 81 | P2 | M |
| R14-M-5 | R14-confirmed (confirms A07, I6, B12) | ingest | Elliptical and context-dependent turns: decontextualisation and an index view | 32 | P2 | M |
| R14-M-6 | R14-confirmed (confirms B3, C3, C4) | retrieval | Indirect gold for judgement questions: evidence describes behaviour, never the question's words | 28 | P2 | M |
| R14-M-7 | R14-confirmed (confirms B4, I8) | retrieval | Explicit-date questions: the date is treated as ordinary words, no session-date prior at fusion | 13 | P1 | S |
| R14-M-8 | R14-confirmed (confirms C5, I57, I76, I79, E05) | reader | Date and duration arithmetic done by the 9B reader; event dates lack precision and status | 19 | P1 | S |
| R14-M-9 | R14-confirmed (confirms C3, C4, C10, I1, I61) | reader | Over-refusal and premise denial: no typed commit route for judgement questions | 40 | P1 | S |
| R14-M-10 | R14-confirmed (confirms C1, C2, C9, I6, R2-4) | reader | Wrong detail and distractor use; one window size for every question shape | 59 | P1 | M |
| R14-M-11 | R14-confirmed (confirms C6, I56) | reader | List and count completeness: partial lists, over-counted events, 'at least N' | 25 | P1 | S |
| R14-M-12 | R14 | reader | Multi-turn join and multi-entity decomposition at read | 54 | P2 | M |
| R14-M-13 | R14-confirmed (confirms SOL-VERIFY, defect_diagnosis) | reader | Ungrounded answers: new content words appear nowhere in the context | 37 | P2 | S |
| R14-M-14 | R14-confirmed (confirms READER_GAPS_FORENSIC 0.4, SOL-CONTRACT) | reader | Verbosity against a short gold: answer length follows difficulty, the lever is an output contract | 49 | P2 | S |
| R14-M-15 | R14-confirmed (confirms C7, E04) | reader | Image captions: misattributed or photo-only evidence | 6 | P3 | S |
| R14-M-16 | R14 | eval | Speaker attribution: wrong-speaker answers are rare, but swapped-speaker golds penalise correct attribution | 39 | P3 | S |
| R14-M-17 | R14-confirmed (confirms A2, I58, I77) | judge | Judge false negatives after the date guard: superset lists, annotation equivalence, containment | 23 | P1 | S |
| R14-M-18 | R14-confirmed (confirms A1, A3, A10, A13) | judge | Judge false positives and the honest headline: partial lists credited, 81.4 is not 81.4 | 34 | P1 | S |
| R14-M-19 | R14-confirmed (confirms A5, A12, I24, I62, A16) | eval | Gold errata, sibling evidence, id hygiene and retrieval metrics that overstate success | 21 | P2 | S |
| R14-M-20 | R14-confirmed (confirms G-W4, S34 S42 S46) | ingest | Profile and attribute state with history; knowledge update; destructive edits | 33 | P2 | M |
| R14-M-21 | R14-confirmed (confirms I54 (kin resolution), SOL-GRAPH) | ingest | Entity aliasing and link tables: seed-to-neighbour expansion without per-session LLM graph builds | 9 | P3 | M |

## R14-M-1 Multi-evidence recall: fixed depth, no coverage objective, weak aggregation trigger

**Tag:** R14-confirmed (confirms B1, I4, I28, I56, R01) | **Layer:** retrieval | **Priority:** P1 | **Effort:** S

### Problem statement

Questions that need three or more independent evidence items (list, count, 'all', 'both') get a fixed top-k anchor set. The chance that every needed item reaches the reader falls steeply with the number of items, and the trigger that deepens retrieval for such questions fires on fewer than half of them.

Evidence (55 questions; measured (RET-1, RET-11) on the reference full run; trigger precision/recall is a CPU computation over the 1,540 rows): 55 aggregation retrieval losses (39 exclusive primary pattern); 37 of them not in list mode; all-gold-in-context 95.9% at one gold turn falls to 8.7% as n_gold grows; list mode fires on 39% of n_gold>=3 questions. 55 question ids are listed in `r14_master.json`. Source items: R14-RET-1, R14-RET-11, R14-RET-10, R14-LIT-P05, R14-qry-2, R14-hop-1, OBS-10.

### Gaps (vs existing GAP_REGISTER ids)

B1 (aggregation retrieval) and I4 (list-mode trigger is an English regex): R14 adds the measured n_gold curve and the 37 missed-trigger questions. The 0-call trigger and the coverage objective are not built.

Existing parts: read.list_mode; read.query_contract; read.pool_protect_per_leg; read.agentic_mode=slot (net -3 at about 8x cost); SOL-LISTMODE; SOL-AGENTIC.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Trivedi2023-ircot], [Press2023-self-ask], [Jeong2024-adaptive-rag], [Zhou2023-least-to-most].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mnemon ALL-need paging; EverOS sufficiency; Hindsight link expansion.

### Hypotheses

A trigger that combines question shape with the leg score profile fires on at least 70% of n_gold>=3 questions at no more than 15% false fire on single-gold ones; list-mode depth plus entity/session-diverse selection lifts all-gold-in-context for n_gold>=3 from 34% (non-list) to 50% or more, recovering 10-20 of 55 questions (about 0.6-1.3 points) without the cost of the slot loop.

Measured by: fail_list (+ 60 matched n_gold>=3 correct as guard); screen S79 trigger + coverage (CPU first, offline over forensics); S80 second-retrieval bounded. Expected effect (hypothesis): 10-20 of 55 recovered (hypothesis); reader ceiling on cat1 given all gold is 75.5%, so recovery is capped at about 0.75 of the retrieval gain.

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Score-profile aggregation trigger | Decide list-depth from question shape AND the retrieval score profile (flat top scores, many same-entity hits); no model call. | ours (RET-11); Adaptive-RAG routing idea [Jeong2024-adaptive-rag] | 0 | false fire on single-gold questions adds distractors | `read.list_mode_trigger_v2 (new)` |
| Coverage-aware selection | MMR-style selection with entity/session diversity so near-duplicates stop crowding the anchors. | ours (RET-1, RET-10); BEIR diversity baseline | 0 | diversity can drop a true duplicate gold turn | `read.coverage_select (new)` |
| Missing-slot second retrieval | One bounded sufficiency check that names the missing slot, then exactly one targeted re-search (not the open slot loop). | IRCoT / Self-Ask [Trivedi2023-ircot] [Press2023-self-ask]; Mnemon ALL-need paging (R14-qry-2); EverOS sufficiency check (R14-hop-1) | 1-2 calls on about 12-18% of questions | slot loop already measured net -3 at 8x cost; keep the bound | `read.agentic_mode=slot / read.deep_v2 (existing)` |

## R14-M-2 Cross-session spread: turn-level, session-agnostic retrieval; summaries and episodes as legs

**Tag:** R14-confirmed (confirms B8, SOL-EPISODE, SOL-GRAPH) | **Layer:** retrieval | **Priority:** P2 | **Effort:** M

### Problem statement

Evidence for one question is spread over several sessions while retrieval is turn-level and session-agnostic; nothing asks whether each session that mentions the entity or topic is represented. Higher-grain units (session summary, episode, dated observation log) are the usual remedy, but the unit returned to the reader matters more than the quality of the summary text.

Evidence (43 questions; measured (RET-8, READ multi-turn table)): 43 questions with gold in three or more sessions; all-gold rate 35.8% at 3 sessions and 15.5% at 4+; in-context accuracy 72.9% at two sessions, 62.5% at three. 43 question ids are listed in `r14_master.json`. Source items: R14-RET-8, R14-LIT-P06, R14-epi-1, R14-epi-2, R14-epi-3, OBS-07.

### Gaps (vs existing GAP_REGISTER ids)

B8 (sessions with no hit). R14 sharpens it to a per-session coverage objective and adds three engine ideas: render the parent summary next to each fact (MemOS background, Memori), return the parent unit with facts as keys (EverOS, Supermemory), and a dated always-in-context observation log without retrieval (Mastra OM).

Existing parts: read.episode_leg_v2; read.session_leg; offline.session_close_v2; write.window_extract=all; S31 S33 S61 S64 (queued).

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Sarthi2024-raptor], [Edge2024-local-global], [Lee2024-readagent], [Pan2025-memory-construction-retrieval].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Nemori episodes; EverOS MemCell; Mastra OM; MemOS background; Honcho summaries.

### Hypotheses

A session-level leg followed by one best turn per matched session lifts all-gold coverage for n_sessions>=3 from 26% to 45% or more; rendering the parent summary line next to a hit adds context for 'which event' questions at under 200 tokens.

Measured by: fail_retrieval (b+cf) and fail_mix_120; metric distinct gold sessions represented; screen S81 session_cover; S31/S33/S61 stay as queued. Expected effect (hypothesis): 43 questions in scope; expect 6-10 recovered if session coverage reaches 45% (hypothesis); overlaps M-1.

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Per-session best turn after a session leg | Session digest leg picks sessions, then one best turn per matched session joins the anchors. | ours (RET-8); RAPTOR / ReadAgent / SeCom idea [Sarthi2024-raptor] [Lee2024-readagent] | 0 at read; digest calls at session close (already built) | digest hallucination; S33 fired zero calls so its inputs must be checked | `read.session_cover (new), read.episode_leg_v2` |
| Parent summary beside the hit | Print the one-line parent episode/summary under each retrieved fact. | MemOS background field, Memori summary attach (R14-epi-1) | 0 at read | adds tokens; worse on single-hop | `read.parent_line (new)` |
| Dated observation log, no retrieval | Background observer writes dated, prioritised observations that replace raw history; reader gets the log. | Mastra Observational Memory (R14-epi-3) | 1 call per token threshold | lossy; large design change | `offline.observation_log (new, GC arm)` |
| Summary degeneracy guard | Reject looping summaries (<35% distinct 4-grams) and keep the previous one. | Honcho (R14-epi-2) | 0 | none | `offline.summary_guard (new, unit test)` |

## R14-M-3 Fusion and rerank cut loss: ranked gold discarded when legs are fused into a small pool

**Tag:** R14-confirmed (confirms B7, B9, B10, I75, I78, R02) | **Layer:** retrieval | **Priority:** P1 | **Effort:** S

### Problem statement

Gold turns that one retrieval leg ranks well are dropped when the legs are fused into a small candidate pool (especially lexical-only or vector-only evidence that no other leg corroborates), and some survivors are dropped by a fixed keep-line while the token budget is half empty.

Evidence (68 questions; measured (RET-4, RET-7, POS-2) from logged leg ranks to depth 60): 68 stage-b/cf fusion-cut questions plus 4 rerank-keep cuts; 11 of 15 top-10 cuts never entered the 20-pool; in-context rate 95.8% at best leg rank 4-10 falls to 67.9% at 21-30; 105 of 115 retrieval failures use under 3,000 of 4,096 tokens; 84 gold turns cut at fusion, 11 at rerank. 97 question ids are listed in `r14_master.json`. Source items: R14-RET-4, R14-RET-7, R14-POS-2, R14-LIT-P09.

### Gaps (vs existing GAP_REGISTER ids)

B7/B9/B10/I75/I78 are confirmed: protection stops at rank 3 per leg, fusion is rank-based, no fusion cut is logged. R14 contributes the rank-band table (best leg rank vs survival) that sizes a deeper protection value offline, and the convex-combination-versus-RRF question.

Existing parts: read.pool_protect_per_leg=3; read.rerank_v2_search_pool (S67); read.rerank_keep; read.exact_budget; SOL-FAMFUS; SOL-POOLPROT; SOL-RRPOOL; SOL-RERANKKEEP.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Cormack2009-rrf], [Bruch2023-fusion-functions], [Thakur2021-beir], [Khattab2020-colbert].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Hindsight proximity rerank.

### Hypotheses

Protecting the top 8 per leg, fusing with score-normalised weights and letting the keep-line fill the token budget (about 15 anchors) keeps most of the 15 top-10 cuts and part of the 11-30 band, recovering 8-20 of 72 questions at zero model cost. Ceiling if every gold within depth 30 is kept: 20 questions.

Measured by: fail_fusion (+95 lucky/barely cases as guard); screen offline CPU re-fusion from forensics first (no run); S82 protect8 + convex fusion; S83 budget keep; S67 queued. Expected effect (hypothesis): 8-20 of 72 recovered (hypothesis, upper bound about 20 within depth 30).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Deeper per-leg protection | Raise pool_protect_per_leg from 3 to 8 so BM25-top-10 or vector-top-10 gold survives fusion. | ours (RET-4); family-level protected pool | 0 (rerank time grows with pool) | pool noise; rerank latency | `read.pool_protect_per_leg=8 (existing config)` |
| Score-aware fusion | Convex combination of normalised scores instead of RRF k=60, calibrated on logged ranks. | Bruch et al. [Bruch2023-fusion-functions]; RRF [Cormack2009-rrf] | 0 | weights overfit to the dev conversations | `read.fusion_convex (new)` |
| Budget-driven keep | Keep anchors until the token budget is filled (keep 15 or budget-driven) instead of a fixed 10. | ours (RET-7) | 0 | more distractors (see M-10) | `read.rerank_keep=15 / read.exact_budget (existing)` |
| Wider rerank pool | Rerank 30-40 candidates, already queued as S67. | ours (R3-07b) | GPU/CPU reranker time | latency | `read.rerank_v2_search_pool (existing, S67)` |

## R14-M-4 Lexical-zero and paraphrase gold: surface form is the strongest predictor of a miss

**Tag:** R14-confirmed (confirms B2, B5, B6, B15) | **Layer:** retrieval | **Priority:** P2 | **Effort:** M

### Problem statement

Gold turns that share almost no content words with the question (paraphrase, synonym, category-versus-instance wording) are missed by both lexical and dense legs. Neither the stored record nor the query is enriched with the vocabulary of the other.

Evidence (81 questions; measured (RET-5, OBS-measurements lexical predicate)): 81 questions with gold turns of near-zero word overlap; word overlap is the strongest single predictor of a gold turn being found (embedding correlation about -0.48); 72 combined retrieval-stage questions are zero-overlap or BM25-miss with a vector hit. 81 question ids are listed in `r14_master.json`. Source items: R14-RET-5, R14-LIT-P04, R14-qry-1, OBS-08.

### Gaps (vs existing GAP_REGISTER ids)

B2/B5/B6/B15 confirmed. R14 adds the engine finding that most engines expand at write time (keys per fact), not at query time, and the literature view that query-time HyDE is net-neutral when a strong hybrid plus reranker is in place.

Existing parts: read.word_vector_leg; read.lexical_analyzer=english; read.planner=llm; read.hyde_leg (proposed only); SOL-LEXICAL; SOL-GAP-HYDE.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Gao2022-hyde], [Wang2023-query2doc], [Zheng2023-step-back], [Rackauckas2024-rag-fusion], [Ma2023-rewrite-retrieve-read], [Chen2024-bge-m3].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mnemon planner; EverOS keyword+question+HyDE; MIRIX topic BM25.

### Hypotheses

Document-side enrichment (generated question or instance-to-category terms per fact, filtered) lifts the no-leg answer-bearing gold turns; expect 6-8 of the 21 paraphrase questions. Query-side multi-query helps less than document expansion on this pipeline.

Measured by: fail_retrieval (81 ids, 21 paraphrase); screen S84 doc expansion; S76 paraphrase probes (OBS-GAPS); word_vector_leg on the 21 ids. Expected effect (hypothesis): 6-8 of 21 paraphrase questions (hypothesis); small share of the rest.

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Document expansion at write | Per fact, generate 2-3 questions it answers plus category words; filter with Doc2Query-minus; index with the fact. | Doc2Query / Doc2Query-minus; EverOS facts-as-keys (R14-decon-1) | one batched call per fact set at ingest | noise terms; index grows | `write.doc_expansion (new)` |
| Record-styled multi-search plus HyDE note | Rewrite the question in the store's own wording (dated third-person) and add one invented answering note. | Mnemon planner, EverOS trio (R14-qry-1); HyDE [Gao2022-hyde]; Query2doc [Wang2023-query2doc] | 1 call per question | negative on strong hybrid; keep behind a guard slice | `read.planner=llm v3, read.hyde_leg (proposed), read.paraphrase_probes (S76)` |
| Learned sparse or late interaction leg | SPLADE / ColBERT / BGE-M3 multi-functional leg alongside BM25 and dense. | [Khattab2020-colbert] [Chen2024-bge-m3] | model inference at index and query | memory and latency | `read.word_vector_leg (existing, 21 ids screen)` |

## R14-M-5 Elliptical and context-dependent turns: decontextualisation and an index view

**Tag:** R14-confirmed (confirms A07, I6, B12) | **Layer:** ingest | **Priority:** P2 | **Effort:** M

### Problem statement

Gold turns that are elliptical or anaphoric (a reply that carries the answer but not the topic) are unfindable because the topic lives in the neighbouring turn; a +-2 window around anchors reaches them only if the topical neighbour was itself found. Pronoun and plural referents are lost the same way.

Evidence (32 questions; measured/heuristic (RET-6 overlap rule; OBS-03, OBS-09 measurements)): 32 context-dependent-gold questions (10 exclusive); 19 retrieval losses in sessions with a short-form window; elliptical turns about 10-15% of turns by the overlap rule. 32 question ids are listed in `r14_master.json`. Source items: R14-RET-6, R14-LIT-P01, R14-decon-1, R14-decon-3, R14-seg-1, OBS-02, OBS-03, OBS-09.

### Gaps (vs existing GAP_REGISTER ids)

A07/I6/B12 confirmed. OBS-GAPS already proposes write.index_view (S69) and write.restate_gate (S70); R14 adds exchange-pair records as a CPU-only alternative and the engine finding that window-fact arms lose on their own (S03 -4, S04 -6) because the returned unit matters.

Existing parts: write.window_extract=facts; write.prompt_v2 CONTEXT lines; read.neighbours_v2; S03 S04 S43-S45 S62 (queued); SOL-BUNDLES.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Choi2021-decontextualization], [Chen2023-dense-x], [Anthropic2024-contextual-retr], [Gunther2024-late-chunking], [Zhong2024-mix-of-granularity].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): EverOS episode as unit; Supermemory chunk attach; Mem0 15-80 word facts; MemOS background.

### Hypotheses

Indexing exchange-level records (turn plus the turn it answers) as an extra leg, or a rule-built context prefix on the embedded text, recovers 8-10 of the 32 questions with no model call; LLM restatement only for rule-flagged turns adds 3-5 more.

Measured by: fail_retrieval + fail_context (OBS-GAPS); screen S85 exchange_records (CPU simulation first); S69/S70/S75 per OBS-GAPS. Expected effect (hypothesis): 8-10 of 32 (hypothesis) for the rule-only forms.

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Exchange-pair records | Index each turn together with its addressed turn as an additional leg; return the pair. | ours (RET-6); Mix-of-Granularity [Zhong2024-mix-of-granularity] | 0; index 2-3x | index size | `write.exchange_records (new)` |
| Rule-built context prefix on embedded text | Prepend speaker, session date and subject of the previous turn to the embedded text; content stays byte-identical. | Anthropic contextual retrieval [Anthropic2024-contextual-retr]; OBS-02 index view | 0 | dilutes embedding if prefix too long | `write.index_view (S69, OBS-GAPS)` |
| Gated restatement of flagged turns | Rule score marks incomplete turns; one grounded call restates only those; grounding check drops invented words. | Decontextualisation [Choi2021-decontextualization]; Dense-X [Chen2023-dense-x]; Supermemory chunk attach | 1 call per flagged window (10-15% of turns) | S03/S04 lost: facts alone regress | `write.restate_gate (S70, OBS-GAPS)` |
| Day/session-first window boundary | Cut windows at session or calendar-day change first. | EverOS boundary order (R14-seg-1) | 0 | none | `write.window_overlap_turns (S75, OBS-GAPS)` |

## R14-M-6 Indirect gold for judgement questions: evidence describes behaviour, never the question's words

**Tag:** R14-confirmed (confirms B3, C3, C4) | **Layer:** retrieval | **Priority:** P2 | **Effort:** M

### Problem statement

Open-domain (hypothetical or trait) questions ask for a judgement while the supporting turns describe behaviour; single-shot retrieval on the question text finds neither. The reader ceiling given all gold is also low.

Evidence (28 questions; measured (RET-2; OBS-measurements)): 28 open-domain retrieval losses with 47.6% of their gold turns missing; reader accuracy given all gold 46.9% on cat3 (96 questions); 41 open-domain wrong answers are not refusals. 28 question ids are listed in `r14_master.json`. Source items: R14-RET-2, OBS-06, OBS-08.

### Gaps (vs existing GAP_REGISTER ids)

B3 confirmed; the class open_domain_inference_retrieval has no built primary solution in the solution map. OBS-06 proposes inference probes (S74).

Existing parts: read.planner=llm; SOL-PLANNER; SOL-GAP-HYDE; SOL-AGENTIC; SOL-GRAPH; SOL-EPISODE.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Zheng2023-step-back], [Gao2022-hyde].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mnemon need-plan; Honcho peer card.

### Hypotheses

Rewriting the question into 3-4 evidence-kind sub-queries (what X chose, enjoys, said about A, B) recovers about half the missing gold (14 of 28), but the 47% reader ceiling caps the accuracy gain at 4-7 questions.

Measured by: fail_retrieval (28) + all 96 cat3; oracle-gold reader-only run first to size the ceiling; screen S86 evidence_kind_probes; shares S74 probes (OBS-06). Expected effect (hypothesis): 4-7 questions net (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Evidence-kind query decomposition | Planner emits sub-queries for the kinds of evidence that would settle the judgement. | ours (RET-2); Step-back [Zheng2023-step-back]; Mnemon need-plan | 1 call per open-domain-shaped question | reader ceiling | `read.planner=llm v3 / read.evidence_kind_probes (new)` |
| Trait and preference synthesis at write | Sleep-time profile/trait facts that give judgement questions a direct hit. | ours (profile_card_v2); Honcho peer card, Memobase profile | sleep-time calls | unread card today (S46 skipped) | `offline.profile_card_v2 + read.profile_header_packing` |

## R14-M-7 Explicit-date questions: the date is treated as ordinary words, no session-date prior at fusion

**Tag:** R14-confirmed (confirms B4, I8) | **Layer:** retrieval | **Priority:** P1 | **Effort:** S

### Problem statement

A question that contains an explicit calendar date points at the session of that date, yet the engine treats the date as ordinary words; the gold turn is found by one leg and lost in fusion. The temporal leg fires on few questions and is off in the reference.

Evidence (13 questions; measured (RET-3)): 13 failing of 180 explicit-date questions (167 pass as guard); 12 of 13 are fusion cuts with the gold session 0-12 days from the query date; temporal leg fires on 187 of 1,540. 13 question ids are listed in `r14_master.json`. Source items: R14-RET-3, R14-LIT-P03, R14-time-2, R14-time-3, OBS-11.

### Gaps (vs existing GAP_REGISTER ids)

B4/I8 sharpened: the loss is concentrated in explicit-date questions, not relative ones. Engine survey adds window overlap plus proximity boost inside the window instead of a hard span filter (Hindsight) and reference_time on every read (R14-time-2).

Existing parts: read.temporal_relative; read.temporal_infer_year; read.lexical_dates; SOL-TEMPLEG; SOL-RELANCHOR; S24 S62 S63.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Gade2024-tempralm], [Strotgen2010-heideltime], [Allen1983-interval-algebra].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Hindsight temporal arm; Cognee TEMPORAL; Mastra three dates.

### Hypotheses

Boosting (not filtering) candidates whose session date is within a few days of the question date recovers 9-11 of 13 questions with no loss on the 167 passing ones.

Measured by: fail_temporal (13 fail + matched controls from the 180); screen S87 date_prior (CPU re-fusion first); S24/S62/S63 queued. Expected effect (hypothesis): 9-11 of 13 recovered (hypothesis; about 0.6-0.7 points).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Date-scoped retrieval prior | Soft boost at fusion for candidates in sessions within N days of a date in the question. | ours (RET-3); Hindsight window overlap plus proximity (R14-time-3); TempRALM [Gade2024-tempralm] | 0 | boost on the wrong year; keep soft | `read.date_prior (new)` |
| Explicit reference_time on every read | Pass the conversation date, never the wall clock, to relative-phrase resolution. | Hindsight (R14-time-2); MIRIX wall-clock defect | 0 | none | `read.temporal_relative (existing) + unit test` |
| LLM interval extraction for unsure phrases | Rule first; one call only when the rule cannot parse. | Cognee TEMPORAL (R14-time-3) | 0-1 call on unparsed phrases | latency | `read.temporal_infer_year (existing)` |

## R14-M-8 Date and duration arithmetic done by the 9B reader; event dates lack precision and status

**Tag:** R14-confirmed (confirms C5, I57, I76, I79, E05) | **Layer:** reader | **Priority:** P1 | **Effort:** S

### Problem statement

Spans between two events, 'for N years' start dates and 'N months now' are answered by the reader's own subtraction; the wrong event line is picked for a 'when' question; a stored date does not say how precise it is or whether the state is still running.

Evidence (19 questions; measured (READ-4; OBS-11 predicates)): 19 date/duration reader failures; 15 how-long failures remain although every endpoint is annotated; 38 how_long/how_many-unit questions in scope; 10 pure arithmetic plus 8 wrong-event classes. 19 question ids are listed in `r14_master.json`. Source items: R14-READ-4, R14-LIT-P03, R14-time-1, R14-decon-2, OBS-11.

### Gaps (vs existing GAP_REGISTER ids)

C5/I76/E05: R14 confirms the class is the best covered on paper and the least screened. Engine survey adds precision, status and type tags on the temporal pass (Mem0 platform) and a three-date line (Mastra).

Existing parts: duration_solve (built, no screen); milestones E05; read.resolve_durations; date repair I57 (+1/0); SOL-DURATION; SOL-RELANCHOR (S62).

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Chen2022-pot], [Gao2023-pal], [Allen1983-interval-algebra], [Chen2021-timeqa].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mem0 platform temporal pass; Mastra OM dates; Hindsight occurred_start/end.

### Hypotheses

duration_solve plus milestones on the 38 how-long/unit questions converts at least a third of the 15 in-context failures (5 questions, about 0.3 points) with zero model calls beyond endpoint extraction.

Measured by: fail_temporal date_* classes (19) + 38 unit questions; screen S88 duration_solve + milestones (built-off, needs a screen only). Expected effect (hypothesis): about 5 questions (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Deterministic date calculator | Resolved event dates in, closed set of operations out (difference, ordering, N weeks before); code, not model. | PoT / PAL idea [Chen2022-pot] [Gao2023-pal]; ours duration_solve | 0 (+1 call on about 5% for endpoint extraction) | wrong endpoint pick | `duration_solve / read.resolve_durations (existing)` |
| Precision/status/type on event time | Add precision (day/month/year) and ongoing-or-completed to the temporal envelope. | Mem0 platform temporal pass (R14-time-1) | within the extraction call | schema change | `write.temporal_envelope (new, S62 arm)` |
| Fake-clock invariant test | Guard that every replayed line carries its own timestamp, not the run date. | Mem0 OSS / Memori / Nemori defect (R14-decon-2) | 0 | none | `unit test` |

## R14-M-9 Over-refusal and premise denial: no typed commit route for judgement questions

**Tag:** R14-confirmed (confirms C3, C4, C10, I1, I61) | **Layer:** reader | **Priority:** P1 | **Effort:** S

### Problem statement

The reader answers 'not mentioned' or denies the premise when the evidence supports a committed inference (hypothetical, 'likely', world-knowledge hop). Prompt-only yes/no rules misfire on other question shapes, and abstention policy has no shared test.

Evidence (40 questions; measured (READ-1; OBS-measurements)): 40 over-refusal/premise-denial rows (26 cat3 failures); 14 open-domain refusals; 37 ungrounded rows overlap. 40 question ids are listed in `r14_master.json`. Source items: R14-READ-1, R14-LIT-P08, R14-ans-1, R14-ans-2, R14-wk-1, OBS-06.

### Gaps (vs existing GAP_REGISTER ids)

C3 (yes/no rule rejected +5/-17) and C4 (screen pending) confirmed. R14 adds the gating principle: decide the route by code from the typed contract, not by prompt wording, plus the sufficiency-gated answer/infer/abstain literature and the shared-test finding that engines set abstention per engine.

Existing parts: --retry-refusal; reader variants dated_infer / grounded_generic_infer / dated_world / routed; read.query_contract + contract_rules; read.deep_v2 (S55 queued); SOL-READERPROMPT.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Joren2024-sufficient-context], [Yu2023-chain-of-note], [Yan2024-crag], [Wen2024-abstention-survey], [Kadavath2022-know-what-they-k].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): EverOS inference CoT; Mem0 proxy; MIRIX guess rule; Mnemon exact string.

### Hypotheses

Gating the commit route on the typed contract (yes/no, would/likely/might) keeps the C3 regression away and converts about one third of the 26 cat3 failures (+6 to +10 questions, 0.4-0.6 points) with no extra calls; a self-consistency vote on judgement shapes only (about 6% of questions) adds a few more.

Measured by: fail_reader (40 + 25 refusal rows); guards cat5 and OP-Bench irrelevance; screen S73 prompt, S74 probes (OBS-GAPS); S89 vote; S20 cat5 guard. Expected effect (hypothesis): +6 to +10 questions (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Typed commit route | Contract rule marks 'inference' shape; builder adds the commit-with-cited-line clause for that shape only; other shapes keep the unchanged text. | ours (OBS-06); Sufficient Context [Joren2024-sufficient-context]; Chain-of-Note [Yu2023-chain-of-note] | 0 | cat5 over-commit; OP-Bench sycophancy | `read.inference_mode (S73, OBS-GAPS)` |
| Sufficiency-gated answer/infer/abstain | Three-way policy using a sufficiency verdict before answering. | CRAG [Yan2024-crag]; Abstention survey [Wen2024-abstention-survey] | 0-1 call | same-model confound | `read.deep_v2 (S55 queued)` |
| Self-consistency vote on judgement shapes | Three samples, majority, on judgement questions only. | Self-consistency; ours (READ-1) | +2-4 calls on about 6% | cost | `read.vote_judgement (new)` |
| Empty-context abstention without a call | No context lines means abstain with no model call. | Mnemon / combined engine (R14-ans-2) | 0 (saves a call) | none | `read.empty_abstain (new, unit test)` |

## R14-M-10 Wrong detail and distractor use; one window size for every question shape

**Tag:** R14-confirmed (confirms C1, C2, C9, I6, R2-4) | **Layer:** reader | **Priority:** P1 | **Effort:** M

### Problem statement

The reader answers from a plausible sibling line: topical, near the gold line, but a different or generic detail. The distractor is a window neighbour of the gold in about a third of cases and a retrieval hit in over half. Context size is the same for every shape, so the window that helps temporal questions is paid for by single-hop distractors. Position effects (lost in the middle) do not reproduce at this context length.

Evidence (59 questions; measured/heuristic (READ-2, DIST-1, LEN-1, POS-1, POS-2); 13 hand-confirmed): 46 wrong_detail rows, 59 distractor-source rows (19 window neighbours, 32 retrieval hits); accuracy given all gold falls 91% (<=40 turns) to 83.6% (>60 turns); position quintiles 0.89-0.92 (Fisher p=0.90) so no position effect; rank>3 gold 74.8% vs 90.8%. 88 question ids are listed in `r14_master.json`. Source items: R14-READ-2, R14-DIST-1, R14-LEN-1, R14-POS-1, R14-POS-2, R14-prof-1.

### Gaps (vs existing GAP_REGISTER ids)

C1/C9/R2-4/I6 confirmed: reordering was rejected, verification not screened, owner/event check not built. R14 adds the negative position result (do not spend a screen), the per-shape window, and Mastra/MemOS style attached source context.

Existing parts: read.present_order (hits-first rejected R2-4); SOL-VERIFY / --verify-answer; SOL-BUNDLES S19/S52-S54; read.exact_budget; SOL-GAP-DISTRACTOR (not built).

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Yu2023-chain-of-note], [Liu2024-lost-middle], [Hsieh2024-found-in-middle], [Yoran2023-retrobust].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Supermemory source chunk attach; Mnemon keyed state block.

### Hypotheses

A binding check that the cited line shares the question's event noun phrase and date (code), plus one repair call on mismatches, removes about a third of true distractor cases (+10 to +14 questions); a per-shape window (1/1 for non-temporal single-hop) recovers about 1 point on single-hop at no loss on temporal.

Measured by: fail_reader (46 + 59 ids); 2x2 narrow/wide window by shape on the dev slice; screen S90 quote-then-answer; S91 window_by_shape; S92 verify_binding; S52-S54 queued. Expected effect (hypothesis): +10 to +14 (binding) and about +1 point single-hop (window), partly overlapping (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Provenance check (owner/event/date binding) | Code checks the cited line against the question's head noun phrase and date; one repair call on mismatch only. | ours (DIST-1); Chain-of-Note [Yu2023-chain-of-note] | +1 call on mismatches only | false mismatch cost | `read.verify_binding (new) / --verify-answer` |
| Quote-then-answer | Reader quotes the line first; the quote must contain the head noun phrase. | ours (READ-2) | longer completion | verbosity | `reader.quote_first (new, harness)` |
| Per-shape window and budget | Narrow window for single-fact shapes, wide for temporal and multi-turn. | ours (LEN-1); S52/S53 exact budget (queued) | 0 | temporal regression if misrouted | `read.window_by_shape (new)` |
| Evidence compression | Compress distractor-heavy context before reading. | RECOMP [Xu2023-recomp] idea | +1 call | loses the gold | `read.compress (existing idea, not built)` |

## R14-M-11 List and count completeness: partial lists, over-counted events, 'at least N'

**Tag:** R14-confirmed (confirms C6, I56) | **Layer:** reader | **Priority:** P1 | **Effort:** S

### Problem statement

The reader returns one or two of several items, counts one event twice because it is mentioned on two dates, or hedges with 'at least N'. Retrieval of list members is handled; enumeration and de-duplication at read are not.

Evidence (25 questions; measured (READ-3)): 25 incomplete-list/wrong-count rows; 16 list_incomplete + 9 count_wrong; 7 of 9 counts are over-counts. 25 question ids are listed in `r14_master.json`. Source items: R14-READ-3, R14-qry-2.

### Gaps (vs existing GAP_REGISTER ids)

C6/I56 confirmed; the two_call and evidence-table variants have never been screened. R14 adds code-side counting from the model's listed events (PoT idea) and the enumerate-then-count shape.

Existing parts: count_verify single (REJECTED +1/-16); two_call and evidence_table (unscreened); S06 S11 S56 (queued); SOL-COUNT; SOL-OBLIG.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Chen2022-pot].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mnemon ALL-need paging.

### Hypotheses

An evidence table (one row per distinct event: actor, object, date) built by code from the context lines, followed by a count over the rows, fixes the over-count class (7 of 9) without the single-call collapse failure.

Measured by: fail_list (25 + list/count-shaped controls); screen S93 evidence_table / two_call; S06, S11, S56 queued. Expected effect (hypothesis): about 5-7 questions (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Evidence table, enumerate then count | Code builds event rows; the model lists; a function counts and de-duplicates. | ours (READ-3); PoT idea [Chen2022-pot] | +1 call on list/count shapes (about 6%) | collapse to one item (seen in count_verify) | `harness evidence_table / two_call (existing)` |
| Obligation coverage for list/count | Evidence obligations name each required item before abstaining. | ours S56 | 0-2 calls | queued | `read.obligations (S56 queued)` |
| Contract-shaped output | List shape prints one bullet per item; count prints number plus list. | ours (READ-8) | 0 | none | `read.query_contract (S06/S11 queued)` |

## R14-M-12 Multi-turn join and multi-entity decomposition at read

**Tag:** R14 | **Layer:** reader | **Priority:** P2 | **Effort:** M

### Problem statement

Questions that need two or more turns, especially across sessions or about several entities (intersection, difference, 'both'), are not joined by the reader even when all evidence is in context. Retrieval-side hop work is plentiful; reader-side decomposition with per-entity quotas and deterministic combine operators is not covered.

Evidence (54 questions; measured (MH-1; OBS-10 predicate)): 54 multi-turn reader failures (52 multi-session of 140 in-context); 43 multi-entity wrong answers; accuracy 90.0% at one evidence turn, 78.5% at two, 68.2% at three, 72.9% across two sessions. 54 question ids are listed in `r14_master.json`. Source items: R14-MH-1, OBS-10, R14-LIT-P05.

### Gaps (vs existing GAP_REGISTER ids)

New: reader-side decomposition when all evidence is already in context. Nearest existing: OBS-10 read.decompose_v2 (S77) for the retrieval side; the retired slot loop is not to be repeated.

Existing parts: read.multi_intent_split (discourse markers only); read.bridge_hop (gated); read.agentic_mode; SOL-OBLIG (S56); SOL-DEEPREAD (S55); retired: slot loop, fact chain.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Zhou2023-least-to-most], [Khot2023-decomposed-prompting], [Press2023-self-ask].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Cognee DECOMPOSITION; MemMachine SplitQuery (+0.46 points at 2x tokens).

### Hypotheses

Decomposition into per-entity sub-questions answered per turn and joined by code lifts multi-session in-context accuracy from 72.9% by +5 points (about +7 questions) at +2-3 calls on about 10% of questions.

Measured by: fail_list + new fail_multi (43, OBS-GAPS); screen S94 reader-side join (after S77); S56 queued. Expected effect (hypothesis): about +7 questions (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Decompose then join | Sub-questions per entity answered per turn; combine operators (intersection, union, difference, count merge) in code with entity quotas. | Least-to-Most [Zhou2023-least-to-most]; Decomposed Prompting [Khot2023-decomposed-prompting]; OBS-10 | +2-3 calls on about 10% | slot-loop style regression | `read.decompose_v2 (S77, OBS-GAPS)` |
| Evidence obligations join | Join obligations stated before the answer. | ours S56 | 0-2 calls | queued | `read.obligations (S56 queued)` |
| Cross-session evidence table | One table across sessions, one call. | ours (MH-1) | +1 call | table noise | `harness evidence_table (existing)` |

## R14-M-13 Ungrounded answers: new content words appear nowhere in the context

**Tag:** R14-confirmed (confirms SOL-VERIFY, defect_diagnosis) | **Layer:** reader | **Priority:** P2 | **Effort:** S

### Problem statement

The answer contains content words that occur in no context line (inference output, arithmetic result, world knowledge or fabrication). The diagnosed verification mode already flags such cases but is unscreened.

Evidence (37 questions; measured (READ-7)): 37 ungrounded wrong rows (22% of in-context wrong rows); 188 correct answers are also 'ungrounded' (false-defect risk). 37 question ids are listed in `r14_master.json`. Source items: R14-READ-7, R14-ans-1.

### Gaps (vs existing GAP_REGISTER ids)

Confirmed: diagnosed mode flags 'a name no memory line contains' but has no screen. R14 adds the false-defect rate requirement.

Existing parts: --verify-answer; --verify-slots diagnosed; defect_diagnosis.py; SOL-VERIFY.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Yu2023-chain-of-note].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): EverOS trimmed CoT.

### Hypotheses

slot_verify diagnosed repairs 1 in 4 of the 37 (+8 questions) at about 1.8% extra calls; without a gold, abstaining is the safe alternative on cat1-4.

Measured by: fail_reader (37 ids); false-defect rate on the 188 correct; screen S95 slot_verify diagnosed. Expected effect (hypothesis): about +8 (hypothesis, upper).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Diagnosed repair | One repair call only when a defect rule fires. | ours (READ-7) | +1 call on about 10% of answers | false defects on the 188 correct ones | `--verify-slots diagnosed (existing)` |
| Larger reader or thinking mode | Reader capacity. | COMBINED_ENGINE comparison (R14-ans-1) | high | thinking mode measured and not adopted (A9) | `reader model role` |

## R14-M-14 Verbosity against a short gold: answer length follows difficulty, the lever is an output contract

**Tag:** R14-confirmed (confirms READER_GAPS_FORENSIC 0.4, SOL-CONTRACT) | **Layer:** reader | **Priority:** P2 | **Effort:** S

### Problem statement

Wrong answers are enumerations or explanations many times the gold length. The length is a difficulty correlate (long answers follow refusals, lists and uncertain counts) so a length cap is not a fix; the lever is an answer-type contract that fixes the output shape per question type.

Evidence (49 questions; measured (READ-8)): 49 rows with answer/gold length ratio above 8. 49 question ids are listed in `r14_master.json`. Source items: R14-READ-8.

### Gaps (vs existing GAP_REGISTER ids)

Confirmed; no new screen needed beyond S06/S11 plus a report column.

Existing parts: read.query_contract + contract_rules; GROUNDED prompts (short, direct); judge_conventions list_superset; S06 S11 (queued).

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): none specific.
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): none specific.

### Hypotheses

Contract-fixed shapes (single fact one phrase; list bullet per item; count number plus list) lower the ratio>8 rate without lowering list recall.

Measured by: ratio>8 ids from fail_reader; screen S06, S11 (queued); add ratio>8 and list-recall columns. Expected effect (hypothesis): no direct gain; reduces judge partial-credit exposure (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Answer-type contract | Output shape fixed per typed question shape. | ours (READ-8) | 0 | list recall | `read.query_contract (existing, S06/S11)` |

## R14-M-15 Image captions: misattributed or photo-only evidence

**Tag:** R14-confirmed (confirms C7, E04) | **Layer:** reader | **Priority:** P3 | **Effort:** S

### Problem statement

The answer is in an image caption but the reader uses a caption from another turn, or the gold fact exists only in the photo. Captions rendered inline in speech make ownership unclear; ingest-time description records are the common engine pattern, not read-time fetch.

Evidence (6 questions; measured (READ-5; OBS-05)): 6 caption-misuse rows (3 cross-turn mixing); 28 caption-only questions; 55 caption-decisive in OBS-05. 6 question ids are listed in `r14_master.json`. Source items: R14-READ-5, R14-LIT-P10, R14-mm-1, OBS-05.

### Gaps (vs existing GAP_REGISTER ids)

C7 (caption rule 85.8 vs 86.7, not adopted) confirmed. R14 adds the engine finding that Mem0, Hindsight, MemOS and EverOS describe images at ingest.

Existing parts: ingest.assets; read.asset_evidence; reader grounded_v2/v3 caption line; SOL-ASSET.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Li2023-blip2], [Wang2025-mirix].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mem0 vision; Hindsight attachments; MemOS parsers.

### Hypotheses

Rendering each caption on its own typed line with turn id, speaker and date lowers cross-turn mixing (3 of 6); photo-only golds are not fixable here (+2 to +3 questions).

Measured by: fail_reader (6) + 28 caption-only; screen S72 (OBS-GAPS). Expected effect (hypothesis): +2 to +3 (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Caption as its own typed line | Own 'Photo (turn, speaker, date)' line next to the turn. | ours (READ-5); MIRIX/Mem0 description records | 0 | tokens | `write.caption_facts (S72, OBS-GAPS)` |
| Ingest-time image description | Vision call once at ingest, stored as a record with provenance. | Mem0 enable_vision, Hindsight attachments (R14-mm-1) | 1 vision call per image | latency at ingest | `ingest.assets (existing)` |

## R14-M-16 Speaker attribution: wrong-speaker answers are rare, but swapped-speaker golds penalise correct attribution

**Tag:** R14 | **Layer:** eval | **Priority:** P3 | **Effort:** S

### Problem statement

Attribution by speaker is the right behaviour, yet in a set of questions the gold turn is spoken by the other person, so a system that ignores the named speaker gains and one that enforces it loses. Restatement can also erase who said it (speaker labels discarded).

Evidence (39 questions; measured (READ-6, GOLD-2)): 1 reader-side wrong-speaker row; 39 speaker-swapped questions of which 23 are credited and not in the errata; 5 speaker candidates still to verify. 40 question ids are listed in `r14_master.json`. Source items: R14-READ-6, R14-GOLD-2, R14-decon-3.

### Gaps (vs existing GAP_REGISTER ids)

New: perspective features are scored against labels that sometimes contradict them; no stratum separates the 23 credited swapped-speaker rows.

Existing parts: SOL-PERSPECTIVE (subject_weight, in reference); I59 owner_check (tag only); I39 perspective layer.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): none specific.
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): SimpleMem speaker discard; MemOS per-speaker.

### Hypotheses

Reporting the swapped-speaker rows as their own stratum explains any perspective-feature loss up to 1.5 points and prevents rejecting a correct attribution feature; owner tags stay tags, not filters.

Measured by: perspective screens with the 23-row stratum split out; screen no new screen; apply to any perspective screen. Expected effect (hypothesis): reporting fix; at most 1 question reader-side (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Speaker-swap stratum in every report | Report the swapped rows separately. | ours (GOLD-2) | 0 | none | `report column (no flag)` |
| Per-speaker perspective ingestion | Keep speaker as subject/owner on restated facts. | MemOS per-speaker (R14-decon-3) | 0 | restatement erasing speaker (SimpleMem) | `write.restate_referents (S70b, OBS-GAPS)` |

## R14-M-17 Judge false negatives after the date guard: superset lists, annotation equivalence, containment

**Tag:** R14-confirmed (confirms A2, I58, I77) | **Layer:** judge | **Priority:** P1 | **Effort:** S

### Problem statement

WRONG verdicts whose answer states the gold fact: a superset or concluding list, an equivalent annotation, a typo, containment with extra detail, a country/city or paraphrase pair, and relative-phrase golds the date guard does not parse. The judge is nearly deterministic, so these errors are systematic, not random.

Evidence (23 questions; hand read of all 287 wrong rows (JUDGE-1, JUDGE-4, JUDGE-5)): 23 false-negative rows by hand (10 clear, 4 likely, 9 borderline); central 16.6 (range 10-23); conventions v1/v2 reach only 6 of 14 clear/likely; 20 unparsed relative-phrase golds; 1,696 identical-answer groups across 6 runs with 4 flips. 42 question ids are listed in `r14_master.json`. Source items: R14-JUDGE-1, R14-JUDGE-4, R14-JUDGE-5, R14-LIT-P11, R14-eval-1.

### Gaps (vs existing GAP_REGISTER ids)

A2/I58/I77 confirmed; v3 conventions are not built. R14 adds that the residue after the date guard is list/containment/annotation/geography.

Existing parts: judge_conventions v1/v2; --judge-date-check (89 deterministic credits); second_view.py; SOL-DATEGUARD; SOL-JUDGE-I77 (not built).

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Zheng2023-judging-llm-judge], [Wang2023-unfair-evaluators], [Liu2023-geval], [Verga2024-poll], [Thakur2024-judging-the-judges], [Kamalloo2023-eval-odqa], [Bulian2022-tomayto].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): EverOS lenient judge; Nemori reader=judge.

### Hypotheses

Conventions v3 (all gold items anywhere incl. a concluding list; annotation-equivalent year; containment with extra detail when no contradiction; edit-distance names; relative_year) credits 7 to 10 of the 14 clear/likely rows with 0 new false positives on the calibration set.

Measured by: 23 FN ids + 34 FP ids frozen as a 57-row calibration extension; judge_calibration_dev.jsonl; screen S96 judge conventions v3 (re-score stored answers, no retrieval). Expected effect (hypothesis): credits 7-10 rows (0.5-0.65 points) with no engine change (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Judge conventions v3 | Deterministic pre-judge rules, scored on the hand-labelled calibration file; require 0 new FP. | ours (JUDGE-1); string-match + LLM hybrid [Kamalloo2023-eval-odqa]; token-F1 normalisation [Bulian2022-tomayto] | 0 | FP on lenient list rule | `judge_conventions v3 (new, S96 re-score)` |
| Second judge on disagreement | Different-family judge only on rows where the first judge and the pre-judge disagree. | PoLL [Verga2024-poll] | about 230 calls per run | same-family bias | `--judge-second (existing second_view)` |

## R14-M-18 Judge false positives and the honest headline: partial lists credited, 81.4 is not 81.4

**Tag:** R14-confirmed (confirms A1, A3, A10, A13) | **Layer:** judge | **Priority:** P1 | **Effort:** S

### Problem statement

CORRECT verdicts for answers that omit required gold items, give a vague description instead of the named item or a coarse stand-in. The credit is a convention, applied unevenly, and it concentrates in multi-hop, which carries most claimed improvements. Reported scores across engines are not comparable (different judges, category sets, readers, budgets).

Evidence (34 questions; hand read with explicit weights (JUDGE-2, JUDGE-3)): 34 false-positive rows by hand (17 clear, 12 likely, 5 borderline), central about 30 (range 17-45 incl. 3 unseen); 201 rule-flagged plus 70 random correct rows read; multi-hop adjusted 66.7% to 61.1%. 34 question ids are listed in `r14_master.json`. Source items: R14-JUDGE-2, R14-JUDGE-3, R14-LIT-P12, R14-eval-1.

### Gaps (vs existing GAP_REGISTER ids)

A1/A3/A10/A13 confirmed. R14 gives the quantified band (see Honest headline) and the rule that per-category claims under 5 points use the strict column. Engine survey: the same engine moves 20-45 points between harnesses; MemSpine's 672-1,571 token context is the one axis on which a fair claim exists.

Existing parts: list_recall column (A3); accuracy_ci cluster bootstrap (A10); errata with both headlines (A5); A1 second judge deferred.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Maharana2024-locomo], [Wu2024-longmemeval], [Li2026-locomo-plus], [Verga2024-poll].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Zep 58-75 across harnesses; Mem0 77.7-92.5; Memori 41-87.

### Hypotheses

Reporting raw, convention-adjusted and strict-list numbers per category with CIs shows the multi-hop figure is overstated by about 5 points (66.7% to 61.1% central); a strict all-items rule would drop a further 12-17 questions; multi-hop deltas under 5 points are uninterpretable on the raw column.

Measured by: 34 FP + 23 FN ids as a frozen calibration extension; whole-run re-score; screen S97 triple-column re-score of stored answers (reference run plus last two full runs). Expected effect (hypothesis): no engine gain; changes what a gain means (hypothesis: central -0.9 points, range -2.3 to +0.4).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Triple reporting | Raw, convention-adjusted, strict-list headline per category with CIs, plus judge/category/reader/budget header. | ours (JUDGE-3); cross-engine table (R14-eval-1) | 0 | none | `report columns (no flag)` |
| Item-recall column and calibrated 'missing item' side | Calibrate the missing-item direction on the 34 FP rows. | ours (JUDGE-2) | 0 | none | `judge_conventions (new rows)` |
| Two-judge adjudication | Different-family judge on disputed rows (A1, deferred). | PoLL [Verga2024-poll]; LoCoMo-Plus [Li2026-locomo-plus] | about 350 calls | owner decision A1 | `--judge-second` |
| Token-efficiency table next to accuracy | Tokens per question beside each accuracy. | Cognee (R14-eval-1) | 0 | none | `report column` |

## R14-M-19 Gold errata, sibling evidence, id hygiene and retrieval metrics that overstate success

**Tag:** R14-confirmed (confirms A5, A12, I24, I62, A16) | **Layer:** eval | **Priority:** P2 | **Effort:** S

### Problem statement

Gold and question defects (gold not entailed by its evidence turn, premise differing from the transcript), sibling turns that also answer a single-answer question, evidence ids lost by the parser, and correct answers that lack gold in context. Gold-id recall is treated as necessary and sufficient when it is neither.

Evidence (21 questions; hand + measured (GOLD-1, DIST-2, RET-12, RET-9, RET-10)): 8 gold/premise/speaker candidates not in errata; 3 sibling-evidence rows of 18 hand-checked (about 7-10 of 59 extrapolated, low confidence); 10 ids with malformed evidence (11 gold ids dropped); 95 correct answers lack gold in context (28 have the answer string elsewhere), 153 barely in; conversations 3 and 5 have the highest loss rates. 112 question ids are listed in `r14_master.json`. Source items: R14-GOLD-1, R14-DIST-2, R14-RET-12, R14-RET-9, R14-RET-10, R14-eval-2.

### Gaps (vs existing GAP_REGISTER ids)

A5/I62/A12/I24 confirmed. New: accept-set annotation for sibling evidence; answer-support recall column; strict evidence parser with an existence check. Estimated unread gold errors (16-24) were not found: the errata is close to complete for the wrong side.

Existing parts: locomo_errata.json (52 entries); evidence_label_error tag; A12 sufficiency_on_qa_set; A16 noise floor.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Maharana2024-locomo].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Cognee 143 excluded; LoCoMo-Refined 1,382 revised.

### Hypotheses

Adding the errata extension changes the excluded accuracy by under 0.2 points; accept-set re-judging of sibling rows lowers the true reader error rate by about 0.5 points; answer-support recall removes about 28 of the 95 'lucky' labels as a cleaner retrieval target.

Measured by: the 10 + 8 + 3 + 59 ids; guard: correct questions with context-dependent gold; screen S98 support-recall report (CPU, no run); unit tests for the parser and errata. Expected effect (hypothesis): measurement honesty; no accuracy gain (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Errata extension and accept set | Add the 8 entries (after verifying 5 speaker candidates) and a multi-gold accept set for sibling evidence. | ours (GOLD-1, DIST-2); LoCoMo-Refined idea (R14-eval-2) | 0 (about 1 hour hand adjudication) | judgement | `locomo_errata.json (data)` |
| Strict evidence parser with existence check | Split on whitespace, zero-pad, assert every gold id exists. | ours (RET-12) | 0 | none | `unit test` |
| Answer-support recall metric | Gold answer string or judge-checked support present in context, next to gold-id recall; per-conversation columns. | ours (RET-9, RET-10) | 0 (string) or 1 judge call | string match misses paraphrase | `forensics report columns` |
| Dual headline with explicit exclusion set | Publish both headlines and the exclusion list. | Cognee 143 excluded golds (R14-eval-2) | 0 | none | `report columns` |

## R14-M-20 Profile and attribute state with history; knowledge update; destructive edits

**Tag:** R14-confirmed (confirms G-W4, S34 S42 S46) | **Layer:** ingest | **Priority:** P2 | **Effort:** M

### Problem statement

User facts change over time. The store must track attribute values with history, supersede by event time, answer 'what is X's ...' from the keyed value rather than the first-ranked raw turn, and never destroy the old value. The profile card is built but no read path consumes it.

Evidence (33 questions; measured (OBS-04 predicate; engine survey)): 33 attribute questions (single-hop or multi-hop) wrong; profile_card_v2 has no read consumer (S46 skipped); standing answers and histories unscreened on a keyed-state arm. 0 question ids are listed in `r14_master.json`. Source items: R14-LIT-P07, R14-prof-1, R14-prof-2, R14-link-2, R14-upd-1, R14-upd-2, OBS-04.

### Gaps (vs existing GAP_REGISTER ids)

G-W4 (extractor does not see keys of earlier facts) confirmed. OBS-04 proposes offline.person_attributes_v2 (S71). R14 adds: known-keys context to the extractor (Mnemon), predict-calibrate delta extraction (Nemori), a supersede-not-delete invariant (MemMachine/MIRIX defect), a judge-empty counter (Graphiti #1666) and read-time withdrawal of covered records.

Existing parts: offline.profile_card_v2; offline.standing_v2 + read.standing_v2; offline.histories_v2; read.profile_header_packing; ladder 13 rungs; S34 S35 S40 S42 S46.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Zhang2018-personalizing-dialo], [Wu2019-trade], [Chhikara2025-mem0], [Rasmussen2025-zep], [Wu2024-longmemeval].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Mnemon keyed state; Memobase profile slots; Honcho peer card; Graphiti bi-temporal; Supermemory isLatest.

### Hypotheses

Event-time-ordered supersede with the cardinality registry yields a correct current value and a history block; known-keys context removes duplicate-key facts and lets same-key supersession be model-driven; validate the history half on a knowledge-update benchmark.

Measured by: fail_mix_120 attribute subset (33) + MAB-CR dev for the history half; screen S99 known_keys (after S34/S42); S71 per OBS-GAPS. Expected effect (hypothesis): not sized beyond 33 targeted; history half is a benchmark claim (hypothesis).

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Person attributes v2 | Typed registry shared by miner, ladder and card; history render. | ours OBS-04; Memobase / Honcho cards | 0-1 per entity | privacy default-deny test needed | `offline.person_attributes_v2 (S71, OBS-GAPS)` |
| Known-keys context to the extractor | Show earlier state keys and latest values so the model reuses identities. | Mnemon fold prompt (R14-link-2); Nemori predict-calibrate | 0 extra (prompt grows) | prompt size on a 9B model | `write.known_keys (new, G-W4)` |
| Supersede-not-delete invariant and judge-empty counter | Never destroy an old value; count empty contradiction lists. | MemMachine/MIRIX defect; Graphiti #1666 (R14-prof-2, R14-upd-1) | 0 | none | `unit test + counter (new)` |
| Read-time withdrawal of covered records | Drop superseded or covered records at render. | Mnemon, Hindsight (R14-upd-2) | judge calls (Mnemon) | wrong withdrawal | `read.withdraw_covered (new)` |

## R14-M-21 Entity aliasing and link tables: seed-to-neighbour expansion without per-session LLM graph builds

**Tag:** R14-confirmed (confirms I54 (kin resolution), SOL-GRAPH) | **Layer:** ingest | **Priority:** P3 | **Effort:** M

### Problem statement

Names appear as nicknames, short forms, role words and typos, so entity-seeded legs are inert until aliases bind; multi-session evidence needs a hop from a seed hit to related stored items, but a graph built by an LLM per session is costly. String and embedding similarity alone mis-merge short names.

Evidence (9 questions; measured (OBS-01) + engine survey): 9 questions targeted by vocative/short-form binding (74 detected turns); entity leg inert until the graph arm builds entities; link expansion shares the 43 cross-session questions with M-2. 0 question ids are listed in `r14_master.json`. Source items: R14-LIT-P02, R14-ent-1, R14-link-1, R14-link-3, R14-hop-1, OBS-01.

### Gaps (vs existing GAP_REGISTER ids)

I54 confirmed and OBS-01 proposes write.vocative_alias_v2 (S68). R14 adds: LLM-free semantic/temporal link table (Hindsight) so multi-evidence recall does not depend on extract_graph calls, context-weighted merge score (name 0.5, co-occurrence 0.3, time 0.2), and the rule that tentative aliases stay 'maybe same'.

Existing parts: write.resolver_v2 (W2-09, off); entity ladder rules/llm; extract_graph (sleep); read.graph_leg_v2; read.neighbours_v2; S13 S32 S30 S38 S39.

### Survey

- Literature (keys in `R14_LITERATURE_SURVEY.md` / `r14_literature.json`): [Wu2020-blink], [DeCao2021-genre], [Joko2021-conv-entity-linking], [Peeters2023-llm-entity-matchi], [Bae2024-hipporag].
- Engines (`R14_ENGINE_SURVEY.md` / `r14_engines.json`): Hindsight memory_links; Graphiti entity-pair edges; A-Mem evolution; Supermemory updates/extends/derives.

### Hypotheses

A link table built at write with no extra LLM call (temporal within 24 h, semantic kNN, entity co-occurrence) plus one-hop expansion from semantic seeds lifts all-gold coverage on the cross-session set, and a context-weighted merge score cuts wrong merges on open-cast data.

Measured by: fail_retrieval + fail_mix_120; CPU prototype over stored embeddings of one conversation first; screen S100 link_table; S68 per OBS-GAPS. Expected effect (hypothesis): not sized separately (hypothesis); overlaps M-2.

### Solutions

| name | generic mechanism | source | cost (model calls) | risk | flag |
|---|---|---|---|---|---|
| Link-table expansion | Write-time temporal, semantic and entity-co-occurrence links; one-hop expansion from seeds at read. | Hindsight memory_links (R14-link-1); HippoRAG [Bae2024-hipporag] | 0 extra LLM | hub entities; timeout drops the leg | `write.link_table (new, S100)` |
| Rule alias binding | Vocative and short-form binding with a participant table. | ours OBS-01; BLINK-style linking [Wu2020-blink] | 0 | false bind on 2-letter names | `write.vocative_alias_v2 (S68, OBS-GAPS)` |
| Context-weighted resolution score | Add co-occurrence and temporal proximity to the merge score. | Hindsight (R14-ent-1); LLM matching only for the unsure band [Peeters2023-llm-entity-matching] | 0 + batched calls for unsure | tuning per embedder | `write.resolver_v2 (existing)` |
| Memory evolution at sleep | Rewrite neighbour context and tags when a new note arrives. | A-Mem (R14-link-3) | 2+ calls per note | drift; do after link table | `offline.evolve (new, deferred)` |

## Ranked roadmap (value per effort)

Rank orders by expected questions recovered per unit effort, with CPU-only / zero-model-call steps first. **CPU first** = the first step needs no model call or GPU (offline replay of logged ranks, unit tests, re-score of stored answers). **Built-off** = the mechanism already exists behind a flag and only needs a screen.

| rank | id | title | tag | layer | targeted q | effort | prio | CPU first | built-off | flag / screen |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | R14-M-3 | Fusion and rerank cut loss: ranked gold discarded when legs are fused into a small pool | R14-confirmed | retrieval | 68 | S | P1 | yes | yes | `read.pool_protect_per_leg=8 (existing config)`; offline CPU re-fusion from forensics first (no run); S82 protect8 + convex fusion; S83 budget keep; S67 queued |
| 2 | R14-M-7 | Explicit-date questions: the date is treated as ordinary words, no session-date prior at fusion | R14-confirmed | retrieval | 13 | S | P1 | yes | no | `read.date_prior (new)`; S87 date_prior (CPU re-fusion first); S24/S62/S63 queued |
| 3 | R14-M-1 | Multi-evidence recall: fixed depth, no coverage objective, weak aggregation trigger | R14-confirmed | retrieval | 55 | S | P1 | yes | no | `read.list_mode_trigger_v2 (new)`; S79 trigger + coverage (CPU first, offline over forensics); S80 second-retrieval bounded |
| 4 | R14-M-17 | Judge false negatives after the date guard: superset lists, annotation equivalence, containment | R14-confirmed | judge | 23 | S | P1 | yes | yes | `judge_conventions v3 (new, S96 re-score)`; S96 judge conventions v3 (re-score stored answers, no retrieval) |
| 5 | R14-M-18 | Judge false positives and the honest headline: partial lists credited, 81.4 is not 81.4 | R14-confirmed | judge | 34 | S | P1 | yes | yes | `report columns (no flag)`; S97 triple-column re-score of stored answers (reference run plus last two full runs) |
| 6 | R14-M-8 | Date and duration arithmetic done by the 9B reader; event dates lack precision and status | R14-confirmed | reader | 19 | S | P1 | yes | yes | `duration_solve / read.resolve_durations (existing)`; S88 duration_solve + milestones (built-off, needs a screen only) |
| 7 | R14-M-9 | Over-refusal and premise denial: no typed commit route for judgement questions | R14-confirmed | reader | 40 | S | P1 | no | yes | `read.inference_mode (S73, OBS-GAPS)`; S73 prompt, S74 probes (OBS-GAPS); S89 vote; S20 cat5 guard |
| 8 | R14-M-10 | Wrong detail and distractor use; one window size for every question shape | R14-confirmed | reader | 59 | M | P1 | no | no | `read.verify_binding (new) / --verify-answer`; S90 quote-then-answer; S91 window_by_shape; S92 verify_binding; S52-S54 queued |
| 9 | R14-M-11 | List and count completeness: partial lists, over-counted events, 'at least N' | R14-confirmed | reader | 25 | S | P1 | yes | yes | `harness evidence_table / two_call (existing)`; S93 evidence_table / two_call; S06, S11, S56 queued |
| 10 | R14-M-14 | Verbosity against a short gold: answer length follows difficulty, the lever is an output contract | R14-confirmed | reader | 49 | S | P2 | yes | yes | `read.query_contract (existing, S06/S11)`; S06, S11 (queued); add ratio>8 and list-recall columns |
| 11 | R14-M-19 | Gold errata, sibling evidence, id hygiene and retrieval metrics that overstate success | R14-confirmed | eval | 21 | S | P2 | yes | no | `locomo_errata.json (data)`; S98 support-recall report (CPU, no run); unit tests for the parser and errata |
| 12 | R14-M-2 | Cross-session spread: turn-level, session-agnostic retrieval; summaries and episodes as legs | R14-confirmed | retrieval | 43 | M | P2 | no | no | `read.session_cover (new), read.episode_leg_v2`; S81 session_cover; S31/S33/S61 stay as queued |
| 13 | R14-M-5 | Elliptical and context-dependent turns: decontextualisation and an index view | R14-confirmed | ingest | 32 | M | P2 | yes | no | `write.exchange_records (new)`; S85 exchange_records (CPU simulation first); S69/S70/S75 per OBS-GAPS |
| 14 | R14-M-12 | Multi-turn join and multi-entity decomposition at read | R14 | reader | 54 | M | P2 | no | no | `read.decompose_v2 (S77, OBS-GAPS)`; S94 reader-side join (after S77); S56 queued |
| 15 | R14-M-13 | Ungrounded answers: new content words appear nowhere in the context | R14-confirmed | reader | 37 | S | P2 | no | yes | `--verify-slots diagnosed (existing)`; S95 slot_verify diagnosed |
| 16 | R14-M-4 | Lexical-zero and paraphrase gold: surface form is the strongest predictor of a miss | R14-confirmed | retrieval | 81 | M | P2 | no | no | `write.doc_expansion (new)`; S84 doc expansion; S76 paraphrase probes (OBS-GAPS); word_vector_leg on the 21 ids |
| 17 | R14-M-6 | Indirect gold for judgement questions: evidence describes behaviour, never the question's words | R14-confirmed | retrieval | 28 | M | P2 | no | no | `read.planner=llm v3 / read.evidence_kind_probes (new)`; S86 evidence_kind_probes; shares S74 probes (OBS-06) |
| 18 | R14-M-20 | Profile and attribute state with history; knowledge update; destructive edits | R14-confirmed | ingest | 33 | M | P2 | no | no | `offline.person_attributes_v2 (S71, OBS-GAPS)`; S99 known_keys (after S34/S42); S71 per OBS-GAPS |
| 19 | R14-M-21 | Entity aliasing and link tables: seed-to-neighbour expansion without per-session LLM graph builds | R14-confirmed | ingest | 9 | M | P3 | no | no | `write.link_table (new, S100)`; S100 link_table; S68 per OBS-GAPS |
| 20 | R14-M-15 | Image captions: misattributed or photo-only evidence | R14-confirmed | reader | 6 | S | P3 | no | no | `write.caption_facts (S72, OBS-GAPS)`; S72 (OBS-GAPS) |
| 21 | R14-M-16 | Speaker attribution: wrong-speaker answers are rare, but swapped-speaker golds penalise correct attribution | R14 | eval | 39 | S | P3 | yes | no | `report column (no flag)`; no new screen; apply to any perspective screen |

Notes on the order:

1. Ranks 1-3 (M-3, M-7, M-1) are retrieval changes with a free offline sizing step: leg ranks are logged to depth 60 in `forensics.jsonl`, so protect-8, keep-15, a date prior and the aggregation trigger can be replayed on CPU before any run. They target 68 + 13 + 55 questions with heavy overlap; the combined ceiling is the 115 retrieval losses, worth at most about 5.4 points if every one received complete gold (reader accuracy given all gold, same run).
2. Ranks 4-5 (M-17, M-18) protect every other measured gain from judge error and cost no model calls. Do them before reading screen deltas that touch multi-hop.
3. Ranks 6-9 (M-8, M-9, M-10, M-11) are mostly built-off flags that need a screen, not new code; M-9 follows the OBS-GAPS build-first recommendation (S73/S74).
4. Ranks 12-17 need new ingest or read machinery (episode cover, restatement, decomposition, document expansion); wait for the verdicts of the queued write-v2 screens (S62, S03, S04), as OBS-GAPS also advises.
5. Reader position effects (lost in the middle) did not reproduce at this context length (R14-POS-1: quintile accuracy 0.89-0.92, Fisher p=0.90); no screen is proposed. Re-measure on long-context benchmarks only.
6. Overlap: one question can sit in several items; targeted counts are not additive.
