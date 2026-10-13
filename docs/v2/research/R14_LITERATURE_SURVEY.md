# R14 literature survey: generic memory problems and the techniques that address them (RES-LIT)
Round R14, package RES-LIT, 2026-10-14. Branch `wp/RES-LIT`. Companion data: `r14_literature.json`.
Scope: 12 generic problems, 2020-2026 literature. No model calls were used. Licence: no dataset text, no dataset speaker names; LoCoMo is cited by class, count and question category only.
## How to read this document
- Reference keys in square brackets `[AuthorYYYY-slug]` resolve in section 'References'. Keys marked `existing` are in `memory-research/_shared/references.md`; keys marked `R14-new` are added here with full citation and URL or DOI.
- Every arXiv id marked `R14-new` was resolved through the arXiv API in this session (title and first author matched). Items marked 'from memory' (Allen 1983, SUTime, HeidelTime, RRF DOI) were not machine-checked.
- 'Reported effect' is what the paper or report claims, not what MemSpine would see. Numbers marked 'approx.' are recalled from the paper and were not re-read; treat them as pointers. Vendor reports are labelled as such.
- 'Fits MemSpine where' uses the five stages ingest / retrieve / compose / reader / judge.
- MemSpine status comes from `docs/v2/LIBRARIES.md`, `docs/v2/PLAN.md`, `docs/v2/LOCOMO_PROBLEM_MAP.md`, `docs/v2/SCREEN_ATLAS.html` (screen ids) and `evals/analysis/GAP_REGISTER.md` (ids such as A2, B14, I2). Where a screen has not run, the item is 'built, off'.
- LoCoMo wrong-answer counts are from the clean-jina-full reference run (287 wrong of 1,540 on cat 1-4, 81.4%), as listed in `LOCOMO_PROBLEM_MAP.md`.
## Top 10 techniques by expected value for MemSpine
Ranking rule: (size of the targeted wrong-answer family) x (credible effect from the literature) / (new cost and risk), then whether the parts already exist. These are expectations, not measurements.
| rank | technique | key papers | problem | why |
|---|---|---|---|---|
| 1 | Sufficiency-gated answer / infer / abstain policy (Sufficient Context, Chain-of-Note, CRAG idea) | [Joren2024-sufficient-context], [Yu2023-chain-of-note], [Yan2024-crag] | P08 | The only cat 5 lever, and cat 5 is 446 questions never measured; LIB-12 gate and LIB-43 deep tier already exist, so the cost is wiring plus one screen. |
| 2 | One targeted second retrieval that names the missing slot (IRCoT / Self-Ask idea, bounded) | [Trivedi2023-ircot], [Press2023-self-ask] | P05 | List/count/join questions are the largest wrong family with a retrieval cause; B8 says lost gold is in sessions with no hit. |
| 3 | Deterministic date calculator for the reader (PoT/PAL idea without code execution) | [Chen2022-pot], [Gao2023-pal] | P03 | 10 plus 1 questions are pure arithmetic; zero model cost; rule-only and safe. |
| 4 | Session or episode gist leg lifted to turns (ReadAgent / SeCom / RAPTOR idea) | [Lee2024-readagent], [Pan2025-memory-construction-retrieval], [Sarthi2024-raptor] | P06 | Targets the 68 stage-b recall misses; all pieces exist flag-off (S31, S33, S61). |
| 5 | Hits-first evidence ordering and edge placement (Lost in the Middle / Found in the Middle) | [Liu2024-lost-middle], [Hsieh2024-found-in-middle] | P08 | Free, addresses the largest single class (`wrong_detail` 46); already a setting, no screen. |
| 6 | Deterministic pre-judge plus a second-family judge for agreement | [Kamalloo2023-eval-odqa], [Verga2024-poll], [Bulian2022-tomayto] | P11 | Protects every other measured gain from judge error in both directions; cheap. |
| 7 | Document expansion at write (generated questions per fact, filtered), not query-time HyDE | [Nogueira2019-doc2query], [Gospodinov2023-doc2query-minus], [Weller2023-expansion-fails] | P04 | Pays at write once, avoids the query-time harm that Weller et al. and our B14 found; can share the extraction call. |
| 8 | Reranker over a wider pool plus protected family pool; test convex combination vs RRF | [Bruch2023-fusion-functions], [Cormack2009-rrf], [Nogueira2020-monot5] | P09 | Targets 47 cut-stage losses; the retrieval-only screens are free. |
| 9 | Rule-built context prefix on embedded text; late chunking as a later step | [Anthropic2024-contextual-retrieval], [Gunther2024-late-chunking] | P01 | Gets a share of the contextual-retrieval gain with no LLM call; moderate integration risk. |
| 10 | Event-time supersede with value histories verified on a knowledge-update slice | [Rasmussen2025-zep], [Chhikara2025-mem0], [Memoryagentbench2025-memoryagentbench] | P07 | Little LoCoMo effect but the core claim of a memory engine; MAB-CR is the proxy since LongMemEval is out of the suite. |

Not in the top 10 on purpose: HyDE and RAG-Fusion style query-time expansion (Weller et al. and our B14 point to neutral or harmful on a strong hybrid), GraphRAG-scale graph extraction (very high write cost; benefit is on global questions that LoCoMo cat 1-4 rarely asks), ColBERT/SPLADE legs (real but costly to integrate; listed as a later retrieval-only screen), LLM listwise reranking (token cost).
## Coverage map: problem to existing MemSpine item
| problem | built | wired / screened | main open gap |
|---|---|---|---|
| P01 Turn decontextualisation and coreference | see section | see section | No embedded-text context prefix and no coref step on the raw-turn index; L1 facts cover this only when extraction is on. |
| P02 Entity linking and alias resolution in conversations | see section | see section | Nickname and role-word aliasing on real dialogue is untested (only the synthetic X-05 set); I5 speaker parsing. |
| P03 Temporal reasoning and normalisation | see section | see section | Reader-side date arithmetic helper (no code-execution path); screens S24/S62/S63 results pending. |
| P04 Query rewriting and expansion | see section | see section | No document expansion and no learned sparse leg; HyDE/planner have no screen. |
| P05 Question decomposition and multi-hop | see section | see section | No screen for agentic_mode/second_round; I4 and I11 generalisation. |
| P06 Hierarchical summaries and memory structures | see section | see section | No recursive multi-level tree; most screens not yet run (flag-off). |
| P07 Personal profile, attribute extraction and knowledge update | see section | see section | Update handling has no screen on LongMemEval-style or MAB-CR data (LongMemEval excluded from suite by owner decision; MAB-CR is the proxy). |
| P08 Reader inference vs refusal; calibrated abstention; context use | see section | see section | Cat 5 never measured; no calibrated abstention policy; no answer-style/contract check on output. |
| P09 Retrieval fusion and reranking | see section | see section | No learned fusion; no late-interaction or SPLADE leg; reranker adopted on coverage (I9). |
| P10 Multimodal (image caption) memory; tool and world-knowledge augmentation | see section | see section | No image memory; external block unscreened. |
| P11 LLM-as-judge reliability for memory QA | see section | see section | Single judge family equal to reader; kappa not measured outside LoCoMo. |
| P12 Memory benchmark methodology: what leading systems report and how | see section | see section | Cat 5 and official-judge reproduction. |

---

