# R14 retrieval and ingestion problems in LoCoMo failures (clean-jina-full)

Package DEEP-RETR, tag R14, 2026-10-14. CPU only, no model calls. Companion files: `r14_retrieval.json` (machine-readable items) and `evals/analysis/r14_retrieval_features.csv` (one row per question, ids, labels and numeric features, no text).

Licence: ids, counts and invented-name paraphrases only. Examples below use invented names (Ilse, Bram) and are not dataset text.

## 1. Population and method

Reference run `clean-jina-full`: 1,540 questions (categories 1-4), 1,253 correct, 287 wrong. Retrieval-stage labels: `b` 68 (gold in no leg), `cf` 43 (cut at fusion/pool), `cr` 4 (cut at rerank keep) = **115 questions = 40.1% of wrong answers, 7.5% of all questions**. Loss rate by category: multi-hop (cat1) 53/282 = 18.8%, open-domain (cat3) 28/96 = 29.2%, temporal (cat2) 14/321 = 4.4%, single-hop (cat4) 20/841 = 2.4%.

Per question I joined: dataset evidence ids, vector/BM25/perspective leg ranks (logged to rank 60), fused pool (20), rerank order, final (10, or 30-34 in list mode) and the rendered context (about 52 turns: anchors plus a window). Gold turn features: stemmed content-word overlap with the question (turn text and caption), answer overlap, length, speaker, caption, relative-date marker, neighbour overlap (+-2 turns), session position, distance to the nearest final anchor. Question features: number of gold turns and sessions, named speakers, date, generic nouns, list shape, list-mode flag.

Gold turns in the 115 losses: 347, of which 137 reach the context and **210 do not**. Of the 210: 115 are in no leg's top 60, 84 were cut at fusion, 11 at rerank.

Reader conversion used for "expected fix": accuracy of this run when every gold turn is in context, by category: cat1 75.5%, cat2 86.6%, cat3 46.9%, cat4 92.2%. If all 115 losses received complete gold the expected gain is about 83.7 questions (+5.4 points, 81.4% to about 86.8%). This is an upper bound for the retrieval side only.