## P01. Turn decontextualisation and coreference
**Generic problem.** A stored unit (a turn, a fact, a chunk) is retrieved and read out of its context. Pronouns, ellipsis, deictic time ('last week', 'there') and elided speakers make the unit ambiguous or unmatched by the query, so the unit is either not found (lexical or embedding mismatch) or found but misread. The generic fix is to make each retrieval unit self-contained at write time, or to carry context into the unit at embed time.
**Link to LoCoMo problem classes (counts only).** Linked to LoCoMo classes `implicit_or_paraphrased_gold` (4), `vocabulary_mismatch_recall` (4), `speaker_confusion` (1), `multi_evidence_recall` (5) and the open-domain recall classes; all small counts, mostly H-rules. Not measured as a separate class.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Decontextualisation of sentences | [Choi2021-decontextualization] | Edit a sentence so it is interpretable alone (resolve pronouns, add minimal context) while keeping its meaning; defined as a task with a human-annotated dataset. | Task definition and baseline models; gains downstream measured on QA-style uses are modest in the paper (not re-checked in detail). | 1 model call per unit at write time (small model is enough in the paper's setting). | ingest |
| Propositions as retrieval units | [Chen2023-dense-x] | An LLM (Propositionizer) rewrites passages into atomic, self-contained propositions; index propositions instead of passages or sentences. | Reported better recall than passage or sentence units for unsupervised dense retrievers (about +12 points relative Recall@5 on average, approx.) and smaller gain for supervised ones; better downstream open-domain QA under a fixed token budget. | Write cost: one LLM pass over every passage; index grows several-fold; loses discourse links between propositions. | ingest / retrieve |
| Contextual retrieval (prepend a generated context line to each chunk before embedding and BM25) | [Anthropic2024-contextual-retrieval] | An LLM sees the whole document and writes 50-100 tokens situating the chunk; the line is prepended for both the embedding and the BM25 index. | Vendor report: top-20 retrieval failure rate down 35% (contextual embeddings), 49% (plus contextual BM25), 67% (plus reranking). Not peer reviewed; corpora are the vendor's own. | One call per chunk; prompt caching makes the document prefix cheap on the vendor API; a local model gets no such discount. | ingest |
| Late chunking | [Gunther2024-late-chunking] | Embed the whole long text with a long-context encoder first, then pool token vectors per chunk, so each chunk vector carries document context without any LLM call. | Reported gains on retrieval benchmarks for documents with cross-chunk references; larger for long documents (magnitudes not re-checked). | No generation; needs a long-context embedding model and one long forward pass per document or session. | ingest |
| Mix-of-Granularity routing | [Zhong2024-mix-of-granularity] | Keep several chunk sizes and a trained router picks the granularity per query. | Reported gains over fixed granularity on medical QA RAG (details not re-checked). | Several indexes; a small router. | retrieve |
| Coreference models and LLM coreference | [Lee2017-e2e-coref], [Toshniwal2020-coref-bounded-memory], [Le2023-llm-coref] | Span-ranking neural coref; bounded-memory variants for long documents; prompting LLMs to resolve mentions. | Le and Ritter report that LLMs are not robust coreference resolvers out of the box (sensitive to prompt and to mention form). Dialogue-domain numbers not re-checked. | A coref model is a small encoder; an LLM pass is a call per window. | ingest |
| Conversational query rewriting (the query-side twin) | [Yu2020-conv-query-rewriting], [Mao2023-llm4cs], [Mo2023-convgqr] | Rewrite a context-dependent question into a self-contained one using the dialogue history, with few-shot LLM or generative models. | Consistent gains on conversational search sets (TREC CAsT, QReCC); ConvGQR and LLM4CS report better than T5 rewriters (magnitudes not re-checked). | One call per query. | retrieve |
| Context line plus speaker header (cheap form) | [Maharana2024-locomo], [Xu2022-beyond-goldfish-memory] | Not a paper technique: store `speaker, date, previous-turn gist` as the embedded text. Closest published practice is the session-date prefix used by the benchmarks themselves. | No isolated ablation found. | Zero model calls. | ingest |

**Testable hypotheses for MemSpine.**
- `H-P01-a` Facts compiled at write time (window_extract=facts, LIB-05 envelope) with a resolved subject and absolute date raise recall of `implicit_or_paraphrased_gold` and `vocabulary_mismatch_recall` questions compared with raw-turn units. Expected effect: +2 to +5 of the 8 wrong questions in those two classes recovered; neutral elsewhere.
- `H-P01-b` Prepending a rule-built context line (speaker, session date, subject of the previous turn) to the embedded and BM25 text, without any LLM, gets a share of the contextual-retrieval gain. Expected effect: Gold-turn recall at pool 30 +0.5 to +1.5 points; zero write calls. Screen as retrieval-only on the dev slice.
- `H-P01-c` Late chunking over a session with a long-context encoder gives context-aware turn vectors cheaply, closing the pronoun and ellipsis gap for vector-only misses (`fusion_loss_vector_only` 6, `lexical_only` 12). Expected effect: Fewer vector-only misses; risk: encoder swap changes all other retrieval numbers, so run as an extra leg first.

**Does MemSpine already have it?** Partly. `ingest/segment.py` (LIB-14) builds NEW+CONTEXT lines for the extractor; `write.window_extract=facts` and `write.envelope_v2` produce L1 facts; `read.neighbours_v2` and LIB-11 lift derived hits back to turns. Not present: proposition-style atomic units as a retrieval layer on its own, a rule-built context prefix on the embedded text, late chunking, any coreference component.

**Open gap.** No embedded-text context prefix and no coref step on the raw-turn index; L1 facts cover this only when extraction is on. Priority: M.

---

## P02. Entity linking and alias resolution in conversations
**Generic problem.** People, places and things are named by nicknames, short forms, role words ('my sister'), typos and cross-language variants; the same entity appears under many surface forms and different entities share one form. Retrieval and aggregation (lists, counts, joins) need one canonical id per entity, with an explicit unresolved state when unsure.
**Link to LoCoMo problem classes (counts only).** Linked to `list_aggregation_generic` (37), `speaker_confusion` (1), and the entity leg. Alias misses are not measured directly in the problem map.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Dense entity linking against a catalogue | [Wu2020-blink] | Bi-encoder retrieves candidate entities, cross-encoder re-ranks; zero-shot to new entities from descriptions. | Strong zero-shot linking on Wikipedia-style catalogues; needs an entity catalogue with descriptions. | Encoder pair; no generation. | ingest |
| Generative entity retrieval | [DeCao2021-genre] | Autoregressively generate the entity name with constrained decoding over a trie. | Competitive linking and retrieval with a very small memory footprint; needs a closed name set. | One constrained decode per mention. | ingest |
| Conversational entity linking | [Joko2021-conv-entity-linking] | Defines linking in dialogue where mentions are implicit and anaphoric; provides datasets and shows off-the-shelf linkers drop on conversation. | Shows a gap between document-trained linkers and dialogue (numbers not re-checked). | Dataset and baseline only. | ingest |
| LLM entity matching (alias pairs as a pairwise decision) | [Peeters2023-llm-entity-matching] | Prompt an LLM with two records and ask whether they refer to the same entity; compared with fine-tuned PLMs. | Reports that LLMs match or beat fine-tuned models with little or no labelled data, with prompt sensitivity (details not re-checked). | A call per candidate pair; restrict to pairs a cheap blocker proposes. | ingest |
| Rule resolver with blocking, plus LLM only for the unsure band | (ours: LIB-04) and [Peeters2023-llm-entity-matching] | Alias, homonym, numeric and cross-language rules; unsure pairs stay separate or go to a model. | Ours: X-05 entity pairs precision(same) 1.000, recall 0.976, 0 false merges (internal set). | Zero calls for rules. | ingest |
| Entity-seeded retrieval with hub damping | [Bae2024-hipporag], [Gutierrez2025-hipporag2], [Chhikara2025-mem0] | Link query entities to graph nodes, seed personalised PageRank (HippoRAG) or boost memories that mention the entity (Mem0). | HippoRAG reports large multi-hop gains over dense retrieval and much lower cost than iterative retrieval (about 10-30x cheaper, 6-13x faster than IRCoT, approx.). | NER at write time; graph build. | retrieve |

**Testable hypotheses for MemSpine.**
- `H-P02-a` A nickname and role-word alias table built from the conversation itself (self-introductions, 'my X, Name') and applied at query time as expansion raises the entity leg's hit rate on list and join questions. Expected effect: Small: 1 to 3 questions of the `list_aggregation_generic` class; no cost.
- `H-P02-b` Keeping unresolved pairs as 'maybe same' (tentative alias with confidence) instead of forcing a merge costs no accuracy and prevents the false-merge failures that LLM linkers show. Expected effect: 0 regressions on a false-merge probe; same accuracy on the dev slice.

**Does MemSpine already have it?** Mostly yes. LIB-04 `derive/resolve.py` (alias, homonym, numeric, cross-language; X-05 precision 1.000, recall 0.976), LIB-34 `read/entity_leg.py` (anti-hub damping), LIB-35 edges, `read.entity_leg_v2`. Gap I5: speaker identity is parsed from a capitalised `Name:` prefix. No evidence yet that nickname handling is exercised on a real conversation set.

**Open gap.** Nickname and role-word aliasing on real dialogue is untested (only the synthetic X-05 set); I5 speaker parsing. Priority: L.

---

## P03. Temporal reasoning and normalisation
**Generic problem.** Memories carry absolute and relative time expressions ('yesterday', 'two weeks ago', 'in the spring'), at several precisions, anchored on a session date that is not the event date. Questions ask for dates, durations, order, and 'current as of' values. Failures: wrong anchor, lost precision, date arithmetic errors by the reader, and retrieval that ignores time.
**Link to LoCoMo problem classes (counts only).** Classes `temporal_evidence_missing` (14), `date_duration_arithmetic` (10), `date_wrong_event` (8), `date_offset_small` (1), `relative_date_gold_vs_judge` (1), `refusal_premise_denial` temporal part (10 of 14). About 43 of 287 wrong answers touch time.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Rule-based time tagging and normalisation | [Strotgen2010-heideltime], [Chang2012-sutime] | Pattern grammars find time expressions and normalise to TIMEX3 values relative to a reference date. | Long-standing strong baselines for English news-style text (precision high, recall limited on informal talk). | Zero model calls; CPU. | ingest |
| Interval algebra for order and relations | [Allen1983-interval-algebra] | Thirteen relations between intervals (before, meets, overlaps ...) give a closed set of comparisons. | Theory, no benchmark; used as the logic under many temporal KGs. | Zero. | compose / reader |
| Offload date arithmetic to code | [Chen2022-pot], [Gao2023-pal] | The model writes a small program (or call to a date helper) for the arithmetic and the interpreter computes it. | Reported large gains on numerical reasoning over CoT (PoT about +12% average over CoT, approx.); not measured on memory QA. | One extra generation; safe sandbox or a closed set of helpers. | reader |
| Temporal reasoning benchmarks and diagnosis | [Chen2021-timeqa], [Tan2023-tempreason], [Wang2023-tram], [Fatemi2024-test-of-time] | TimeQA (time-sensitive facts), TempReason (L1 time-time, L2 time-event, L3 event-event), TRAM, Test of Time (synthetic structure vs semantics). | All report that LLMs are weak at time arithmetic and event ordering; TempReason proposes temporal-span-extraction pretraining plus time-sensitive RL (gains reported on their set). | Evaluation resources. | judge / reader |
| Time-aware retrieval | [Gade2024-tempralm], [Dhingra2022-time-aware-lm] | TempRALM adds a temporal term to the retriever score (recency of the document relative to the query time); Dhingra et al. condition LM on timestamps. | TempRALM reports up to about 74% over a plain RAG baseline on temporal QA (approx., single paper). | Zero to small; needs per-record timestamps. | retrieve |
| Time-aware query expansion and time-range filtering for memory | [Wu2024-longmemeval] | LongMemEval's proposed optimisations: extract the time range from the question and filter or expand the index, index with event timestamps. | Reported recall gains on the temporal-reasoning type (about 7-11% recall, approx.; see `paper/research_notes/longmemeval.md`). | One extraction per query (LLM or rule). | retrieve |
| Neuro-symbolic temporal reasoning over memory | [Ge2025-tremu] | TReMu: timeline summarisation per session, neuro-symbolic reasoning, multiple-choice temporal QA on a LoCoMo-derived set. | Reports a large improvement over plain LLM prompting on its own temporal benchmark (size not re-checked). | Extra calls for timelines. | ingest / reader |
| Bi-temporal knowledge graphs | [Rasmussen2025-zep], [Graphiti2024-graphiti], [Su2026-beyond-dialogue-time] | Edges carry event time and ingest time with validity intervals; invalidation instead of overwrite. | Zep reports gains on LongMemEval temporal and multi-session types (vendor paper; protocol differs from other systems). | Edge extraction calls at write. | ingest / retrieve |

**Testable hypotheses for MemSpine.**
- `H-P03-a` A deterministic date calculator exposed to the reader (resolved event date plus a closed set of operations: difference, weekday, offset) removes arithmetic errors without a code-writing step. Expected effect: Up to 10 of the `date_duration_arithmetic` and 1 of `date_offset_small` questions fixed; no regression elsewhere (rule-only).
- `H-P03-b` Time-range extraction from the question (rule first) used as a soft boost on event-dated records recovers part of `temporal_evidence_missing`. Expected effect: +3 to +6 of the 14 questions; 0 model calls.
- `H-P03-c` Question-date anchoring (I8: relative questions anchored on the question date, not the session date) is needed for LongMemEval-style data and should be neutral on LoCoMo. Expected effect: Neutral on LoCoMo, positive on LongMemEval temporal; test as a switch.

**Does MemSpine already have it?** Largely built, mostly off or unmeasured. LIB-01 `derive/temporal.py` (anchor ladder, precision, three-valued comparisons, durations, relations, as-of), LIB-22 `read/timeview.py`, `read.temporal_relative`, `temporal_infer_year`, `temporal_leg_event_dates`, W2-14, screens S24, S62, S63. Gap I8: question-date anchoring untested; C5: duration annotator proposed.

**Open gap.** Reader-side date arithmetic helper (no code-execution path); screens S24/S62/S63 results pending. Priority: H.

---

## P04. Query rewriting and expansion
**Generic problem.** The query and the stored text use different words (vocabulary mismatch), different granularity (a question vs a statement), or the query needs context to be self-contained. Fixes rewrite the query, generate a hypothetical answer, add synonyms, or move expansion into the index (sparse learned expansion, document expansion).
**Link to LoCoMo problem classes (counts only).** Classes `vocabulary_mismatch_recall` (4), `lexical_outranked_recall` (2), `fusion_loss_lexical_only` (12), `open_domain_inference_retrieval` (28, partly), `implicit_or_paraphrased_gold` (4). Our own B14 found that existing expansion options are neutral or harmful.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| HyDE (hypothetical document embeddings) | [Gao2022-hyde] | LLM writes a hypothetical answer passage; embed it and retrieve neighbours of that vector. | Zero-shot dense retrieval improved over unsupervised Contriever and competitive with fine-tuned retrievers on web search and low-resource sets (magnitudes not re-checked). | One generation per query; can hallucinate, which Weller et al. show hurts strong retrievers. | retrieve |
| Query2doc | [Wang2023-query2doc] | Few-shot LLM writes a pseudo-document; append it to the query (repeat the query for BM25). | About +3 MRR@10 points for BM25 on MS MARCO dev (approx.); smaller for strong dense retrievers. | One generation per query. | retrieve |
| Step-back prompting | [Zheng2023-step-back] | Ask a more abstract question first and retrieve for both. | Reported large gains on reasoning-heavy QA including TimeQA (size not re-checked); the retrieval-specific effect is smaller. | One extra generation and retrieval. | retrieve |
| Multi-query and RAG-Fusion | [Rackauckas2024-rag-fusion] | Generate several paraphrases of the query, retrieve for each, fuse rankings with RRF. | Practitioner report (single paper, small evaluation); gains on recall for ambiguous queries. | N generations and N retrievals. | retrieve |
| Rewrite-Retrieve-Read | [Ma2023-rewrite-retrieve-read] | A small rewriter trained with RL from the reader's feedback rewrites the query for a frozen retriever and reader. | Gains on open-domain and multiple-choice QA over plain retrieve-then-read. | Rewriter training; one call per query. | retrieve |
| Prompted query expansion and its failure modes | [Jagerman2023-query-expansion-prompting], [Weller2023-expansion-fails] | Prompt variants (Q2D, CoT) for expansion. Weller et al. study when expansions fail. | Expansion helps weaker retrievers and hurts strong ones; failures grow when the LLM lacks knowledge about the topic or the query is already specific. Important for a strong hybrid pipeline. | One call per query. | retrieve |
| Corpus-steered and feedback-based expansion | [Lei2024-csqe], [Mackie2023-grf] | Expand using first-pass retrieved text (CSQE) or LLM-generated feedback text (GRF), so terms come from the corpus, not only the model. | CSQE improves BM25 and some dense settings without the hallucination problem (magnitudes not re-checked). | First pass retrieval plus one generation. | retrieve |
| Learned sparse retrieval (SPLADE) | [Formal2021-splade], [Formal2021-splade-v2], [Chen2024-bge-m3] | Encoder outputs a sparse vector over the vocabulary with learned term weights and expansion terms; BGE-M3 gives dense, sparse and multi-vector outputs from one model. | SPLADE v2 is competitive with dense retrievers in-domain and strong on BEIR [Thakur2021-beir]; handles synonyms inside an inverted index. | Encoder pass at write and query; index size larger than BM25. | ingest / retrieve |
| Document expansion (doc2query) | [Nogueira2019-doc2query], [Gospodinov2023-doc2query-minus] | Predict likely questions for each document at write time and append them to the index text; Doc2Query-- filters hallucinated questions. | Large MRR gain over BM25 on MS MARCO; Doc2Query-- shows filtering out low-quality generated queries improves effectiveness and shrinks the index. | One generation per unit at write; no query-time cost. | ingest |
| Static word-vector or lexical analyser fixes | (ours, B6, B15) | English analyser (stemming) and a word-vector leg for BM25 misses. | Ours: analyser recovered 21 of the morphology-only misses (B6); word-vector leg implemented. | Zero calls. | retrieve |

**Testable hypotheses for MemSpine.**
- `H-P04-a` Document expansion at write time (generated questions per L1 fact, filtered as in Doc2Query--) helps the `implicit_or_paraphrased_gold` and `vocabulary_mismatch_recall` classes without any per-query cost, unlike HyDE. Expected effect: +2 to +4 of 8 questions; ingest cost one call per fact (shared with extraction if the same call returns questions).
- `H-P04-b` Query expansion is net-neutral or negative on this pipeline because the hybrid plus reranker is a strong retriever (Weller et al.); only a gated expansion (fire when first-pass gate says weak) can help. Expected effect: Gated expansion: +0 to +3 questions; ungated: -2 to +1 (consistent with B14).
- `H-P04-c` A learned sparse leg (SPLADE or BGE-M3 sparse) replaces the BM25 leg for the lexical-outranked and lexical-only fusion losses. Expected effect: Recall at pool 30 +1 to +3 points; costs an index rebuild.

**Does MemSpine already have it?** Partly. `read.planner=llm`, `read.compose_rewrites`, HyDE-style answer hypothesis (off, no screen), LIB-43 one bounded rewrite on the deep tier, `read.word_vector_leg` (B15), `lexical_analyzer=english` (B6). B14: existing expansion options neutral or harmful. Not present: learned sparse retrieval, document expansion (doc2query-style) at write.

**Open gap.** No document expansion and no learned sparse leg; HyDE/planner have no screen. Priority: M.

---

## P05. Question decomposition and multi-hop
**Generic problem.** Answers need several pieces that no single retrieval returns: chains (A then B), joins, intersections or comparisons, and aggregation over many events (lists, counts). One retrieval step with a fixed pool misses the later pieces because their query depends on earlier ones, or because they span sessions.
**Link to LoCoMo problem classes (counts only).** Classes `list_aggregation_generic` (37), `list_incomplete` (16), `count_wrong` (9), `multi_evidence_recall` (5); tags `3plus_gold_turns` 55 and `spans_3plus_sessions` 43 on wrong questions. These are the largest wrong-answer family after reader detail errors.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Least-to-most prompting | [Zhou2023-least-to-most] | Decompose into easier subquestions solved in order, each using earlier answers. | Near-perfect on SCAN length split with small examples; strong on compositional tasks; not retrieval-specific. | N generations. | reader / retrieve |
| Self-Ask | [Press2023-self-ask] | The model asks and answers follow-up questions, optionally with a search engine for each. | Narrows the compositionality gap (the gap between answering sub-facts and the compositional question). | N generations plus retrievals. | retrieve |
| IRCoT | [Trivedi2023-ircot] | Interleave retrieval with chain-of-thought: each reasoning sentence is the next query. | Large retrieval and QA gains on HotpotQA, 2WikiMultihopQA, MuSiQue, IIRC (double-digit retrieval recall gain, approx.), even for small readers. | One retrieval per reasoning step; many calls. | retrieve |
| Decomposed prompting | [Khot2023-decomposed-prompting] | A decomposer prompt routes sub-tasks to specialised handlers (including a retriever or symbolic function). | Outperforms CoT and least-to-most on several compositional tasks; supports hierarchical decomposition. | Several calls. | reader |
| Adaptive routing by query complexity | [Jeong2024-adaptive-rag] | A small classifier picks no retrieval, single-step, or multi-step retrieval per query. | Better accuracy/cost trade-off than always using the heavy path. | A classifier call. | retrieve |
| Active retrieval on uncertainty | [Jiang2023-active-retrieval-augmented], [Asai2024-self-rag] | FLARE retrieves again when the next sentence has low-confidence tokens; Self-RAG learns reflection tokens for when to retrieve and whether passages are useful. | Gains on multi-hop and long-form QA; Self-RAG needs a fine-tuned model. | Extra retrievals only when triggered. | retrieve / reader |
| Graph-structured retrieval for joins | [Bae2024-hipporag], [Gutierrez2025-hipporag2], [Edge2024-local-global], [Guo2024-lightrag] | Entity graph with personalised PageRank (HippoRAG), community summaries for global questions (GraphRAG), dual-level keyword retrieval (LightRAG). | HippoRAG: up to about 20% better multi-hop retrieval than dense, one-step; GraphRAG: wins over naive RAG on comprehensiveness (72-83% win rate, approx.) for global sensemaking questions. | Graph extraction per chunk is the main cost (many calls at write). | ingest / retrieve |
| Map-reduce and agent chains over long memory | [Zhang2024-chain-of-agents] | Worker agents read segments in sequence and pass notes; a manager answers. | Beats RAG and full-context baselines on several long-context tasks (up to about 10%, approx.). | Calls proportional to context size. | reader |
| Set-operation queries | [Malaviya2023-quest] | QUEST: entity-seeking queries with implicit intersection, union and difference; shows retrievers and LLMs struggle with set constraints. | Benchmark finding: low F1 especially for queries with set operations; no method claims a full fix. | Evaluation resource. | retrieve / compose |
| Benchmarks to test decomposition | [Yang2018-hotpotqa], [Trivedi2022-musique] | HotpotQA and MuSiQue (MuSiQue built to resist single-hop shortcuts). | MuSiQue is markedly harder than HotpotQA for single-hop-trained models. | Evaluation. | judge |
| Event table, enumerate-then-count | (ours: count-verify, evidence-table, LIB-15 obligations) | Reader first lists matching events with their sources, then counts or compares in code or rule. | Harness flags exist; no published equivalent on memory QA found. | One or two reader calls. | reader |

**Testable hypotheses for MemSpine.**
- `H-P05-a` For list and count questions, a sufficiency check that names the missing slot and triggers exactly one targeted second retrieval (LIB-43 plus `second_round`) beats widening the pool, because lost gold is mostly in sessions with no first-pass hit (B8: 75%). Expected effect: +8 to +15 of the 37 `list_aggregation_generic` questions if slot-naming works; cost one extra call on the list-shaped 10-15% of queries.
- `H-P05-b` Enumerate-then-count with code-side counting (the model lists events, a function counts and de-duplicates) fixes `count_wrong`. Expected effect: +3 to +6 of 9; zero extra retrievals.
- `H-P05-c` Set operations (intersection, 'both', 'neither') need per-constraint retrieval and an explicit intersection, as in QUEST analysis, not a single merged query. Expected effect: Gains limited to comparison-shaped questions (small count on LoCoMo; larger on a join-heavy slice).

**Does MemSpine already have it?** Built, mostly off. `read.agentic_mode=query|slot`, `read.second_round`, `read.fact_chain`, LIB-15 obligations (join, duration, list, count, comparison), LIB-43 deep tier, `read.set_question_wide`, bridge-hop gate (I11: thresholds tuned on one sample), harness `--count-verify`, `--evidence-table`. Screens S55, S56, S63. Gap: list-mode trigger is a LoCoMo-vocabulary regex (I4).

**Open gap.** No screen for agentic_mode/second_round; I4 and I11 generalisation. Priority: H.

---

## P06. Hierarchical summaries and memory structures
**Generic problem.** Questions arrive at different grain: a detail inside one turn, a theme across sessions, a global pattern. A flat turn index serves the first; the others need summaries, episodes, graph structure or communities, with links back to the source turns so details can still be read.
**Link to LoCoMo problem classes (counts only).** Classes `multi_evidence_recall`, `list_aggregation_generic`, `temporal_evidence_missing`; B8 (75% of lost gold is in sessions with no hit). Atlas tags `spans_3plus_sessions` 43.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| RAPTOR | [Sarthi2024-raptor] | Embed chunks, cluster, summarise clusters, recurse; retrieve over all tree levels (collapsed tree or top-down). | Reported +20% absolute on QuALITY with GPT-4 (approx.); gains on NarrativeQA and QASPER. | A summary call per cluster at each level; index rebuild when data changes. | ingest |
| GraphRAG community summaries | [Edge2024-local-global] | Extract entity graph, detect communities (Leiden), pre-summarise communities, answer global questions by map-reduce over summaries. | Wins over naive RAG on comprehensiveness and diversity (72-83%, approx.); very high build cost. | Many calls per chunk at write; map-reduce at query. | ingest / compose |
| HippoRAG / HippoRAG 2 | [Bae2024-hipporag], [Gutierrez2025-hipporag2] | Open-IE triples, graph, personalised PageRank from query entities; v2 adds dense passage nodes and recognition memory filter. | HippoRAG 2 reports better factual, sense-making and associative memory results than standard RAG together (single-paper numbers). | Triples at write; PPR at query is cheap. | ingest / retrieve |
| MemGPT / Letta | [Packer2023-memgpt] | OS-style paging between context and external stores, with the model issuing memory calls. | Strong on its document-QA and deep-memory-retrieval tasks; depends on agent loop quality. | Many calls per turn. | compose |
| MemoryBank | [Zhong2023-memorybank] | Daily and global summaries plus Ebbinghaus-style forgetting and user portrait. | Reported qualitative gains; weak benchmark. | Summary calls. | ingest |
| Generative Agents reflection | [Park2023-generative-agents] | Observations scored for importance; when the sum passes a threshold the agent writes higher-level reflections, stored as new memories with pointers. | Ablation in the paper shows reflection helps believability (human ratings). | Calls on importance and reflection. | ingest |
| A-MEM | [Xu2025-mem] | Zettelkasten-style notes with generated keywords, tags, links and memory evolution on insert. | Reported gains over MemGPT and MemoryBank on LoCoMo (third-party LoCoMo-Plus finds it best of three but still weak on cognitive items). | 2-3 calls per insert. | ingest |
| Zep / Graphiti temporal knowledge graph | [Rasmussen2025-zep], [Graphiti2024-graphiti] | Episodes, semantic entity graph, community subgraph, bi-temporal edges. | Vendor paper reports gains on LongMemEval; its LoCoMo result is disputed between vendors (protocol differences). | Several calls per episode. | ingest / retrieve |
| Session or episode summaries and gist memory | [Lee2024-readagent], [Pan2025-memory-construction-retrieval] | ReadAgent pages text into gist memories and looks up the original pages when needed; SeCom segments conversations into topical units before indexing and compresses them. | SeCom reports gains on LoCoMo and Long-MT-Bench+ over turn-level and session-level units; ReadAgent extends effective context 3-20x. | One call per segment. | ingest |
| Sliding-window recursive dialogue summaries | [Wang2023-recursive-dialogue-summary], [Wu2021-recursively-summarizing-books] | Summary of the previous summary plus the latest window; overlap keeps boundary facts. | Improves consistency in long-term dialogue (paper's own metrics); loses detail, so keep source links. | One call per window. | ingest |
| Reflective memory management | [Tan2025-prospect-retrospect] | Prospective reflection (topic-based summaries) plus retrospective reflection (re-rank via reader feedback, RL). | Reported gains on LongMemEval and MSC (more than 5% over no-management baseline). | Calls plus an RL-trained reranker adaptor. | ingest / retrieve |
| Think-in-Memory | [Liu2023-think-in-memory] | Store the model's own thoughts (conclusions) rather than raw history to avoid repeated reasoning. | Reported gains in dialogue consistency and recall (details not re-checked). | Calls to generate thoughts. | ingest |
| Memory-inspired global RAG | [Qian2024-memorag] | A light long-range model builds a global memory of the corpus and produces draft clues that steer retrieval. | Gains on tasks where the needed information is not stated in the query (details not re-checked). | Long-context model pass. | retrieve |

**Testable hypotheses for MemSpine.**
- `H-P06-a` Session-level summaries used as an extra retrieval leg that lifts back to turns (episode_leg_v2 plus LIB-11) recover the 'gold in sessions with no hit' loss (B8) better than widening the pool. Expected effect: +10 to +20 of the 68 stage-b recall misses; cost one summary call per session.
- `H-P06-b` Communities (GraphRAG-style) help only for global or theme questions, which are rare in LoCoMo cat 1-4; expect near-zero on LoCoMo and a gain on a global-question slice. Expected effect: LoCoMo +0 to +2; global slice +5 or more.

**Does MemSpine already have it?** Built, off. L2 session close (LIB-40, O4-02), `read.episode_leg_v2`, communities LIB-36 / `offline.communities_v2`, standing answers LIB-32, patterns LIB-31, graph legs and edge compiler, decay LIB-33, sleep summary arm gc9; screens S01, S31, S33, S34, S35, S40, S61, S64. Not present: a recursive cluster tree (RAPTOR) and a collapsed-tree retrieval over mixed levels.

**Open gap.** No recursive multi-level tree; most screens not yet run (flag-off). Priority: H.

---

## P07. Personal profile, attribute extraction and knowledge update
**Generic problem.** User facts change over time (job, city, preference). The store must track attribute values with history, decide supersede vs coexist vs contest, and answer both 'what is it now' and 'what was it then'. Persona extraction and slot tracking are older versions of the same problem.
**Link to LoCoMo problem classes (counts only).** Not a large LoCoMo class (cat 1-4 rarely test updates); central in LongMemEval knowledge-update, MemoryAgentBench conflict resolution and PrefEval. Gap I17: update handling untested in the harness.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Persona extraction from dialogue | [Zhang2018-personalizing-dialogue-agents], [Xu2022-beyond-goldfish-memory], [Long2022-long-time-no] | PersonaChat profile sentences; MSC with summarised memory; LD-SDM style long-term persona memory with extractor and selector. | Persona memory improves consistency and engagement in multi-session chat (human and automatic metrics in each paper). | One extraction per turn or session. | ingest |
| Dialogue state tracking as slot tracking | [Wu2019-trade], [Heck2020-trippy], [Hu2022-ic-dst] | Maintain slot-value pairs across turns by copy/generate (TRADE), copy from earlier turns (TripPy), or few-shot LLM in-context DST. | Joint goal accuracy gains of different sizes on MultiWOZ; IC-DST shows few-shot LLM DST is competitive (numbers not re-checked). | Per-turn extraction. | ingest |
| LLM memory managers with add/update/delete | [Chhikara2025-mem0], [Yan2025-memory-r1] | Mem0: LLM decides ADD / UPDATE / DELETE / NOOP against retrieved neighbours; Memory-R1 trains the manager with RL. | Mem0 reports better LLM-as-judge accuracy than several memory systems and lower latency than full-context (vendor-run protocol); Memory-R1 reports gains with very little training data. | 1-2 calls per write. | ingest |
| Bi-temporal supersede and invalidation | [Rasmussen2025-zep] | Mark an old edge invalid at the event time of the new fact rather than deleting it. | Supports as-of questions; reported on LongMemEval knowledge-update (vendor protocol). | Edge comparison per write. | ingest |
| Knowledge-update and conflict-resolution benchmarks | [Wu2024-longmemeval], [Memoryagentbench2025-memoryagentbench], [Chen2025-halumem] | LongMemEval knowledge-update type; MemoryAgentBench conflict resolution (single- and multi-hop contradictions); HaluMem operation-level hallucination (extraction, update, QA). | MemoryAgentBench finds all tested methods poor on multi-hop conflict resolution (as published; many near or below 7% on its multi-hop setting, approx.); HaluMem finds extraction and update hallucinations compound. | Evaluation. | judge |
| Profile hierarchy and EMA drift | [Tan2025-prospect-retrospect], [Wang2025-mirix] | Typed memory (core, episodic, semantic, procedural, resource, vault) with a profile block updated by an agent. | MIRIX reports gains on LoCoMo and a multimodal benchmark (vendor-paper protocol). | Multi-agent calls. | ingest |

**Testable hypotheses for MemSpine.**
- `H-P07-a` Event-time-ordered supersede with the cardinality registry (LIB-02) yields a correct current value and correct history on a knowledge-update slice without reader changes. Expected effect: Knowledge-update accuracy +10 to +20 points on a held-out slice relative to latest-turn-wins; zero change on LoCoMo.
- `H-P07-b` A 'latest-wins plus question-date' clause (I3) is needed in the reader prompt, otherwise stale values retrieved alongside new ones are chosen about as often as new ones. Expected effect: Stale-value rate halves on a conflict slice.

**Does MemSpine already have it?** Built, off, unmeasured. LIB-02 `derive/integrate.py` (X-05 contradictions 44/44), LIB-20 value histories, LIB-37 reconcile sweep, LIB-39 profile card (opt-in), LIB-22 time views. Gap I17: harness writes all turns as episodic; no stale-vs-current metric outside MemoryAgentBench. I18: no preference-following reader prompt or adherence metric.

**Open gap.** Update handling has no screen on LongMemEval-style or MAB-CR data (LongMemEval excluded from suite by owner decision; MAB-CR is the proxy). Priority: M.

---

## P08. Reader inference vs refusal; calibrated abstention; context use
**Generic problem.** Given retrieved evidence the reader must (a) infer when the answer is implied, (b) abstain when it is not there, and (c) use long or noisy context without being misled by position or distractors. Over-refusal wastes correct evidence; over-answering produces hallucination, and the benchmark category that tests it (LoCoMo cat 5) is the one our reference run never measured.
**Link to LoCoMo problem classes (counts only).** Classes `inference_open_domain` (26), `refusal_premise_denial` (14), `wrong_detail` (46), `distractor_line` (3), `image_caption_misuse` (6). Gaps I1 (refusal retry forces an answer), I2 (cat 5, 446 questions, never measured), I3 (anti-abstention prompt).
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Abstention survey and taxonomy | [Wen2024-abstention-survey], [Rajpurkar2018-squad2] | Abstention viewed from query, model and human-value angles; SQuAD 2.0 introduced unanswerable questions. | Surveys report that models over-answer unanswerable items; calibrated thresholds trade coverage for accuracy. | Evaluation design. | reader / judge |
| Self-knowledge and calibration | [Kadavath2022-know-what-they-know] | Models can estimate P(True) and P(IK) (probability that they know) with reasonable calibration. | Calibration improves with scale; useful as an abstain signal. | One extra forward or short generation. | reader |
| Sufficient context | [Joren2024-sufficient-context] | Classify whether the retrieved context is sufficient to answer; combine with model confidence for selective generation. | Shows models often answer instead of abstaining when context is insufficient; the combined signal improves the share of correct answers among answered by roughly 2-10% (approx.). | One classifier call (an autorater) per query. | reader / compose |
| Robustness to irrelevant context | [Yoran2023-retrobust], [Cuconasu2024-power-of-noise] | Fine-tune the reader to ignore irrelevant passages (RetRobust); study which passages hurt (adversarial, near-miss) and which are harmless. | RetRobust: reader keeps accuracy under irrelevant context. Power of Noise: related-but-wrong passages hurt most; random passages can even raise accuracy (reported up to about 35%, approx.). | Fine-tuning data, or a filter before the reader. | reader / compose |
| Note-taking before answering | [Yu2023-chain-of-note] | Reader writes a short note per passage (relevant, partly relevant, none) then answers or says unknown. | Reported gains on noisy retrieval and more 'unknown' when evidence is absent. | Longer output. | reader |
| Corrective and self-reflective retrieval | [Yan2024-crag], [Asai2024-self-rag] | A lightweight evaluator scores retrieved documents and triggers re-retrieval or filtering (CRAG); reflection tokens (Self-RAG). | Gains over plain RAG on short and long-form generation. | One evaluator pass. | retrieve / reader |
| Lost in the middle and evidence ordering | [Liu2024-lost-middle], [Hsieh2024-found-in-middle], [Peysakhovich2023-attention-sorting] | Accuracy is U-shaped in position; calibrate attention bias or sort passages by attention so the key ones sit at edges. | Liu: large drop when evidence is in the middle (over 20 points in multi-document QA, approx.); Found in the Middle: up to about 15 points recovered on a position-sensitive task (approx.). | Zero for reorder; a pass for attention sorting. | compose |
| Context compression | [Jiang2023-llmlingua], [Jiang2023-longllmlingua], [Xu2023-recomp] | Token-level pruning by a small LM (LLMLingua), question-aware compression that also reorders (LongLLMLingua), abstractive or extractive summaries trained for the reader (RECOMP). | LLMLingua: up to 20x compression with small loss; LongLLMLingua: improved accuracy at about 4x fewer tokens on long-context QA (approx.); RECOMP: about 6% of tokens kept with little loss on its tasks. | A small model pass per query; lossy for exact details (dates, names). | compose |
| Answer-type contract and verification | [Press2023-self-ask], (ours: LIB-16 contracts, verify-answer) | State answer shape (when, list, count, yes/no, free text) from the question and check the output against it. | No isolated published number on memory QA found; ours: typed query contract rules. | Zero (rules) to one call (verify). | reader |

**Testable hypotheses for MemSpine.**
- `H-P08-a` A sufficiency-gated answer policy ('answer / infer / abstain') replacing the single anti-abstention prompt raises cat 5 accuracy a lot and costs few cat 1-4 points, because the refusal retry (I1) currently forces answers. Expected effect: Cat 5 (446 q): large gain from a near-zero base if abstention is currently ~0; cat 1-4: -0 to -1 point. Must be measured, since cat 5 was never run.
- `H-P08-b` Hits-first ordering with neighbours after (`read.present_order`) and a 'most relevant at the edges' placement reduce `distractor_line` and `wrong_detail` errors. Expected effect: +3 to +8 of 49 questions; zero cost.
- `H-P08-c` For open-domain inference, a separate inference route (commit to yes / no / likely with the supporting line) beats a bigger reader on the 26 `inference_open_domain` questions that have all gold in context. Expected effect: +3 to +8 of 26; one prompt branch.

**Does MemSpine already have it?** Partly. LIB-12 `read/gate.py` (answerable / missing / conflicting / weak), LIB-16 contract rules, `read.present_order`, reader prompt variants (dated_infer, grounded_detail, routed_generic), harness `--verify-answer`, LLMLingua as a compose fallback (`compose/compression.py`), LIB-06 exact budget, LIB-43 sufficiency check. Gaps I1, I2, I3 open; the world-knowledge yes/no rule was tested and rejected (C3).

**Open gap.** Cat 5 never measured; no calibrated abstention policy; no answer-style/contract check on output. Priority: H.

---

## P09. Retrieval fusion and reranking
**Generic problem.** Several retrievers (BM25, dense, entity, temporal, graph) each see part of the gold; combining them loses items at the cut. A reranker can recover precision but also delete good items when it sees too small a pool. Pool depth, fusion weights and reranker input are coupled.
**Link to LoCoMo problem classes (counts only).** Stage `cf` cut at fusion/pool: 43 wrong; `fusion_loss_lexical_only` 12, `fusion_loss_vector_only` 6; `cr` rerank_keep cut 4; B7 237 gold turns cut at top-10; B9 reranker deletes hits.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Reciprocal rank fusion | [Cormack2009-rrf] | Score = sum over lists of 1/(k + rank), k=60 in the paper; no score calibration needed. | Outperformed Condorcet and learned rank fusion on TREC sets; robust baseline. | Zero. | retrieve |
| Convex combination vs RRF | [Bruch2023-fusion-functions] | Analysis of fusion functions for lexical + semantic hybrid; compare normalised convex combination with RRF. | Convex combination (with min-max or similar normalisation) beats RRF in and out of domain and needs few labelled queries to tune; RRF is sensitive to its k parameter. | A small tuning set. | retrieve |
| Hybrid BM25 + dense as a baseline | [Thakur2021-beir] | BEIR: BM25 is a strong zero-shot baseline; late interaction and rerankers lead on average at higher cost. | BM25 beats many dense models out of domain; reranking and late interaction best on average. | Evaluation. | retrieve |
| Late interaction (ColBERT, ColBERTv2) | [Khattab2020-colbert], [Santhanam2022-colbertv2] | Per-token vectors, MaxSim scoring; v2 compresses residuals and denoises supervision. | Higher out-of-domain accuracy than single-vector dense at index-size cost (v2 reduces footprint several-fold). | Large index; special engine support. | retrieve |
| Multi-functional embedding (dense + sparse + multi-vector) | [Chen2024-bge-m3] | One model emits all three; combine scores. | Reports best results when the three are combined on multilingual retrieval. | One encoder; larger index. | retrieve |
| Cross-encoder rerankers | [Nogueira2019-monobert], [Nogueira2020-monot5] | Score (query, passage) jointly; monoT5 treats it as seq2seq with a relevance token. | Large gains over first-stage retrieval on MS MARCO and BEIR; gains shrink if the first stage already has the item in a tiny pool. | Linear in pool depth. | retrieve |
| LLM listwise and setwise reranking | [Sun2023-rankgpt], [Pradeep2023-rankzephyr], [Zhuang2024-setwise] | Prompt an LLM to order a window of passages; distil into open models; setwise cuts the number of calls. | Zero-shot LLM rerankers match or beat supervised ones on several sets (RankGPT with GPT-4); setwise much cheaper than pairwise/listwise. | Many LLM tokens. | retrieve |
| Memory-specific reasoning-aware reranking | [Li2026-memreranker] | Reranker trained for agent-memory retrieval (temporal, multi-hop reasoning cues). | Preprint; reported gains over general rerankers on memory benchmarks (not verified here). | A trained reranker. | retrieve |
| Protected pool and family-level RRF | (ours: LIB-10) | Reserve slots per retrieval family so a strong family cannot crowd out a weak but unique one. | Ours: screened; see screens S16, S60, S61, S67. | Zero. | retrieve |

**Testable hypotheses for MemSpine.**
- `H-P09-a` Family-level fusion with a protected pool plus a reranker that sees a pool of 30 (not the fused top-10) removes most `cf` and `cr` losses, because the reranker can then promote, not only delete (B10). Expected effect: Of 47 cf+cr losses, recover 10 to 20; cost: reranker latency linear in pool.
- `H-P09-b` A convex combination with a small calibrated weight beats RRF k=60 on this memory pool, as Bruch et al. report on BEIR. Expected effect: +0.5 to +1.5 points recall at pool 30; screen as retrieval-only (free).
- `H-P09-c` Late interaction (a ColBERT leg) is the one candidate that helps vocabulary-mismatch and lexical-outranked cases without LLM calls; the cost is index size and engine support. Expected effect: +1 to +3 recall points at pool 30; high integration cost, so gate behind a small offline test.

**Does MemSpine already have it?** Mostly built. LIB-10 `read/fusion.py` (family RRF, protected pool, percentile floors), `read.rerank_v2` + `rerank_v2_search_pool=30` + `rerank_keep`, jina reranker v3.5 in the reference run, LIB-42 reranker chain, `candidate_pool`, B9-B13 audits, `read.word_vector_leg`. Not present: convex-combination fusion, late interaction / multi-vector, learned sparse.

**Open gap.** No learned fusion; no late-interaction or SPLADE leg; reranker adopted on coverage (I9). Priority: M.

---

## P10. Multimodal (image caption) memory; tool and world-knowledge augmentation
**Generic problem.** Photos arrive as captions or not at all; the caption is a machine description that can be misread as something a speaker said. Some questions need public knowledge that is not in the conversation (the open-domain category); the question is when to fetch it, how to label it, and how to keep it out of the user's own memory.
**Link to LoCoMo problem classes (counts only).** Classes `image_caption_misuse` (6), `open_domain_inference_retrieval` (28), `inference_open_domain` (26), three photo-only gold items counted as gold errors (A5).
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| Caption models as the image-to-text bridge | [Li2023-blip2] | Frozen image encoder + LLM bridge gives captions or answers about images. | LoCoMo itself ships BLIP-style captions; the quality of the caption bounds what text memory can answer. | A vision model at write. | ingest |
| Retrieval over images and text jointly | [Chen2022-murag] | MuRAG: shared multimodal memory of images and text retrieved by a joint encoder. | Large gains over text-only on WebQA and MultimodalQA (as published). | Multimodal encoder and index. | retrieve |
| Multi-type agent memory with a resource store | [Wang2025-mirix] | Separate stores for text, screenshots and files. | Reports gains on a screenshot memory QA set (vendor-paper protocol). | Multi-agent calls. | ingest |
| Tool use learned by the model | [Schick2023-toolformer], [Yao2023-react] | Toolformer: self-supervised insertion of API calls; ReAct: interleaved reasoning and tool actions. | Improved zero-shot on QA/maths with search and calculator. | Calls per tool use. | reader |
| Search-augmented freshness | [Vu2023-freshllms] | FreshPrompt puts search-engine results with dates into the prompt, ordered by recency. | Large gains on fast-changing questions (FreshQA). | A search call per query. | reader |
| Foundational RAG | [Gao2020-retrieval-augmented-generation] | Non-parametric memory from an external corpus combined with a parametric generator. | Improves knowledge-intensive tasks. | Index. | retrieve |
| Labelled external-knowledge block | (ours: LIB-45 `read/external.py`) | Opt-in public knowledge in a separate, labelled block with trust cap and deposit guard. | Not screened; world-knowledge clause tested in C3. | Retrieval or model call; no deposit into user memory. | compose |

**Testable hypotheses for MemSpine.**
- `H-P10-a` Rendering a caption as its own 'Photo shows: ...' line, not as speech, plus the prompt rule, reduces `image_caption_misuse`. Expected effect: +3 to +5 of 6 (C7 'tested, kept in grounded prompt'; effect on the full set not isolated).
- `H-P10-b` For `inference_open_domain`, the missing piece is reader reasoning, not external knowledge; an external block should be net-neutral on LoCoMo and positive only for freshness-type questions. Expected effect: LoCoMo: -0 to +2; fresh-fact slice: positive.

**Does MemSpine already have it?** Partly. Caption rule in `grounded_detail` (C7), LIB-45 external-knowledge library (opt-in), world-knowledge clause tested and rejected in its yes/no form (C3). No image encoder path, which is by design (captions only).

**Open gap.** No image memory; external block unscreened. Priority: L.

---

## P11. LLM-as-judge reliability for memory QA
**Generic problem.** Scores come from a judge model; its biases (position, verbosity, self-preference), its false negatives on paraphrase and date forms, and its false positives on partial lists change the ranking of systems. The judge is also usually the reader model here (A1).
**Link to LoCoMo problem classes (counts only).** Classes `judge_false_negative` (6), `relative_date_gold_vs_judge` (1), and A2 (24-50 false negatives, 1.6-3.2 points), A3 (45-60 false positives), A4 (home-made judge prompt), I12-I13.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| MT-Bench judge study | [Zheng2023-judging-llm-judge] | Documents position, verbosity and self-enhancement bias and limited maths ability; strong judges agree with humans over 80%. | Over 80% agreement with human preferences for GPT-4 judges; mitigations: swap positions, reference-guided grading. | Extra judge calls for swaps. | judge |
| Position bias in evaluators | [Wang2023-unfair-evaluators] | Order of candidates flips verdicts; calibration by evidence-first reasoning, multiple orderings, and human-in-the-loop. | Shows large ranking shifts from order alone. | Several calls. | judge |
| Rubric-based scoring (G-Eval) | [Liu2023-geval] | Chain-of-thought rubric steps and probability-weighted score. | Higher human correlation than prior metrics on summarisation. | One call with CoT. | judge |
| Panel of judges (PoLL) | [Verga2024-poll] | Pool several smaller models from different families. | Higher agreement with humans than a single large judge and lower cost (about 7x cheaper, approx.), less intra-model bias. | Several small calls. | judge |
| Judge vulnerabilities and agreement measurement | [Thakur2024-judging-the-judges] | Compares many judges with Cohen kappa vs percent agreement; shows leniency and prompt sensitivity, and that percent agreement hides differences. | Reports that only the strongest judges are near human-level and that scores shift with prompt details. | Evaluation. | judge |
| Survey of judge practice | [Li2024-generation-to-judgment] | Taxonomy of judge design, biases and mitigations. | Overview (no single effect). | None. | judge |
| Lexical match under-counts correct open answers | [Kamalloo2023-eval-odqa] | Evaluation of open-domain QA: lexical match (EM / F1) rejects many valid answers; model-based and human checks show much higher accuracy. | Reports that a large share of lexical-match failures are in fact correct; recommends model-based judging. | Judge call. | judge |
| Learned answer-equivalence metrics | [Bulian2022-tomayto], [Chen2020-mocha] | BEM / learned metrics trained on human equivalence judgements beat token overlap. | Higher correlation with human equivalence than F1 or EM. | Small encoder. | judge |
| Atomic fact checks | [Min2023-factscore] | Split an answer into atomic facts and check each against a source. | Fine-grained precision for long answers; useful for list answers (partial credit). | Calls per fact. | judge |
| Deterministic pre-judge for dates and normalisation | (ours, A2) | Weekday, date and number normalisation before the judge; list recall/precision per item. | Ours: 7 of 20 exact resolved dates were rejected against relative-form gold (A2). | Zero. | judge |

**Testable hypotheses for MemSpine.**
- `H-P11-a` A deterministic pre-judge (date normalisation, number words, list per-item match) removes most of the 6 judge false negatives with no model call and no false positives. Expected effect: +4 to +6 questions on the same answers; accuracy +0.3 to +0.4 points; rerun is judge-only.
- `H-P11-b` A second judge from a different model family (PoLL-style, two small judges plus a tie-break) changes ranking only inside the noise band of 2.2 points; use it to report an agreement rate, not to change the headline. Expected effect: Cohen kappa vs the single judge is high (above 0.85) on non-list answers; lower on lists.
- `H-P11-c` Judge = reader (A1) inflates agreement on shared failure modes (weekday arithmetic, long lists); a different judge family exposes them. Expected effect: Expect 1 to 3 flips in either direction per 100 questions.

**Does MemSpine already have it?** Partly. Errata file (A5), deterministic date checks and guarded rubric (A2-A4, I12), judge-only rerun path. Not done: second-family judge or panel, kappa on benchmarks other than LoCoMo (I13), official-judge reproduction (A4).

**Open gap.** Single judge family equal to reader; kappa not measured outside LoCoMo. Priority: M.

---

## P12. Memory benchmark methodology: what leading systems report and how
**Generic problem.** Reported scores are not comparable: different judges, different category sets (cat 5 often dropped), different answer models, different retrieval budgets, and different data cuts. A system comparison needs the protocol next to the number.
**Link to LoCoMo problem classes (counts only).** Gaps A4, A10, A12, A13, I2, I14-I16. Our reference: 81.4% on cat 1-4 (1,540 questions) with a local 9B reader and judge; question-sampling CI +-2.2 points.
**Techniques.**
| technique | paper(s) | mechanism | reported effect | cost | fits MemSpine where |
|---|---|---|---|---|---|
| LoCoMo | [Maharana2024-locomo] | 10 very long dialogues (about 300 turns, 9K tokens avg per the paper, up to 35 sessions), QA categories single-hop, multi-hop, temporal, open-domain, adversarial; event-summarisation and multimodal tasks. | Original paper: long-context and RAG LLMs lag humans by a wide margin, especially on temporal reasoning. Systems commonly report cat 1-4 only. | Public. | judge |
| LongMemEval | [Wu2024-longmemeval] | 500 questions over scalable chat histories with five abilities (extraction, multi-session reasoning, temporal reasoning, knowledge update, abstention). | Commercial assistants and long-context LLMs drop sharply (about 30-60% accuracy drop reported); proposes session decomposition, fact-augmented keys and time-aware query expansion. | Public; excluded from our suite by owner decision. | judge |
| LoCoMo-Plus | [Li2026-locomo-plus] | Adds cue-trigger 'cognitive' items where the link between cue and trigger is implicit. | Memory systems score about 10-26% on the cognitive part while scoring higher on factual LoCoMo (e.g. Mem0 57.24 LoCoMo avg vs 15.80 cognitive per our research note). | Public. | judge |
| MemoryAgentBench | [Memoryagentbench2025-memoryagentbench] | Four competencies: accurate retrieval, test-time learning, long-range understanding, conflict resolution, built from incremental multi-turn interactions. | No tested method masters all four; conflict resolution is weakest. | Public. | judge |
| PrefEval | [Zhao2025-prefeval] | Preference following over long, inserted-conversation contexts; explicit, implicit, and persona forms. | Reported that preference following degrades with context length for many LLMs (figures not re-checked). | Public. | judge |
| OP-Bench | [Hu2026-op-bench] | Over-personalisation: irrelevant, baiting, repetition, and memory-sycophancy cases. | Memory systems lower the average versus no-memory (BASE 73.8 vs Mem0 42.12, MemU 35.09, MemOS 37.17 with Qwen3-8B per our SOTA note). | Public. | judge |
| ConvoMem | [Pakhomov2025-convomem-benchmark] | Early-conversation memory benchmark; argues the first 150 conversations do not need RAG. | Full-context beats memory systems at small history size. | Public. | judge |
| What vendors report and how | [Mem02026-ai-memory-benchmarks], [Chhikara2025-mem0], [Rasmussen2025-zep], [Wang2025-mirix], [Memos2025-memos] | Vendor blogs and papers report LoCoMo cat 1-4 by an LLM judge; protocols, judge prompts and answer models differ; some rerun competitors with their own settings. | Range seen in our research notes: 51.67 to 94.4 on LoCoMo across incompatible protocols (see `paper/research_notes/systems_2026.md`); Zep and Mem0 disputes are vendor-to-vendor. | None. | judge |
| Hallucination at the operation level | [Chen2025-halumem] | Reports extraction, update and QA hallucination separately. | Hallucinations in extraction and update propagate to QA. | Public. | judge |

**Testable hypotheses for MemSpine.**
- `H-P12-a` Reporting each system with (judge, category set, reader, retrieval budget, CI) in one table, plus our run under the official judge prompt, removes the apparent gaps between systems that come from protocol. Expected effect: Not a score effect; reduces the spread of published numbers to the CI.
- `H-P12-b` Cat 5 results should be reported as a separate abstention metric (A13): precision of refusals and answer rate on answerable items. Expected effect: Makes I1/I3 trade-off visible; headline accuracy on cat 1-4 unchanged.

**Does MemSpine already have it?** Built. Suite fixed by the owner (LoCoMo + cat 5, OP-Bench, MAB-CR, ConvoMem, PrefEval; no LongMemEval), adoption rule average-led. Gaps: cat 5 unmeasured (I2), A4 official judge, I14 gold audit for non-LoCoMo sets, I15 splits.

**Open gap.** Cat 5 and official-judge reproduction. Priority: M.

---

## References
### R14-new (added in this round)
- **[Anthropic2024-contextual-retrieval]** `R14-new` Anthropic (2024). Introducing Contextual Retrieval. Engineering blog, Sept 2024 (vendor report, not peer reviewed). https://www.anthropic.com/news/contextual-retrieval
- **[Bruch2023-fusion-functions]** `R14-new` Bruch, Gai, Ingber (2023). An Analysis of Fusion Functions for Hybrid Retrieval. ACM TOIS. https://arxiv.org/abs/2210.11934
- **[Bulian2022-tomayto]** `R14-new` Bulian, Buck, Gajewski, Borschinger, Schuster (2022). Tomayto, Tomahto. Beyond Token-level Answer Equivalence for Question Answering Evaluation. EMNLP. https://arxiv.org/abs/2202.07654
- **[Chang2012-sutime]** `R14-new` Chang, Manning (2012). SUTime: A Library for Recognizing and Normalizing Time Expressions. LREC (from memory, no URL checked). https://nlp.stanford.edu/pubs/lrec2012-sutime.pdf
- **[Chen2020-mocha]** `R14-new` Chen, Stanovsky, Singh, Gardner (2020). MOCHA: A Dataset for Training and Evaluating Generative Reading Comprehension Metrics. EMNLP. https://arxiv.org/abs/2010.03636
- **[Chen2021-timeqa]** `R14-new` Chen, Wang, Wang (2021). A Dataset for Answering Time-Sensitive Questions (TimeQA). NeurIPS Datasets and Benchmarks. https://arxiv.org/abs/2108.06314
- **[Chen2022-murag]** `R14-new` Chen, Hu, Chang, Cohen, et al. (2022). MuRAG: Multimodal Retrieval-Augmented Generator for Open Question Answering over Images and Text. EMNLP. https://arxiv.org/abs/2210.02928
- **[Chen2022-pot]** `R14-new` Chen, Ma, Wang, Cohen (2022). Program of Thoughts Prompting: Disentangling Computation from Reasoning for Numerical Reasoning Tasks. TMLR. https://arxiv.org/abs/2211.12588
- **[Chen2023-dense-x]** `R14-new` Chen, Zhang, Zhang, et al. (2023). Dense X Retrieval: What Retrieval Granularity Should We Use? EMNLP 2024. https://arxiv.org/abs/2312.06648
- **[Chen2024-bge-m3]** `R14-new` Chen, Xiao, Zhang, Luo, Lian, Liu (2024). M3-Embedding (BGE-M3): Multi-Linguality, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation. https://arxiv.org/abs/2402.03216
- **[Choi2021-decontextualization]** `R14-new` Choi, Palomaki, Reddy, et al. (2021). Decontextualization: Making Sentences Stand-Alone. TACL 9. https://arxiv.org/abs/2102.05169
- **[Cormack2009-rrf]** `R14-new` Cormack, Clarke, Buettcher (2009). Reciprocal Rank Fusion outperforms Condorcet and individual rank learning methods. SIGIR. DOI 10.1145/1571941.1572114 (from memory, DOI not machine-checked). https://doi.org/10.1145/1571941.1572114
- **[Cuconasu2024-power-of-noise]** `R14-new` Cuconasu, Trappolini, Siciliano, et al. (2024). The Power of Noise: Redefining Retrieval for RAG Systems. SIGIR. https://arxiv.org/abs/2401.14887
- **[DeCao2021-genre]** `R14-new` De Cao, Izacard, Riedel, Petroni (2021). Autoregressive Entity Retrieval (GENRE). ICLR. https://arxiv.org/abs/2010.00904
- **[Dhingra2022-time-aware-lm]** `R14-new` Dhingra, Cole, Eisenschlos, et al. (2022). Time-Aware Language Models as Temporal Knowledge Bases. TACL. https://arxiv.org/abs/2106.15110
- **[Fatemi2024-test-of-time]** `R14-new` Fatemi, Kazemi, Tsitsulin, et al. (2024). Test of Time: A Benchmark for Evaluating LLMs on Temporal Reasoning. https://arxiv.org/abs/2406.09170
- **[Formal2021-splade]** `R14-new` Formal, Piwowarski, Clinchant (2021). SPLADE: Sparse Lexical and Expansion Model for First Stage Ranking. SIGIR. https://arxiv.org/abs/2107.05720
- **[Formal2021-splade-v2]** `R14-new` Formal, Lassance, Piwowarski, Clinchant (2021). SPLADE v2: Sparse Lexical and Expansion Model for Information Retrieval. https://arxiv.org/abs/2109.10086
- **[Gade2024-tempralm]** `R14-new` Gade, Jetcheva (2024). It's About Time: Incorporating Temporality in Retrieval Augmented Language Models (TempRALM). https://arxiv.org/abs/2401.13222
- **[Gao2022-hyde]** `R14-new` Gao, Ma, Lin, Callan (2023). Precise Zero-Shot Dense Retrieval without Relevance Labels (HyDE). ACL. https://arxiv.org/abs/2212.10496
- **[Gao2023-pal]** `R14-new` Gao, Madaan, Zhou, et al. (2023). PAL: Program-aided Language Models. ICML. https://arxiv.org/abs/2211.10435
- **[Ge2025-tremu]** `R14-new` Ge, Han, et al. (2025). TReMu: Towards Neuro-Symbolic Temporal Reasoning for LLM-Agents with Memory in Multi-Session Dialogues. ACL Findings 2025 (venue not re-checked). https://arxiv.org/abs/2502.01630
- **[Gospodinov2023-doc2query-minus]** `R14-new` Gospodinov, MacAvaney, Macdonald (2023). Doc2Query--: When Less is More. ECIR. https://arxiv.org/abs/2301.03266
- **[Gunther2024-late-chunking]** `R14-new` Gunther, Mohr, Williams, Wang, Xiao (2024). Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models. https://arxiv.org/abs/2409.04701
- **[Guo2024-lightrag]** `R14-new` Guo, Xia, Yu, Ao, Huang (2024). LightRAG: Simple and Fast Retrieval-Augmented Generation. https://arxiv.org/abs/2410.05779
- **[Heck2020-trippy]** `R14-new` Heck, van Niekerk, Lubis, et al. (2020). TripPy: A Triple Copy Strategy for Value Independent Neural Dialog State Tracking. SIGDIAL. https://arxiv.org/abs/2005.02877
- **[Hsieh2024-found-in-middle]** `R14-new` Hsieh, Chuang, Li, et al. (2024). Found in the Middle: Calibrating Positional Attention Bias Improves Long Context Utilization. ACL Findings. https://arxiv.org/abs/2406.16008
- **[Hu2022-ic-dst]** `R14-new` Hu, Lee, Xie, et al. (2022). In-Context Learning for Few-Shot Dialogue State Tracking. EMNLP Findings. https://arxiv.org/abs/2203.08568
- **[Jagerman2023-query-expansion-prompting]** `R14-new` Jagerman, Zhuang, Qin, Wang, Bendersky (2023). Query Expansion by Prompting Large Language Models. https://arxiv.org/abs/2305.03653
- **[Joko2021-conv-entity-linking]** `R14-new` Joko, Hasibi, Balog, de Vries (2021). Conversational Entity Linking: Problem Definition and Datasets. SIGIR. https://arxiv.org/abs/2105.04903
- **[Joren2024-sufficient-context]** `R14-new` Joren, Zhang, Ferng, et al. (2024). Sufficient Context: A New Lens on Retrieval Augmented Generation Systems. ICLR 2025. https://arxiv.org/abs/2411.06037
- **[Kadavath2022-know-what-they-know]** `R14-new` Kadavath, Conerly, Askell, et al. (2022). Language Models (Mostly) Know What They Know. https://arxiv.org/abs/2207.05221
- **[Kamalloo2023-eval-odqa]** `R14-new` Kamalloo, Dziri, Clarke, Rafiei (2023). Evaluating Open-Domain Question Answering in the Era of Large Language Models. ACL. https://arxiv.org/abs/2305.06984
- **[Khot2023-decomposed-prompting]** `R14-new` Khot, Trivedi, Finlayson, et al. (2023). Decomposed Prompting: A Modular Approach for Solving Complex Tasks. ICLR. https://arxiv.org/abs/2210.02406
- **[Le2023-llm-coref]** `R14-new` Le, Ritter (2023). Are Large Language Models Robust Coreference Resolvers? https://arxiv.org/abs/2305.14489
- **[Lee2017-e2e-coref]** `R14-new` Lee, He, Lewis, Zettlemoyer (2017). End-to-end Neural Coreference Resolution. EMNLP. https://arxiv.org/abs/1707.07045
- **[Lee2024-readagent]** `R14-new` Lee, Chen, et al. (2024). A Human-Inspired Reading Agent with Gist Memory of Very Long Contexts (ReadAgent). ICML. https://arxiv.org/abs/2402.09727
- **[Lei2024-csqe]** `R14-new` Lei, Cao, Zhou, et al. (2024). Corpus-Steered Query Expansion with Large Language Models. EACL. https://arxiv.org/abs/2402.18031
- **[Li2023-blip2]** `R14-new` Li, Li, Savarese, Hoi (2023). BLIP-2: Bootstrapping Language-Image Pre-training with Frozen Image Encoders and Large Language Models. ICML. https://arxiv.org/abs/2301.12597
- **[Li2024-generation-to-judgment]** `R14-new` Li, Jiang, et al. (2024). From Generation to Judgment: Opportunities and Challenges of LLM-as-a-judge. https://arxiv.org/abs/2411.16594
- **[Liu2023-geval]** `R14-new` Liu, Iter, Xu, et al. (2023). G-Eval: NLG Evaluation using GPT-4 with Better Human Alignment. EMNLP. https://arxiv.org/abs/2303.16634
- **[Liu2023-think-in-memory]** `R14-new` Liu, Lai, et al. (2023). Think-in-Memory: Recalling and Post-thinking Enable LLMs with Long-Term Memory. https://arxiv.org/abs/2311.08719
- **[Ma2023-rewrite-retrieve-read]** `R14-new` Ma, Gong, He, Zhao, Duan (2023). Query Rewriting for Retrieval-Augmented Large Language Models. EMNLP. https://arxiv.org/abs/2305.14283
- **[Mackie2023-grf]** `R14-new` Mackie, Chatterjee, Dalton (2023). Generative Relevance Feedback with Large Language Models. SIGIR. https://arxiv.org/abs/2304.13157
- **[Malaviya2023-quest]** `R14-new` Malaviya, Shaw, Chang, Toutanova (2023). QUEST: A Retrieval Dataset of Entity-Seeking Queries with Implicit Set Operations. ACL. https://arxiv.org/abs/2305.11694
- **[Mao2023-llm4cs]** `R14-new` Mao, Dou, Chen, Qian, Zhu (2023). Large Language Models Know Your Contextual Search Intent: A Prompting Framework for Conversational Search. EMNLP Findings. https://arxiv.org/abs/2303.06573
- **[Min2023-factscore]** `R14-new` Min, Krishna, Lyu, et al. (2023). FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation. EMNLP. https://arxiv.org/abs/2305.14251
- **[Mo2023-convgqr]** `R14-new` Mo, Mao, Dou, et al. (2023). ConvGQR: Generative Query Reformulation for Conversational Search. ACL. https://arxiv.org/abs/2305.15645
- **[Nogueira2019-doc2query]** `R14-new` Nogueira, Yang, Lin, Cho (2019). Document Expansion by Query Prediction (doc2query). https://arxiv.org/abs/1904.08375
- **[Nogueira2019-monobert]** `R14-new` Nogueira, Cho (2019). Passage Re-ranking with BERT. https://arxiv.org/abs/1901.04085
- **[Nogueira2020-monot5]** `R14-new` Nogueira, Jiang, Pradeep, Lin (2020). Document Ranking with a Pretrained Sequence-to-Sequence Model (monoT5). EMNLP Findings. https://arxiv.org/abs/2003.06713
- **[Peeters2023-llm-entity-matching]** `R14-new` Peeters, Bizer (2023). Entity Matching using Large Language Models. https://arxiv.org/abs/2310.11244
- **[Peysakhovich2023-attention-sorting]** `R14-new` Peysakhovich, Lerer (2023). Attention Sorting Combats Recency Bias In Long Context Language Models. https://arxiv.org/abs/2310.01427
- **[Pradeep2023-rankzephyr]** `R14-new` Pradeep, Sharifymoghaddam, Lin (2023). RankZephyr: Effective and Robust Zero-Shot Listwise Reranking is a Breeze! https://arxiv.org/abs/2312.02724
- **[Press2023-self-ask]** `R14-new` Press, Zhang, Min, et al. (2023). Measuring and Narrowing the Compositionality Gap in Language Models (Self-Ask). EMNLP Findings. https://arxiv.org/abs/2210.03350
- **[Rackauckas2024-rag-fusion]** `R14-new` Rackauckas (2024). RAG-Fusion: a New Take on Retrieval-Augmented Generation. https://arxiv.org/abs/2402.03367
- **[Rajpurkar2018-squad2]** `R14-new` Rajpurkar, Jia, Liang (2018). Know What You Don't Know: Unanswerable Questions for SQuAD. ACL. https://arxiv.org/abs/1806.03822
- **[Santhanam2022-colbertv2]** `R14-new` Santhanam, Khattab, Saad-Falcon, Potts, Zaharia (2022). ColBERTv2: Effective and Efficient Retrieval via Lightweight Late Interaction. NAACL. https://arxiv.org/abs/2112.01488
- **[Sarthi2024-raptor]** `R14-new` Sarthi, Abdullah, Tuli, Khanna, Goldie, Manning (2024). RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval. ICLR. https://arxiv.org/abs/2401.18059
- **[Strotgen2010-heideltime]** `R14-new` Strotgen, Gertz (2010). HeidelTime: High Quality Rule-based Extraction and Normalization of Temporal Expressions. SemEval-2010 (from memory, no URL checked). https://aclanthology.org/S10-1071/
- **[Sun2023-rankgpt]** `R14-new` Sun, Yan, Ma, et al. (2023). Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents (RankGPT). EMNLP. https://arxiv.org/abs/2304.09542
- **[Tan2023-tempreason]** `R14-new` Tan, Ng, Bing (2023). Towards Benchmarking and Improving the Temporal Reasoning Capability of Large Language Models (TempReason). ACL. https://arxiv.org/abs/2306.08952
- **[Thakur2024-judging-the-judges]** `R14-new` Thakur, Choudhary, Ramayapally, Vaidyanathan, Hupkes (2024). Judging the Judges: Evaluating Alignment and Vulnerabilities in LLMs-as-Judges. https://arxiv.org/abs/2406.12624
- **[Toshniwal2020-coref-bounded-memory]** `R14-new` Toshniwal, Wiseman, Ettinger, Livescu, Gimpel (2020). Learning to Ignore: Long Document Coreference with Bounded Memory Neural Networks. EMNLP. https://arxiv.org/abs/2010.02807
- **[Trivedi2022-musique]** `R14-new` Trivedi, Balasubramanian, Khot, Sabharwal (2022). MuSiQue: Multihop Questions via Single-hop Question Composition. TACL. https://arxiv.org/abs/2108.00573
- **[Trivedi2023-ircot]** `R14-new` Trivedi, Balasubramanian, Khot, Sabharwal (2023). Interleaving Retrieval with Chain-of-Thought Reasoning for Knowledge-Intensive Multi-Step Questions (IRCoT). ACL. https://arxiv.org/abs/2212.10509
- **[Verga2024-poll]** `R14-new` Verga, Hofstatter, Althammer, et al. (2024). Replacing Judges with Juries: Evaluating LLM Generations with a Panel of Diverse Models (PoLL). https://arxiv.org/abs/2404.18796
- **[Vu2023-freshllms]** `R14-new` Vu, Iyyer, Wang, et al. (2023). FreshLLMs: Refreshing Large Language Models with Search Engine Augmentation (FreshPrompt). ACL Findings 2024. https://arxiv.org/abs/2310.03214
- **[Wang2023-query2doc]** `R14-new` Wang, Yang, Wei (2023). Query2doc: Query Expansion with Large Language Models. EMNLP. https://arxiv.org/abs/2303.07678
- **[Wang2023-recursive-dialogue-summary]** `R14-new` Wang, Zhang, Wang, et al. (2023). Recursively Summarizing Enables Long-Term Dialogue Memory in Large Language Models. https://arxiv.org/abs/2308.15022
- **[Wang2023-tram]** `R14-new` Wang, Zhao (2023). TRAM: Benchmarking Temporal Reasoning for Large Language Models. https://arxiv.org/abs/2310.00835
- **[Wang2023-unfair-evaluators]** `R14-new` Wang, Yu, Cai, et al. (2023). Large Language Models are not Fair Evaluators. ACL 2024. https://arxiv.org/abs/2305.17926
- **[Weller2023-expansion-fails]** `R14-new` Weller, Lo, Cohan, Van Durme, et al. (2023). When do Generative Query and Document Expansions Fail? A Comprehensive Study Across Methods, Retrievers, and Datasets. https://arxiv.org/abs/2309.08541
- **[Wen2024-abstention-survey]** `R14-new` Wen, Yao, Feng, et al. (2024/2025). Know Your Limits: A Survey of Abstention in Large Language Models. TACL. https://arxiv.org/abs/2407.18418
- **[Wu2019-trade]** `R14-new` Wu, Madotto, Hosseini-Asl, et al. (2019). Transferable Multi-Domain State Generator for Task-Oriented Dialogue Systems (TRADE). ACL. https://arxiv.org/abs/1905.08743
- **[Wu2020-blink]** `R14-new` Wu, Petroni, Josifoski, Riedel, Zettlemoyer (2020). Scalable Zero-shot Entity Linking with Dense Entity Retrieval (BLINK). EMNLP. https://arxiv.org/abs/1911.03814
- **[Xu2023-recomp]** `R14-new` Xu, Shi, Choi (2023). RECOMP: Improving Retrieval-Augmented LMs with Compression and Selective Augmentation. ICLR 2024. https://arxiv.org/abs/2310.04408
- **[Yan2024-crag]** `R14-new` Yan, Gu, Zhu, Ling (2024). Corrective Retrieval Augmented Generation (CRAG). https://arxiv.org/abs/2401.15884
- **[Yang2018-hotpotqa]** `R14-new` Yang, Qi, Zhang, et al. (2018). HotpotQA. EMNLP. https://arxiv.org/abs/1809.09600
- **[Yoran2023-retrobust]** `R14-new` Yoran, Wolfson, Ram, Berant (2023). Making Retrieval-Augmented Language Models Robust to Irrelevant Context. ICLR 2024. https://arxiv.org/abs/2310.01558
- **[Yu2020-conv-query-rewriting]** `R14-new` Yu, Liu, Xiong, et al. (2020). Few-Shot Generative Conversational Query Rewriting. SIGIR. https://arxiv.org/abs/2006.05009
- **[Yu2023-chain-of-note]** `R14-new` Yu, Zhang, Pan, et al. (2023). Chain-of-Note: Enhancing Robustness in Retrieval-Augmented Language Models. https://arxiv.org/abs/2311.09210
- **[Zhang2024-chain-of-agents]** `R14-new` Zhang, Chen, Arik, et al. (2024). Chain of Agents: Large Language Models Collaborating on Long-Context Tasks. NeurIPS. https://arxiv.org/abs/2406.02818
- **[Zheng2023-step-back]** `R14-new` Zheng, Mishra, Chen, et al. (2023). Take a Step Back: Evoking Reasoning via Abstraction in Large Language Models. ICLR 2024. https://arxiv.org/abs/2310.06117
- **[Zhong2024-mix-of-granularity]** `R14-new` Zhong, Liu, et al. (2024). Mix-of-Granularity: Optimize the Chunking Granularity for RAG. https://arxiv.org/abs/2406.00456
- **[Zhou2023-least-to-most]** `R14-new` Zhou, Scharli, Hou, et al. (2023). Least-to-Most Prompting Enables Complex Reasoning in Large Language Models. ICLR. https://arxiv.org/abs/2205.10625
- **[Zhuang2024-setwise]** `R14-new` Zhuang, Zhuang, Zuccon (2024). A Setwise Approach for Effective and Highly Efficient Zero-shot Ranking with Large Language Models. SIGIR. https://arxiv.org/abs/2310.09497

### Existing keys reused (from `_shared/references.md`)
- **[Asai2024-self-rag]** `existing` Asai et al. (2024). Self-RAG. ICLR. https://arxiv.org/abs/2310.11511
- **[Bae2024-hipporag]** `existing` Jimenez Gutierrez et al. (2024). HippoRAG (key is Bae2024-hipporag in the file). https://arxiv.org/abs/2405.14831
- **[Chen2025-halumem]** `existing` Chen et al. (2025). HaluMem. https://arxiv.org/abs/2511.03506
- **[Chhikara2025-mem0]** `existing` Chhikara et al. (2025). Mem0. https://arxiv.org/abs/2504.19413
- **[Edge2024-local-global]** `existing` Edge et al. (2024). From Local to Global: a Graph RAG approach. https://arxiv.org/abs/2404.16130
- **[Gao2020-retrieval-augmented-generation]** `existing` Lewis et al. (2020). Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks (key in file is Gao2020-..., a mis-key; content is the Lewis et al. RAG paper). https://arxiv.org/abs/2005.11401
- **[Graphiti2024-graphiti]** `existing` Graphiti (Zep docs).
- **[Gutierrez2025-hipporag2]** `existing` Gutierrez et al. (2025). HippoRAG 2.
- **[Hu2026-op-bench]** `existing` Hu et al. (2026). OP-Bench. https://arxiv.org/abs/2601.13722
- **[Jeong2024-adaptive-rag]** `existing` Jeong et al. (2024). Adaptive-RAG. https://arxiv.org/abs/2403.14403
- **[Jiang2023-active-retrieval-augmented]** `existing` Jiang et al. (2023). Active Retrieval Augmented Generation (FLARE). https://arxiv.org/abs/2305.06983
- **[Jiang2023-llmlingua]** `existing` Jiang et al. (2023). LLMLingua. https://arxiv.org/abs/2310.05736
- **[Jiang2023-longllmlingua]** `existing` Jiang et al. (2023). LongLLMLingua. https://arxiv.org/abs/2310.06839
- **[Khattab2020-colbert]** `existing` Khattab, Zaharia (2020). ColBERT. SIGIR. https://arxiv.org/abs/2004.12832
- **[Li2026-locomo-plus]** `existing` Li et al. (2026). LoCoMo-Plus. ACL 2026. https://arxiv.org/abs/2602.10715
- **[Li2026-memreranker]** `existing` Li et al. (2026). MemReranker. https://arxiv.org/abs/2605.06132
- **[Liu2024-lost-middle]** `existing` Liu et al. (2024). Lost in the Middle. TACL. https://arxiv.org/abs/2307.03172
- **[Long2022-long-time-no]** `existing` Xu et al. (2022). Long Time No See! Open-domain conversation with long-term persona memory.
- **[Maharana2024-locomo]** `existing` Maharana et al. (2024). LoCoMo. ACL 2024. https://arxiv.org/abs/2402.17753
- **[Mem02026-ai-memory-benchmarks]** `existing` Mem0 (2026). AI Memory Benchmarks in 2026 (vendor blog). https://mem0.ai/blog/ai-memory-benchmarks-in-2026
- **[Memoryagentbench2025-memoryagentbench]** `existing` Hu, Wang, McAuley (2025). MemoryAgentBench. https://arxiv.org/abs/2507.05257
- **[Memos2025-memos]** `existing` Li et al. (2025). MemOS.
- **[Packer2023-memgpt]** `existing` Packer et al. (2023). MemGPT. https://arxiv.org/abs/2310.08560
- **[Pakhomov2025-convomem-benchmark]** `existing` Pakhomov et al. (2025). ConvoMem Benchmark. https://arxiv.org/abs/2511.10523
- **[Pan2025-memory-construction-retrieval]** `existing` Pan et al. (2025). On Memory Construction and Retrieval for Personalized Conversational Agents (SeCom). https://arxiv.org/abs/2502.05589
- **[Park2023-generative-agents]** `existing` Park et al. (2023). Generative Agents. https://arxiv.org/abs/2304.03442
- **[Qian2024-memorag]** `existing` Qian et al. (2024). MemoRAG. https://arxiv.org/abs/2409.05591
- **[Rasmussen2025-zep]** `existing` Rasmussen et al. (2025). Zep. https://arxiv.org/abs/2501.13956
- **[Schick2023-toolformer]** `existing` Schick et al. (2023). Toolformer. NeurIPS. https://arxiv.org/abs/2302.04761
- **[Su2026-beyond-dialogue-time]** `existing` Su et al. (2026). Beyond Dialogue Time: Temporal Semantic Memory. https://arxiv.org/abs/2601.07468
- **[Tan2025-prospect-retrospect]** `existing` Tan et al. (2025). Reflective Memory Management (RMM). ACL 2025.
- **[Thakur2021-beir]** `existing` Thakur et al. (2021). BEIR. https://arxiv.org/abs/2104.08663
- **[Wang2025-mirix]** `existing` Wang et al. (2025). MIRIX. https://arxiv.org/abs/2507.07957
- **[Wu2021-recursively-summarizing-books]** `existing` Wu et al. (2021). Recursively Summarizing Books with Human Feedback. https://arxiv.org/abs/2109.10862
- **[Wu2024-longmemeval]** `existing` Wu et al. (2025). LongMemEval. ICLR 2025. https://arxiv.org/abs/2410.10813
- **[Xu2022-beyond-goldfish-memory]** `existing` Xu, Szlam, Weston (2022). Beyond Goldfish Memory. https://arxiv.org/abs/2107.07567
- **[Xu2025-mem]** `existing` Xu et al. (2025). A-MEM. https://arxiv.org/abs/2502.12110
- **[Yan2025-memory-r1]** `existing` Yan et al. (2025). Memory-R1. https://arxiv.org/abs/2508.19828
- **[Yao2023-react]** `existing` Yao et al. (2023). ReAct. https://arxiv.org/abs/2210.03629
- **[Zhang2018-personalizing-dialogue-agents]** `existing` Zhang et al. (2018). PersonaChat.
- **[Zhao2025-prefeval]** `existing` Zhao et al. (2025). PrefEval.
- **[Zheng2023-judging-llm-judge]** `existing` Zheng et al. (2023). Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena. NeurIPS. https://arxiv.org/abs/2306.05685
- **[Zhong2023-memorybank]** `existing` Zhong et al. (2023). MemoryBank. https://arxiv.org/abs/2305.10250

## Notes and cautions
- Not covered because no source was found in this pass: a dedicated study of nickname handling in dialogue entity linking; a published ablation of a rule-built context prefix; independent reproduction of vendor LoCoMo numbers.
- Dates in the Zep/Mem0/Letta benchmark dispute and the exact figures in vendor blogs are not re-verified here; use `paper/research_notes/systems_2026.md` and cite the vendor source with the protocol.
- Third-party gold-error counts for LoCoMo are not cited; our own errata (24 gold errors among the 287 wrong, class `gold_error`) are measured and in `analysis/locomo_errata.json`.