Union-leg ceiling (any leg's top-K, no cut): all gold reachable in 9 / 16 / 20 / 40 of the 115 questions for K = 10 / 20 / 30 / 60; 15 / 28 / 44 / 87 of 210 missing gold turns.

### Why gold turns are missing (gold-level, exclusive classes, n = 210)

| Class | Gold turns | Questions | Meaning |
| --- | --- | --- | --- |
| cut, best leg rank <=10 | 15 | 15 | found near the top of a leg, lost in fusion |
| cut, best leg rank 11-30 | 29 | 24 | |
| cut, best leg rank 31-60 | 51 | 39 | |
| no leg, answer-bearing, no question words | 30 | 21 | paraphrase or category-versus-instance wording |
| no leg, context-dependent | 36 | 24 | own overlap <=0.1, a neighbour carries it |
| no leg, indirect | 27 | 21 | answer not stated in the turn |
| no leg, overlap but deep rank | 22 | 17 | |

### Exclusive waterfall (one primary pattern per question, order: date-anchored cut, open-domain, aggregation, rerank cut, fusion cut, context-dependent, two-gold, paraphrase, indirect)

| Primary pattern | Questions | Expected fix |
| --- | --- | --- |
| multi-evidence aggregation (n_gold >= 3) | 39 | 29.7 |
| open-domain inference (cat3) | 28 | 13.1 |
| fusion cut (single or paired gold) | 14 | 11.9 |
| date-anchored cut | 12 | 10.8 |
| context-dependent gold | 10 | 8.2 |
| two-gold partial or none | 6 | 4.5 |
| rerank cut | 2 | 1.8 |
| paraphrase | 2 | 1.8 |
| indirect | 1 | 0.9 |
| other (9-152) | 1 | 0.9 |

Ids per pattern are in the JSON (`primary_pattern_ids`) and the CSV (`primary_pattern`).

## 2. Items (overlapping counts; a question can belong to several)

| Id | Generic problem | Count (share of 115) | Expected fix | Layer | Priority |
| --- | --- | --- | --- | --- | --- |
| R14-RET-1 | Aggregation needs more independent evidence than a fixed top-k delivers | 55 (47.8%) | 37.6 | retrieve | H |
| R14-RET-2 | Open-domain judgement questions have indirect evidence | 28 (24.3%) | 13.1 | retrieve | M |
| R14-RET-3 | Explicit-date questions ignore the session date; gold found then cut | 13 (11.3%) | 11.7 | retrieve | H |
| R14-RET-4 | Gold found by one leg is lost at fusion | 68 (59.1%); 43 only this | 50.3 | retrieve | H |
| R14-RET-5 | Word-overlap gap: paraphrase and category wording | 81 any zero overlap (70.4%); 68 all <=0.1 | 56.8 / 48.4 | retrieve | M |
| R14-RET-6 | Elliptical or anaphoric gold turns need the neighbour | 32 (27.8%) | 23.8 | ingest | M |
| R14-RET-7 | Rerank keep-line and unspent budget | 4 stage cr; 105 under 3,000 tokens | 3.0 | compose | M |
| R14-RET-8 | Gold spread over >= 3 sessions | 43 (37.4%) | 29.0 | retrieve | M |
| R14-RET-9 | Lucky and barely-in passes distort recall metrics | 95 lucky + 153 barely | n/a | data | L |
| R14-RET-10 | Conversation-level difficulty (3 and 5) | 39 | n/a | data | L |
| R14-RET-11 | Aggregation not detected by list mode | 37 | 23.9 | retrieve | H |
| R14-RET-12 | Evidence-id parsing hides gold ids | 10 ids | n/a | data | L |

Expected fix is the sum of category reader conversion over the pattern's questions, not additive across rows.

### R14-RET-1 Aggregation under a fixed anchor budget (H)

- **Evidence (measured).** 55 of 115 losses have n_gold >= 3 (exclusive waterfall 39). Share of questions with every gold turn in context, by number of gold turns, all 1,540: 1 turn 95.9%, 2 turns 77.3%, 3 turns 54.3%, 4-5 turns 30.0%, 6+ turns 8.7%. Loss rate for n_gold >= 3 is 29.9% against 4.4% otherwise (OR 9.2). Mean fraction of gold retrieved in the 55: 47%. Missing gold classes in the 55: cut 31-60: 38, no-leg paraphrase: 21, context-dependent: 21, cut 11-30: 20, indirect: 20, deep: 12, cut top-10: 8.
- **Gap.** Depth is constant per shape; there is no coverage objective (entity, session, item) and no stop rule.
- **Hypothesis.** An evidence-set objective plus a slot-filling second round lifts all-gold-in-context for n_gold >= 3 from 34% (non-list) / 44% (list) to at least 60%, converting about 20 questions (+1.3 points).
- **Solutions.** (a) adaptive depth by evidence demand (0 calls, heuristic); (b) coverage-aware selection such as maximal marginal relevance with entity or session buckets (0 calls; B14 reports session_cap and cluster options neutral in single-shot, so gate by the aggregation detector); (c) slot-filling second round (`read.agentic_mode=slot`, 1-2 calls on 12-18% of questions); (d) derived instance index at write time (`offline.standing_v2`, ingest-time calls).
- **Existing.** B1, B8, I4, R01, SOL-LISTMODE, SOL-STANDING, SOL-AGENTIC, SOL-OBLIG. **R14-confirmed** for B1.
- **Test.** Slice of the 55 ids plus 60 matched n_gold >= 3 correct questions as guard; sample-first screen, then full run.

### R14-RET-2 Open-domain judgement questions (M)

- **Evidence (measured).** 28 cat3 losses; 99 of 208 cat3 gold turns (47.6%) miss the context, against 2.8% for single-hop. Reader accuracy given all gold is only 46.9%, so retrieval repairs only about 13 of 28. Typical shape (invented): "Would Ilse prefer the coast or the mountains?" with gold turns that describe holidays, never naming either.
- **Gap.** No built primary solution for this class in the solution map; the query is the question text.
- **Hypothesis.** Decomposing into evidence-kind sub-queries recovers about half of the missing gold in cat3, a gain of about 6 questions after the reader ceiling.
- **Solutions.** Evidence-kind query decomposition (1 call); HyDE-style answer-hypothesis leg (`SOL-GAP-HYDE`, not built, 1 call); trait and preference synthesis at write or sleep time lifted to source turns (K stage; risk: unsupported inferences, A08).
- **Existing.** B3, C3, C4, SOL-PLANNER. **R14-confirmed** for B3.

### R14-RET-3 Date-anchored questions (H)

- **Evidence (measured).** 13 retrieval losses carry an explicit date (day or month plus year); 12 are cf, 1 is b. Gold session date minus question date: 0 days in 5, <= 7 days in 7, <= 12 days in 10. Dated questions are 11.7% of all questions but 28% (12/43) of fusion cuts. 167 of the 180 dated questions pass, so a prior has plenty of guard cases.
- **Gap.** No session-date prior at fusion; the temporal leg is built, off in the reference and fires on only 187 questions.
- **Hypothesis.** A date window prior recovers 9-11 of the 13 (expected fix 10.8-11.7) without loss on the 167.
- **Solutions.** Date-scoped retrieval prior (`read.temporal_relative`, `read.temporal_infer_year`, `read.lexical_dates`, 0 calls; risk: premise errors, I61); event-date index matching both said and happened dates (I42, I79).
- **Existing.** B4, I8, SOL-TEMPLEG. **R14-sharpened** for B4.
- **Test.** All 180 explicit-date questions as the slice (13 fail, 167 guard).

### R14-RET-4 Found by a leg, lost at fusion (H)

- **Evidence (measured).** 68 questions have at least one such gold turn (95 turns); in 43 every missing gold is of this kind. By leg: 27 vector-only, 27 BM25-only, 24 both. Of the 15 turns with best leg rank <= 10, 11 never entered the 20-candidate pool, 10 of them BM25 top-10 gold with vector rank above 30 or absent. In-context rate by best leg rank (all gold turns): 1-3: 99.6%, 4-10: 95.8%, 11-20: 87.9%, 21-30: 67.9%, 31-60: 47.6%.
- **Gap.** Protection covers the top 3 per leg; fusion is rank-based; no fusion cut is logged (I78).
- **Hypothesis.** Top-8 per-leg protection or score-normalised fusion keeps the 15 top-10 cuts; a 30-40 rerank pool recovers part of the 11-30 band. Ceiling at K = 10 / 20 / 30 / 60: 9 / 16 / 20 / 40 questions.
- **Solutions.** Deeper per-leg protection (`read.pool_protect_per_leg` 3 to 8); score-aware or learned fusion (`read.family_fusion`, `read.legs_v2`); wider rerank pool (`read.rerank_v2_search_pool`). Offline CPU re-fusion from the logged legs sizes the effect before any run.
- **Existing.** B7, B9, B10, I75, I78, R02, SOL-POOLPROT. **R14-confirmed** for B7 and I75.

### R14-RET-5 Word-overlap gap (M)

- **Evidence (measured).** Gold-level miss rate by overlap with the question: 0: 33.9%, 0.1-0.2: 19.0%, 0.2-0.4: 6.2%, above 0.4: 2.1%. Spearman of overlap with BM25 rank -0.66 and vector rank -0.48. 133 of 210 missing gold turns (63%) have zero overlap against 22% of retrieved ones. 81 questions have such a turn; 68 have all missing gold at overlap <= 0.1. Subtypes: answer-bearing but unmatched wording 21 questions, inferential 21, context-dependent 24 (RET-6), deep 17.
- **Gap.** Both legs depend on the surface form; neither the record nor the query is enriched with the category vocabulary.
- **Hypothesis.** Record-side enrichment recovers 6-8 of the 21 paraphrase questions; query-side expansion helps less (B14 found expansion options neutral).
- **Solutions.** Record-side gloss at write (1 call per turn at ingest); learned sparse or late-interaction leg (word-vector leg is built, B15); query-side expansion (1 call).
- **Existing.** B2, B5, B6, B15, SOL-LEXICAL.

### R14-RET-6 Elliptical and anaphoric gold turns (M)

- **Evidence (measured, heuristic rule: own overlap <= 0.1 and a turn within +-2 has overlap >= 0.25).** 32 questions, 36 missing gold turns; miss rate 24.7% against 13.7% for other gold turns; in 25 cases the previous turn alone carries the overlap. Only 12 of 210 missing gold turns lie within 2 turns of a final anchor (median distance to the nearest anchor in the same session: 6). Example shape (invented): question "What did Ilse plan to do with the recipe Bram promised?", gold turn "Great, I will make it for the family this weekend".
- **Gap.** Single-turn records; the topical neighbour must itself be found before the window helps.
- **Hypothesis.** An exchange-pair index or write-time contextualisation recovers 8-10 of the 32.
- **Solutions.** Exchange-pair records as a second index (0 calls, index 2-3x); decontextualisation at write for the elliptical 10-15% of turns (1 call each); reply-edge expansion (A07).
- **Existing.** A07, I6, SOL-BUNDLES, SOL-WINDOWFACTS.

### R14-RET-7 Rerank keep-line and unspent budget (M)

- **Evidence (measured).** 4 stage-cr questions; 11 gold turns in 9 questions were in the pool and dropped at reranker positions 4-21. The 115 losses use a median 2,216 of 4,096 context tokens; 105 are under 3,000; only 2 of 1,540 contexts hit the cap. Gold arrives via anchors in 1,726 of 1,996 cases and via window only in 270 (86 of those in wrong answers).
- **Hypothesis.** Budget-driven keep (or keep 15) recovers the 4 questions at no accuracy cost.
- **Solutions.** Budget-driven keep (`read.rerank_keep`, `read.exact_budget`); anchor-over-window allocation (`read.replay_window_*`). Guard: the 184 correct questions that rely on window-only gold.
- **Existing.** B9, B10, SOL-RERANKKEEP.

### R14-RET-8 Gold spread over sessions (M)

- **Evidence (measured).** 43 of 115 losses have gold in >= 3 sessions; loss rate 34.1% against 5.1% (OR 9.7). All-gold rate by number of gold sessions: 1: 95.3%, 2: 69.9%, 3: 35.8%, 4+: 15.5%.
- **Solutions.** Session-level leg with the best turn per matched session (`read.episode_leg_v2`, digest calls at close); entity-linked turn index (`read.entity_leg_v2`).
- **Existing.** B8. **R14-confirmed.**

### R14-RET-9 Lucky and barely-in passes (L, data)

- **Evidence (measured).** 95 correct answers (7.6% of correct) have at least one gold turn missing from the context, 20 with none (68 cat1, 15 cat3, 8 cat2, 4 cat4). 28 of the 95 have the gold answer string present in the context in a turn that is not labelled gold; 37 have a gold answer of two words or fewer; 15 are yes/no or either-or. 153 further correct answers have every gold in context but at least one only through the window or at anchor rank >= 8. Accuracy by reach: all gold at anchors 88.3% (1,161), all in context some via window 79.8% (163), partial 53.2% (141), none 28.2% (71).
- **Implication.** Gold ids are neither necessary nor sufficient; a recall screen should add answer-support recall and a fragility slice (the 153 plus the 95) as guard.
- **Solutions.** Answer-support recall (string or checked entailment); perturbation re-runs on the fragile slice (noise floor A16: 19% of dev questions flip).

### R14-RET-10 Conversation effects (L, data)

- **Evidence (measured).** Loss rate by conversation index: 3.9%, 6.2%, 5.9%, 11.6%, 6.7%, 13.0%, 6.7%, 7.3%, 7.7%, 5.1%. Conversations 3 and 5: 39/322 = 12.1% against 76/1,218 = 6.2% (OR 2.07, p = 0.0008); ten-way chi-square p = 0.08, so the pattern is suggestive. Loss rate vs mean words per turn: Spearman -0.62 (n = 10); vs number of sessions -0.07.
- **Hypothesis.** Topic-dense conversations (many near-duplicate pet or hobby turns) crowd gold out; diversity-aware selection reduces the gap.
- **Solutions.** Per-conversation breakdown in every screen report; near-duplicate suppression in the pool (I31).

### R14-RET-11 Aggregation not detected (H)

- **Evidence (measured).** List mode fires on 72 of 184 questions with n_gold >= 3 (39%). Loss rate 33% (37/112) when not fired against 25% (18/72) when fired; all-gold rate 33.9% against 44.4%. 37 of the 55 aggregation losses were not in list mode. Question openers with high loss rate: could 43%, might 42%, would 29% (open-domain), how many 19%, which 15.5%, what are 15.6%; "when" 2.1%.
- **Gap.** The trigger is an English vocabulary regex (I4) with no retrieval-side signal.
- **Hypothesis.** Question shape plus leg score profile fires on at least 70% of n_gold >= 3 at <= 15% false fire; the missed 37 gain about 4 all-gold questions.
- **Solutions.** Score-profile trigger (0 calls); small learned shape classifier (1 small call, I28). Offline test from the CSV columns `list_mode`, `n_gold`, `nfinal`.
- **Existing.** I4, I28, I56, SOL-LISTMODE, SOL-CONTRACT.

### R14-RET-12 Evidence-id hygiene (L, data)

The "11 gold ids missing from forensics" are in questions 8-31 (3 ids), 8-38 (4) and 8-46 (4): the evidence is one string with space-separated ids, so the parser reads none. All three are correct cat3 answers with 2 of 3-4 gold turns in context (lucky passes). Also: 9-69 has a zero-padded id, 3-58 and 6-38 each name a turn that does not exist, and 0-30, 0-46, 9-39, 9-42 carry no evidence ids. Fix: split on whitespace, strip leading zeros, assert existence.

## 3. Patterns tested and not supported

| Pattern | Result (measured) |
| --- | --- |
| Very short gold turns | 15 of 2,359 gold turns; 1.0% of missing; not a driver |
| Long gold turns (>= 60 words) | 3.8% of missing vs 4.1% of retrieved |
| Gold in image captions | Missing gold carries a caption in 36.2% vs 39.1% of retrieved; answer only in the caption: 8 questions (3.8% vs 2.9%); captions are already in the stored text |
| Entity aliasing and nicknames | 4 questions; 1.9% of missing vs 2.4% of retrieved |
| Question names one person, gold said by the other | 17 of 83 such gold turns missed (20.5%) vs 15.3%, OR 1.43, p = 0.22 |
| Relative date in the gold turn | miss 14.2% vs 15.9%; the cost is on the reader and date side |
| Generic category noun in the question | OR 1.28, p = 0.23; the list-shaped wording (OR 2.8) acts through n_gold |
| Recency or position | miss rate 17.9% (first fifth of the conversation) to 14.3% (last fifth); weak primacy tilt, no recency effect |
| Photo-referencing questions | 1 of 37 |

## 4. Reproduction and guards

- Features were computed from `results.jsonl`, `forensics.jsonl` (`vector`, `lexical`, `extra_legs`, `fused`, `pool`, `rerank_scores`, `final`, `context_records`), `ingest.jsonl`, `locomo10.json` evidence and `locomo_problem_labels_clean_jina_full.txt`; the CSV carries the derived columns (`m_*` counts are over missing gold turns).
- Heuristic features (overlap rule, context dependence, generic-noun lexicon, date parser) are marked heuristic in the JSON; thresholds are simple fixed cut-offs (0.1 and 0.25 overlap, +-2 turns); the context-dependence rule was loosened once after a first, too strict, version found only one case.
- Dataset text appears only in local scratch files outside the repository.
