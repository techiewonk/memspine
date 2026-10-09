# LoCoMo retrieval gaps and reranker effect (qs-eq06-roff baseline)

Scope: 1,540 non-adversarial LoCoMo questions, run `qa-full-qs-eq06-roff--memspine` (Qwen3-Embedding-0.6B + Tantivy BM25, RRF, top_k 10, replay window +-2, no reranker, reader/judge Qwen3.5-9B), compared with `qs-eq06-rjina`, `qs-ejina-rjina`, `qs-ebge-roff`. Analysis was CPU-only (Qwen3-Embedding-0.6B re-embedded on CPU, own BM25 implementation); no git, GPU, Ollama or harness was touched and no existing file was edited.

## 0. Summary

1. Retrieval is implicated in 210 of the 395 wrong answers (115 `retrieval_miss` + 95 `partial`, 13.6% of all questions). The other 183 are reader failures with all gold in context, and 2 have no usable gold id. If every incomplete question got complete gold and the reader kept its category-specific accuracy-given-complete-gold (cat1 0.83, cat2 0.78, cat3 0.30, cat4 0.91), accuracy would be about 83.0% vs 74.4% today. That +8.6 points (157 questions) is the ceiling for any retrieval work, not a forecast.
2. The biggest finding is the top-10 cut-off, not the embedder. The context is built from only 10 hits (about 8 non-overlapping windows). Of all 2,358 gold turns, only 57.8% are themselves a fused top-10 hit. Another 22.2% sit at fused ranks 11-60 of the two legs' top-30 lists, and the +-2 window rescues a further 16.8%. Class B (gold in a leg top-30 but outside the fused top-10) is the largest class: 74 of the 210 wrong questions (35%) and 142 of 391 missing gold turns (36%). The context uses only about 1,570 of 4,096 tokens, so the budget is not the constraint. Raising the candidate set to 20-25 hits (`candidate_pool` 2-3) is simulated to recover about 58-72 questions (about 47-57 correct answers, +3.0 to +3.7 points).
3. The reranker was run in its weakest configuration. With `candidate_pool=1` it only sees the fused top-10, so it can only delete: the reranked context is a subset of the no-rerank context in 1,540 of 1,540 questions. The deletion comes from min-max normalising rerank scores and then applying `assembly.relative_floor: 0.3` (the lowest-scored candidate is always below the floor). It keeps 4.7 of 10 hits on average and cuts the context from 39.3 to 16.7 turns.
4. The reranker itself discriminates well: it keeps 94-100% of the hits that are gold and only 28-81% of the others. The loss is collateral: 72% of the gold turns lost (64 of 89) were window neighbours of a dropped hit, not hits themselves. The gold-loss accounts for -12 correct answers of the net -47; the rest comes from less surrounding context even when all gold survives (-29, p about 0.007).
5. 69% of missing gold turns share no content word with the question (23% for covered gold). Names do not help: the question's speaker appears in 92-98% of missed gold turns' prefixes, and every turn in a two-person conversation carries a name. Remaining misses beyond the top-30 are indirect-evidence turns (multi-hop, image captions, enumerations, date-relative turns), and those need new retrieval behaviour (decomposition, contextual indexing, date-resolved matching), not tuning.
6. Several existing legs and weights are net harmful in simulation (lexical weight 2, core_terms_leg, cohesion_leg, sentence_leg, entity_expand_leg, rrf_k 1). Do not enable them for LoCoMo.

## 1. What could and could not be reconstructed

**What `retrieved_ids` contains.** `memspine_system.py::query` builds the evidence list from the engine's final `assembled.records`; `retrieved_ids` is therefore the whole context (hits + replayed window neighbours, dated lines), not the hits. `Engine._read_routed` (mode `replay`) calls `_assemble_core` for the top-10, then for each episodic hit adds the hit first, then neighbours nearest-first inside the same session, `replay_window=2` each side, skipping any that do not fit the budget. Hit identity and rank are not stored in `results.jsonl`; `trace.jsonl` `E_t` is just the context in order with placeholder scores `1/(rank+1)`. The leg-level forensics (`MEMSPINE_FORENSICS_DIR`) exist only for the 2-question run `fx-test2`.

**What I did.** I re-ran the retrieval on CPU: Qwen3-Embedding-0.6B (fp32, same query instruction, documents `"Speaker: text [image: caption]"`), cosine top-30; own BM25 (k1 1.2, b 0.75, alphanumeric lower-case tokens, same as Tantivy default) top-30; RRF k=60; top-10; +-2 window inside the dataset session. Validation against the real run:

| check | result |
|---|---|
| mean context size, simulated vs actual | 39.9 vs 39.3 turns |
| Jaccard of simulated vs actual context | 0.90 mean; 959 of 1,540 identical |
| simulated top-10 hits found in the actual context | 94.5% |
| gold any/all coverage, simulated vs actual | 0.900/0.785 vs 0.905/0.792 |
| reranked context subset of no-rerank context | 1,540 / 1,540 (actual runs) |

Residual differences: bf16-on-GPU vs fp32 numerics, my BM25 vs Tantivy, the `temporal_leg` (on in the base template, only fires on questions naming an absolute date), tie ordering, and the budget skipping. Hence numbers from the simulation carry roughly +-6 questions of noise (the baseline simulation "newly covers" 6 wrong questions that the real run missed; recovery counts below subtract this where marked "net").

**Not possible:** rank of a gold turn *before and after* the Jina reranker (rerank scores are not stored and I did not run the reranker, which needs a GPU-sized model); LLM-dependent options (planner, rewrites, mined facts) cannot be simulated, so their estimates are judgement, flagged as such. Gold labels are known to be incomplete: 106 incomplete questions were answered correctly (26 miss, 80 partial), so "gold coverage" understates the reader's evidence. Question numbers (`Q#`) below are the 0-based position in `results.jsonl` among the 1,540 non-cat-5 questions.

## 2. Task 1: the missing gold turns

### 2.1 Population

| | all 1,540 | cat1 multi-hop (282) | cat2 temporal (321) | cat3 open (96) | cat4 single-hop (841) |
|---|---|---|---|---|---|
| all gold in context | 79.2% | 34.8% | 87.5% | 47.9% | 94.4% |
| some gold | 11.3% | 51.8% | 3.1% | 18.8% | 0.1% |
| no gold | 9.5% | 13.5% | 9.0% | 29.2% | 5.5% |
| accuracy given all gold in context | 0.85 (1,036 of 1,219) | 0.83 | 0.78 | 0.30 | 0.91 |
| wrong with retrieval problem (miss / partial) | 210 (115 / 95) | 107 | 33 | 34 | 36 |

Cat1 is a coverage problem: only 35% of multi-hop questions have complete evidence in context. Cat3's problem is mostly the reader (0.30 accuracy even with all gold), though 29% of cat3 questions have no gold either. By evidence breadth (questions): 1 gold turn: 1,122 q, 7.6% miss, accuracy 0.81; 2 turns: 229 q, 12.7% miss + 25.3% partial, accuracy 0.59; 3-4 turns: 141 q, 16.3% miss + 57.4% partial, accuracy 0.55; 5+ turns: 43 q, 9.3% miss + 83.7% partial, accuracy 0.40. Multi-evidence spread over sessions: same session 81 q, accuracy 0.65; 2 sessions 206 q, 0.58; 3+ sessions 126 q, 0.45.

### 2.2 Taxonomy

Each missing gold turn is assigned to the first matching class, in this order (the order is the cheapest fix first). Question-level class = the hardest class among a question's missing turns (order of difficulty A < B < C5 < C3 < C4 < C2 < C1).

| class | definition (simulated retrieval) | what it means |
|---|---|---|
| A near-window | gold is 3-5 turns from a fused top-10 hit in the same session | window too small |
| B cut-off | gold is in the vector or BM25 top-30 (fused rank 11-60) but outside the fused top-10 | top-10 too small / ranking |
| C4 date-dependent | beyond top-30 in both legs; turn uses a relative time word ("yesterday", "last week"); question is cat2 or names a date | needs the session date to match |
| C1 image-caption | beyond top-30; turn carries an `[image: ...]` caption | answer lives in a generic BLIP caption |
| C2 short/anaphoric | beyond top-30; <= 6 words, backchannel, or a pronoun/"yes" opener <= 12 words | needs the previous turn |
| C5 enumeration | beyond top-30; question has >= 4 gold turns | one of many incidental mentions |
| C3 no anchor | beyond top-30; none of the above | no lexical or semantic anchor in the question |

**Wrong answers with a retrieval problem (210 questions, 391 missing gold turns):**

| class | questions | % of 210 | of which miss / partial | missing turns | % of 391 | cat1 / cat2 / cat3 / cat4 (questions) |
|---|---|---|---|---|---|---|
| A near-window | 26 | 12.4% | 16 / 10 | 56 | 14.3% | 8 / 3 / 4 / 11 |
| B cut-off (leg top-30) | 74 | 35.2% | 39 / 35 | 142 | 36.3% | 39 / 17 / 3 / 15 |
| C1 image-caption | 49 | 23.3% | 25 / 24 | 65 | 16.6% | 28 / 0 / 18 / 3 |
| C2 short/anaphoric | 4 | 1.9% | 2 / 2 | 5 | 1.3% | 3 / 0 / 0 / 1 |
| C3 no anchor | 33 | 15.7% | 20 / 13 | 45 | 11.5% | 16 / 3 / 8 / 6 |
| C4 date-dependent | 10 | 4.8% | 8 / 2 | 10 | 2.6% | 0 / 10 / 0 / 0 |
| C5 enumeration | 14 | 6.7% | 5 / 9 | 68 | 17.4% | 13 / 0 / 1 / 0 |
| total | 210 | | 115 / 95 | 391 | | 107 / 33 / 34 / 36 |

**All incomplete questions, correct or not (316 questions, 581 missing turns):** A 44 q / 89 turns; B 109 / 219; C1 71 / 92; C2 5 / 7; C3 47 / 63; C4 12 / 12; C5 28 / 99.

Sub-structure of the larger classes (wrong set):
- A: distance to the nearest hit is 3 turns for 18 turns, 4 for 33, 5 for 5. A window of 4-5 would close it.
- B: fused rank 11-20 for 65 turns, 21-30 for 30, 31-60 for 47. 117 of 142 are in the **vector** top-30 only, 23 in BM25 top-30 only, 2 in both. Median cosine rank of these gold turns within the conversation: 15.
- C classes: median cosine rank 123-193 (of 140-960 turns per conversation), BM25 rank 177-370. These are not near-misses.

### 2.3 Features of missed vs covered gold turns (all 2,358 gold turns; 581 not in context)

| feature | covered gold | missed gold | note |
|---|---|---|---|
| zero content-word overlap with question (speaker names excluded) | 23.4% | 69.2% | the dominant signal |
| mean overlap fraction | 0.35 | 0.09 | |
| median cosine rank in its conversation | 3 | 50 | |
| median BM25 rank | 9 | 192 | |
| question names the turn's speaker | 92-98% of missed turns in every class | | non-discriminative: a two-person chat |
| image-caption turn | share of gold 38.5% (dataset 20.8%) | miss rate 26.0% vs 24.6% overall (lift 1.05) | images are *common* gold, not harder per se |
| relative-time word in turn | 38.4% of gold | miss rate 23.6% (lift 0.96) | |
| short (<= 6 words) | 0.5% of gold | 18.2% (lift 0.74) | rare |
| pronoun/"yes" opener, <= 12 words | 1.2% of gold | 42.9% (lift 1.74) | rare but hard (28 turns) |

So the intuitive "backchannel / anaphora / image" explanation covers few misses (C2 is 1.3% of missing turns). The real pattern is **indirect evidence**: the gold turn mentions the answer without the question's vocabulary (a camping mention for "what activities does Melanie do", a photo caption for "what recipes", "yesterday ... Boston" for "where was Calvin on 3 October"). Cosine finds many of these around rank 7-30 (class B); the rest are beyond rank 100.

Within the 210, the hit set is also redundant by construction: the 10 hits span on average 7.5 distinct sessions and 8.7 non-overlapping windows, so the context is 8-9 mini-contexts rather than a few rich ones.

### 2.4 Worked examples

Format: question || gold answer; missing turn id (session, date), cosine rank in conversation (v), BM25 rank (b), fused rank (f), window distance to nearest hit (d).

**A near-window (window too small)**
- Q1276 (cat1, miss, 8 gold turns) "What kind of hobbies does Evan pursue?" || painting, hiking, ...: D1:14 "Yep, it's a great stress-buster. I started doing this a few years back. [image: painting of a cactus]" v10, f22, d4. The painting hit is four turns away; the turn itself says nothing without its neighbours.
- Q1209 (cat4, miss) "Why did Jolene have to reschedule their meeting with Deborah on September 8, 2023?" || had plans: D26:15 "Sorry, I remembered that I already have plans for this day." v83, d4. The question turn is in context, the reply is 4 turns later.
- Q1055 (cat1, partial) "Which games have Jolene and her partner played together?": D2:30 "We are planning to play 'Walking Dead' next Saturday." v71, d4.
- Q446 (cat1, partial) "What mediums does Nate use to play games?" || Gamecube, PC, Playstation: D27:21 image turn, d4.
- Q917 (cat2, miss) "Where was James at on July 12, 2022?" || Toronto: D16:9 (9 July) d4, f57.

**B cut-off (in a leg top-30, outside fused top-10)**
- Q35 (cat2, miss) "When did Melanie go camping in July?" || two weekends before 17 July: D9:1 "...a quiet weekend after we went camping with my fam two weekends ago" v11, b37, f18.
- Q1536 (cat4, miss) "What event did Calvin attend in Boston?" || fancy gala: D30:2 "I went to a fancy gala in Boston yesterday" v7, b53, f17. A single-hop question lost only to the cut.
- Q764 (cat1, miss, 4 gold turns) "What kind of indoor activities has Andrew pursued with his girlfriend?": D25:1 "My girlfriend and I went to this awesome wine tasting last weekend" v11, b53, f24.
- Q941 (cat1, partial) "Which of James's family members have visited him in the last year?" || mother, sister: D28:19 "my mother came to see me with her army friend two days ago" v5, f14. Cosine rank 5 but fused rank 14: BM25 buried it (b157) because the turn says "mother" not "family members", and RRF punishes the disagreement.
- Q1275 (cat1, partial, 5 of 6 missing) "Who was injured in Evan's family?": D7:9 "His ankle is getting better, but still sore" v8, f17.

**C1 image-caption**
- Q765 (cat1, partial) "What kind of places have Andrew and his girlfriend checked out around the city?": D23:3 "This weekend, I'm planning to check out this cozy cafe ... [image: a group of people ...]" v82, b74.
- Q443 (cat1, miss, 6 gold turns) "What things has Nate recommended to Joanna?": D27:23 "I'm currently playing this awesome fantasy RPG called Xenoblade Chronicles ... I highly recommend it" [image] v522.
- Q1123 (cat1, partial) "What activities does Deborah pursue besides yoga?": D12:1 "Had a blast biking nearby ... Checked out an art show ... [image]" v66, b220.
- Q587 (cat3, miss) "Would Tim enjoy reading books by C. S. Lewis or John Greene?" || C. S. Lewis: D1:14 (Harry Potter fan chat + image) v59. Open-domain; the evidence is oblique.
- Q451 (cat3, miss) "What alternative career might Nate consider after gaming?" || animal keeper with turtles: D25:19 "They eat a combination of vegetables, fruits, and insects. [image: a container of lettuce]" v444. The caption-only turn gives no lexical link.

**C2 short/anaphoric (rare)**
- Q1385 (cat1, partial) "Which bands has Dave enjoyed listening to?" || Aerosmith, The Fireworks: D23:9 "The Fireworks headlined the festival." v193.
- Q785 (cat1, miss) "What problems did Andrew face before he adopted Toby?": D2:12 "Thanks! Fingers crossed for the apartment and that furry friend." v33.
- Q1223 (cat4, miss) "What outdoor activity did Jolene suggest doing together with Deborah?" || Surfing: D29:27 "It's okay, maybe we can try it together sometime!" v196. The previous turn names surfing.
- Q887 (cat1, partial) "Which places or events have John and James planned to meet at?": D1:36 "Yeah, VR gaming is awesome! Let's do it next Saturday!" v161.

**C3 no anchor**
- Q441 (cat1, miss) "What animal do both Nate and Joanna like?" || turtles: D26:9 "They make me think of strength and perseverance" v101 (the turtle noun is in the neighbouring turn).
- Q786 (cat1, miss) "Did Audrey and Andrew grow up with a pet dog?": D13:10 "Max and I would take long walks in the neighborhood when I was a kid" v91 (the dog is "Max").
- Q781 (cat3, miss) "What is an indoor activity that Andrew would enjoy doing while making his dog happy?" || cook dog treats: D10:12 "I've been getting into cooking more" v120.
- Q818 (cat1, partial) "Has Andrew moved into a new apartment for his dogs?" || No: D28:12 "keeping the new addition on a leash while they get used to being outside" v41.
- Q587 (cat3): D1:18 "walking into a Harry Potter movie!" v96.

**C4 date-dependent**
- Q1425 (cat2, miss) "Which city was Calvin at on October 3, 2023?" || Boston: D21:1 (session dated 4 Oct) "Yesterday I met with some incredible artists in Boston". "Yesterday" resolves to 3 Oct, but the record's event time is 4 Oct and the text has no date. v72, b70.
- Q886 (cat2, miss) "Which recreational activity was James pursuing on March 16, 2022?" || bowling: D1:26 (session 17 March) "yesterday I went bowling". v84, b372.
- Q1413 (cat2, partial) "Where was Dave in the last two weeks of August 2023?" || San Francisco: D17:1 (2 Sept) "yesterday I came back from San Francisco".
- Q243 (cat2, miss) "When did Maria meet Jean?" || 24 Feb 2023: D7:1 (25 Feb) relative phrase in a long turn. v177.

**C5 enumeration (>= 4 gold turns)**
- Q155 (cat1, miss, 4 turns) "What do Jon and Gina both have in common?": D1:4 "I'm starting a dance studio" v202.
- Q645 (cat1, partial, 4 of 6 missing) "What books has John read?": D17:9 "Yep, I just finished this amazing fantasy series" v37 (no title in the turn).
- Q459 (cat1, partial, 6 of 7 missing) "What recommendations has Nate received from Joanna?": D15:14 cork board remark, v416.
- Q280 (cat1, partial) "What exercises has John done?": D10:1 "started a weekend yoga class" v33.
- Q1071 (cat3, miss, 10 of 10 missing) "How old is Jolene?" || likely no more than 30; she is in school: D8:2 "a lot going on with my studies and exams" v374.

Examples are listed at the missing-turn level (a question can appear under several classes; its question-level class is the hardest). Appendix A lists all 210 questions with every missing gold turn.

## 3. Task 2: the +-2 window

### 3.1 What the window does today (simulated hit sets, actual gold)

All 2,358 valid gold turns, by relation to the simulated fused top-10:

| relation | turns | share |
|---|---|---|
| gold turn is itself a top-10 hit | 1,362 | 57.8% |
| 1 turn from a hit (window ring 1) | 206 | 8.7% |
| 2 turns from a hit (ring 2) | 191 | 8.1% |
| 3-5 turns from a hit | 96 | 4.1% |
| > 5 turns from a hit, same session | 96 | 4.1% |
| session has no hit | 407 | 17.3% |

The window therefore supplies 397 gold turns (16.8%) that retrieval did not rank. At question level, 257 questions have all gold in context only because of the window (cat1 42, cat2 40, cat3 18, cat4 157); 208 of those (81%) were answered correctly. The window carries up to 13.5 accuracy points (208 of 1,540), an upper bound since the reader may answer some of them without the gold turn. The rescued gold lies mostly **after** the hit: 250 of 397 (63%) are 1-2 turns later (reply to a question hit), 147 earlier.

### 3.2 Simulated windows (k=10 hits, same session, no budget cap; tokens estimated as chars/4 + date prefix, simulation overstates the actual by about 10%)

| window | gold any | gold all | est. tokens | turns | wrong questions newly fully covered |
|---|---|---|---|---|---|
| 0 | 76.1% | 62.6% | 465 | 10.0 | 2 |
| 1 | 84.3% | 70.8% | 1,141 | 26.0 | 3 |
| **2 (current)** | 89.7% | 78.3% | 1,731 | 39.9 | 6 (simulation noise) |
| 3 | 90.3% | 79.2% | 2,255 | 52.5 | 14 |
| 5 | 91.9% | 81.5% | 3,169 | 74.8 | 32 |
| 8 | 93.1% | 82.7% | 4,268 | 101.8 | 43 |

Net of the 6-question simulation noise, window 3 recovers about 8 questions (+6 correct), window 5 about 26 (+20 correct, +1.3 points) at +83% context tokens. Marginal cost-effectiveness is poor beyond 2: ring 3 adds +0.9 points of full coverage for +524 tokens, rings 4-5 +2.3 points for +914.

Asymmetric windows (hit - before, after), k=10:

| before / after | gold all | est. tokens |
|---|---|---|
| 2 / 2 | 78.5% | 1,731 |
| 1 / 2 | 75.6% | 1,458 |
| 1 / 3 | 76.2% | 1,745 |
| 2 / 3 | 79.0% | 2,015 |
| 2 / 4 | 80.3% | 2,276 |
| 1 / 4 | 77.5% | 2,011 |

At equal cost (about 2,250 tokens) 2/4 beats symmetric 3 (80.3% vs 79.2%). Shrinking "before" hurts: keep 2 before. The engine has only a symmetric `replay_window`; an after-only extension needs new code (small).

Under the larger-k options, the window matters more than in the top-10 world: at k=20 a window of 1 loses 5 points of full coverage relative to 2 (79.9% vs 84.8%), so raise k before shrinking windows.

## 4. Task 3: reranker harm

### 4.1 Run comparison (actual)

| run | gold any | gold all | turns in context | context tokens | QA accuracy |
|---|---|---|---|---|---|
| qs-eq06-roff | 90.5% | 79.2% | 39.3 | 1,571 | 74.4 |
| qs-eq06-rjina | 88.8% | 76.9% | 16.7 | 672 | 71.3 |
| qs-ejina-rjina | 88.4% | 76.1% | 16.7 | 655 | 71.0 |
| qs-ebge-roff | 88.6% | 76.6% | 39.4 | 1,513 | 71.9 |

### 4.2 Mechanism (code-verified, then confirmed on data)

- `candidate_pool` defaults to 1 and none of the runs set it, so the reranker receives `candidates[:top_k]` = the fused top-10. Reordering ten items cannot change which ten are used; it can only change the scores that follow.
- `_minmax_normalize` maps the reranker's scores to [0, 1]: the worst candidate gets 0, the best 1. `assembly.relative_floor: 0.3` (base template) then drops every candidate below 0.3 x the best. The floor was a harmless no-op on RRF scores (the weakest single-leg rank-10 hit still scores about 0.44 x a two-leg rank-1) but on min-max rerank scores it always removes the worst candidate and everything in the bottom 30% of the score range.
- Result: of the simulated 10 hits, an average of 4.7 are retained (distribution of retained hits 1..10: 95, 172, 223, 245, 244, 199, 167, 136, 53, 6 questions). The chance that a hit is dropped climbs from 3% at fused rank 1 to 25%, 41%, 51%, 59%, 62%, 65%, 70%, 72%, 77% at ranks 2-10. Every hit that goes takes its +-2 window with it.
- Verified: in all 1,540 questions the reranked context is a subset of the no-rerank context; the reranker never added a turn. The reranker input is `concat_background(record)` = `[type: episodic]` header + the single turn: no neighbours (`rerank_context=0`), no date (`rerank_date_prefix=false`).

### 4.3 Where the gold was lost (qs-eq06-roff to qs-eq06-rjina)

- 34 questions had all gold in the no-rerank context and not in the reranked one (cat1 12, cat2 4, cat3 3, cat4 15); none went the other way. Accuracy on those 34: 0.71 to 0.35 (-12 correct).
- 89 gold turns were lost; only 25 were themselves hits. 64 (72%) were window neighbours of a hit that was dropped. The reranker is not misjudging the gold; it is dropping the hit next to it.
- Hit-level retention (simulated hits): gold hits are kept 100% (ranks 1-2: 810/812), 97% (ranks 3-5: 286/296), 94% (ranks 6-10: 238/254); non-gold hits are kept 81%, 47%, 28%. As a gold detector the reranker is strong.
- Profile of lost gold turns: 43 of 89 (48%) are image-caption turns versus 37% (629 of 1,688) of the gold turns that survived, and 26 of 89 (29%) carry a relative-time word versus 34% of survivors. The tilt towards image turns is modest; the loss is driven mainly by hit rank and window adjacency, not turn type.
- Examples: Q1181 "What kind of yoga routine does Deborah recommend to Jolene?" || gentle flow routine: the answer turn D16:15 sat two turns after the rank-3 hit D16:13; that hit was dropped and the answer lost (roff correct, rjina wrong). Q39 (cat1, LGBTQ participation): gold D5:1 (pride parade) was itself a rank-9 hit and D9:12 (art show) the neighbour of the rank-6 hit D9:11; both hits were dropped (this question was still answered correctly). Q750 "What is John's favorite book series?": gold D27:19 is a Tim turn, so the reranker's drop is defensible (label noise).

### 4.4 Accuracy decomposition (rjina -47 correct vs roff; 109 roff-only, 62 rjina-only)

| group | questions | change |
|---|---|---|
| gold coverage lost | 34 | -12 |
| all gold in both contexts | 1,185 | -29 (85.4% to 83.0%; 71 vs 42 discordant, McNemar chi2 about 7.4, p about 0.007) |
| partial/miss in either | 321 | -6 |

In the 1,185 questions with full gold in both, the loss by category is: cat2 -6.9 points (277 q), cat1 -4.7 (86), cat3 -4.7 (43), cat4 -0.5 (779). So 60% of the reranker's accuracy cost happens without losing any gold turn. The data cannot say why (candidate hypotheses: removal of non-gold dated context the temporal questions use; gold lists that omit a needed turn; evidence ordering). It needs a controlled run with the floor removed.

### 4.5 Why a reranker over 10 candidates is structurally limited

- It can only reorder the top-10: 996 of 2,358 gold turns (42.2%) are not in the fused top-10. Of these, 358 (15.2% of all gold) are at fused rank 11-30, 166 (7.0%) at 31-60 in the union of the legs, and 472 (20.0%) are in neither leg's top-30 (of the 996 outside the top-10: 437 are in the vector top-30 only, 79 in the BM25 top-30 only, 8 in both, 472 in neither). A reranker over a larger pool can reach the first two groups (22.2%); nothing in the pipeline reaches the last 20%.
- Per category, gold at fused rank 11-60: cat1 31.8%, cat2 15.7%, cat3 20.7%, cat4 15.9%; absent from both legs: cat1 28.8%, cat2 10.4%, cat3 51.4%, cat4 8.0%.
- Oracle bounds, wrong set (210): a perfect reranker choosing the 10 hits from a pool of 20, 30 or 60 would make 46, 62 or 86 of the 210 questions fully covered (about 37, 50, 69 expected correct answers at the category-wise reader accuracy). Class B is 40/53/72 of those.
- Pointwise vs listwise: the Jina v3.5 reranker is listwise (one pass over up to 64 documents, `max_documents=64`), so a 30-document pool is within its window. Listwise scores are only comparable inside one window, which is a second reason to set `rerank_keep` rather than rely on a score floor.
- The observed discrimination (94-100% of gold hits kept) suggests the model is not the weak part; the pool size and the floor are.
- The ejina-rjina run vs qs-eq06-roff: 81 questions lose full gold coverage and 34 gain it (a mixture of embedder and reranker effects, not separable here).

## 5. Task 4: fixes per class

Estimates are "questions recovered" among the 210 wrong retrieval questions (net of about 6 simulation noise where applicable) and "expected correct" = recovered x reader accuracy given complete gold (cat1 0.83, cat2 0.78, cat3 0.30, cat4 0.91). They are coverage-based upper-ish estimates: a larger context can also dilute the reader, and the harness truncates at 4,096 tokens on the rendered text.

### 5.1 Simulated with real data (CPU)

Note: coverage percentages here divide by 1,540 for the window/top_k rows and by the 1,535 questions with a valid gold id for the enrichment/leg rows (a 0.2-point difference); baselines are 78.3% and 78.5% respectively.

| fix (engine option / new code) | targets | est. context tokens | gold all (sim. baseline 78.5%) | recovered (wrong set, net) | expected correct |
|---|---|---|---|---|---|
| `replay_window: 3` | A | 2,255 | 79.2% | about 8 | +6 |
| `replay_window: 5` | A | 3,169 | 81.5% | about 26 | +20 |
| asymmetric window 2 before / 4 after (new, small) | A | 2,276 | 80.3% | about 15 | +12 |
| top_k 15 (`candidate_pool: 2` equivalent) | A, B | 2,536 | 82.0% | about 36 | +29 |
| **top_k 20 (`candidate_pool: 2`)** | A, B | 3,312 | 84.6% | about 58 | +47 |
| **top_k 25 (about `candidate_pool: 3`, budget-bound)** | A, B | 3,881 | 86.1% | about 72 | +57 |
| top_k 30, window 1 | A, B | 3,263 | 82.9% | about 71 | +56 (but 46 questions lose coverage) |
| contextual enrichment: `[session date] + previous turn` prefixed in both the embedded and the BM25 text (new code) | B, A, a few C | same as baseline | 79.7% (any 90.0% to 92.6%) | about 18 net questions across all 1,540 | about +14 |
| same enrichment, date only | B | same | 78.6% | about 1 | about 0 |
| `rrf_k: 10` | B | same | 79.1% | about 9 (noise level) | about +7 |

Enrichment and larger k target the same class: with k=20 the enriched index adds nothing (84.5% vs 84.8% plain). Enrichment is attractive only if the context must stay near 1,600 tokens.

Legs and weights that are net negative on this data (gold-all coverage change vs baseline; recovered minus lost questions across all 1,540): lexical weight 2 (-70), vector weight 2 (-4), `rrf_k: 1` (-2), `core_terms_leg` (-42), `cohesion_leg` (-44; in this harness every turn of a session carries the same timestamp, so the leg degenerates to the first 30 turns of the top-3 anchors' sessions), `sentence_leg` (-29, -22 at half weight), `entity_expand_leg` (-4, -1 at half weight), vector-only (-31), BM25-only (-148). Treat these as "do not enable for LoCoMo" unless a real run says otherwise.

### 5.2 Reranker (partly measured, partly judgement)

| fix | what it changes | recovered (est.) | expected correct | basis |
|---|---|---|---|---|
| `candidate_pool: 3`, `rerank_keep: 10-15`, reranker on | rerank the fused top-30, keep the best 10-15 plus windows (the configuration the base template comments suggest) | 35-50 (oracle bound 62) | +27 to +40 | oracle bound simulated; reranker keeps 94-100% of gold when it is a candidate; discounted for 30-document listwise noise |
| same, with `relative_floor: 0` for reranked runs (or `rerank_blend`, `rerank_gate`) | stops the floor deleting hits | removes the 34 lost-gold questions (about 12 correct) and likely part of the -29 context effect | +12 to +47 | measured loss (4.3-4.4); upper end assumes the context-size loss is fully reversible |
| `rerank_context: 1-2`, `rerank_date_prefix: true` | the reranker sees neighbours and the date (fixes short/anaphoric and date-relative turns) | 3-8 | +2 to +6 | judgement; C2 (4 q) + C4 (10 q) are the addressable part |

The floor-only fix returns the reranked run to about the no-rerank level; the reranker earns its keep only with a pool above 10.

### 5.3 Not simulable (LLM or new code), by class

| class | questions (wrong set) | fix | maps to | est. recovered | expected correct |
|---|---|---|---|---|---|
| A near-window | 26 | window 4-5, or asymmetric window; `session_digest: true` (N31 header of the best two sentences of each hit session) | `replay_window`, `session_digest` | 15-20 (window 5 sim. 26 incl. other classes) | +12 to +16 |
| B cut-off | 74 | larger pool (above), rerank pool, enrichment; `rrf_k: 10` marginal | `candidate_pool`, `rerank_keep` | 45-55 | +35 to +45 |
| C4 date-dependent | 10 (cat2) | resolve "yesterday/last week" against the turn's own time so the turn matches the date in the question: `temporal_leg_mentions: true`, `temporal_infer_year: true`, `lexical_dates: true` (write-side date words in BM25), `rerank_date_prefix` | existing | 5-8 | +4 to +6 |
| C1 image-caption | 49 (cat1 28, cat3 18) | larger k (their median cosine rank is 123, so only partly reachable); index the caption as its own unit or sentence (new code); `maxsim_leg` (embeds sentences at read time; not simulated, `sentence_leg` lexical variant was net negative); better captions (VLM) | `maxsim_leg`, new code | 8-12 | +5 to +8 |
| C3 no anchor | 33 | query decomposition / subquery probes for multi-hop: `planner: llm`, `planner_version: v2/v3` (lookup subqueries as extra RRF legs), `compose_rewrites`; entity-linked expansion | existing (needs the `plan` and `query_rewrite` LLM roles) | 5-10 (judgement) | +3 to +7 |
| C5 enumeration | 14 | list-question routing with a larger budget share: `aggregate_in_replay` + `aggregate_top_k: 30`, `completeness_check`, `gist_after` (more distinct evidence per token), mined-fact cards (`cards: header`, needs `consolidation.mine_facts`) | existing | 4-7 | +3 to +6 |
| C2 short/anaphoric | 4 | contextual enrichment (previous turn), `rerank_context` | new code / existing | 2-3 | +2 |

Overlap: the classes are not additive. A plausible stack, `candidate_pool: 3` + `rerank_keep` + `relative_floor` fix + `temporal_leg_mentions` + `aggregate_in_replay`, recovers about 60-85 questions (+47 to +65 correct, +3.0 to +4.2 points, 74.4% to about 77.5-78.5%). Going further (decomposition probes, caption units, after-window) might add 15-25 questions and would start to run into the reader ceiling (cat3 accuracy given complete gold is only 0.30). The other 53% of errors (183 reader failures) is outside retrieval.

### 5.4 Deferred (do not start)

A word2vec/static-embedding + BM25 leg (query-term expansion to close the vocabulary gap): the diagnosis supports it in principle, since 69% of missing gold turns have zero content-word overlap with the question, but a lexical expansion helps only those within reach of an expanded synonym (e.g. "family members" to "mother"); class B items with vector rank 5-30 and BM25 rank 150-450 (Q941) are the likeliest beneficiaries. It belongs on the future to-do list; it was not simulated.

## 6. Caveats

- The simulation reproduces the actual context closely (Jaccard 0.90) but not exactly; counts have roughly +-6 questions of noise.
- "Recovered" counts coverage. Gold lists are incomplete (106 incomplete questions were answered correctly) and the reader may be hurt or helped by bigger contexts; the reranker runs show that *smaller* contexts cost accuracy even with complete gold, which argues against fearing a 2-3x larger context at this budget.
- Per-class labels use heuristics (regexes, content-word overlap); borderline cases between C3 and C5, or C1 and C3, exist. Class A/B boundaries depend on the simulated hit set.
- The rerank-score-level analysis (rank before/after) and the pool=3 reranker run were not possible without the GPU/reranker weights; the 94-100% gold-hit retention is inferred from which hits survived.

## Appendix A: the 210 wrong questions with a retrieval problem

Column codes: class A, B, C1-C5 as in 2.2; `v` = cosine rank of the gold turn among all turns of its conversation, `b` = BM25 rank (`-` if no query term matches), `f` = rank in the fused list of the two legs' top-30 (`-` if in neither). Gold present = gold turns in context / valid gold turns.

| Q# | cat | outcome | question | missing gold turns: id [class, vector rank, bm25 rank, fused rank] | gold present |
|---|---|---|---|---|---|
| 3 | 1 | miss | What did Caroline research? | D2:8 [B,v8,b186,f19] | 0/1 |
| 7 | 1 | miss | What is Caroline's relationship status? | D3:13 [B,v29,b382,f54], D2:14 [A,v86,b342,f-] | 0/2 |
| 11 | 1 | partial | Where did Caroline move from 4 years ago? | D4:3 [C3,v245,b34,f-] | 1/2 |
| 14 | 3 | partial | Would Caroline still want to pursue counseling as a career if she hadn't re... | D3:5 [B,v71,b11,f16] | 1/2 |
| 15 | 1 | partial | What activities does Melanie partake in? | D9:1 [C5,v61,b94,f-], D1:12 [C1,v136,b286,f-], D1:18 [C5,v36,b227,f-] | 1/4 |
| 19 | 1 | miss | What do Melanie's kids like? | D6:6 [B,v7,b328,f14], D4:8 [B,v22,b135,f43] | 0/2 |
| 34 | 1 | miss | What events has Caroline participated in to help children? | D9:2 [B,v36,b29,f53], D3:3 [C3,v251,b138,f-] | 0/2 |
| 35 | 2 | miss | When did Melanie go camping in July? | D9:1 [B,v11,b37,f18] | 0/1 |
| 37 | 1 | miss | What did Melanie paint recently? | D8:6 [B,v6,b210,f11], D9:17 [B,v18,b180,f31] | 0/2 |
| 38 | 1 | miss | What activities has Melanie done with her family? | D8:4 [C1,v89,b136,f-], D8:6 [C1,v122,b150,f-], D9:1 [C5,v31,b44,f-], D6:4 [C1,v32,b177,f-], D1:18 [C5,v40,b133,f-], D3:14 [B,v18,b306,f34] | 0/6 |
| 40 | 1 | partial | How many times has Melanie gone to the beach in 2023? | D6:16 [B,v39,b15,f24] | 1/2 |
| 42 | 3 | miss | Would Melanie be more interested in going to a national park or a theme park? | D10:12 [B,v11,b187,f22], D10:14 [C1,v109,b156,f-] | 0/2 |
| 48 | 1 | partial | What types of pottery have Melanie and her kids made? | D12:14 [C3,v207,b365,f-] | 2/3 |
| 51 | 1 | partial | What has Melanie painted? | D8:6 [B,v9,b213,f18] | 2/3 |
| 61 | 1 | partial | What musical artists/bands has Melanie seen? | D11:3 [B,v11,b53,f25] | 1/2 |
| 64 | 3 | miss | Would Melanie likely enjoy the song "The Four Seasons" by Vivaldi? | D15:28 [A,v7,b305,f15] | 0/1 |
| 69 | 3 | miss | What personality traits might Melanie say Caroline has? | D16:18 [A,v236,b243,f-], D13:16 [C3,v65,b148,f-], D7:4 [B,v14,b58,f29] | 0/3 |
| 75 | 1 | partial | How many children does Melanie have? | D18:1 [C1,v101,b310,f-] | 1/2 |
| 155 | 1 | miss | What do Jon and Gina both have in common? | D1:2 [B,v23,b292,f40], D1:3 [C5,v147,b99,f-], D1:4 [C5,v202,b242,f-], D2:1 [B,v162,b7,f17] | 0/4 |
| 169 | 1 | partial | Why did Gina decide to start her own clothing store? | D6:8 [B,v19,b126,f33] | 1/2 |
| 183 | 1 | miss | How long did it take for Jon to open his studio? | D1:2 [B,v32,b30,f56], D15:13 [C1,v42,b349,f-] | 0/2 |
| 190 | 2 | miss | When did Gina go to a dance class with a group of friends? | D19:6 [B,v51,b1,f11] | 0/1 |
| 197 | 4 | miss | What is Jon's attitude towards being part of the dance festival? | D1:28 [B,v98,b5,f13] | 0/1 |
| 208 | 4 | miss | What did Jon and Gina compare their entrepreneurial journeys to? | D6:15 [A,v237,b115,f-], D6:16 [A,v73,b149,f-] | 0/2 |
| 212 | 4 | miss | What does Jon's dance make him? | D9:5 [B,v7,b85,f15] | 0/1 |
| 226 | 4 | miss | What does Jon plan to do at the grand opening of his dance studio? | D15:9 [A,v223,b147,f-] | 0/1 |
| 227 | 4 | miss | What does Gina say to Jon about the grand opening? | D15:12 [B,v21,b297,f39] | 0/1 |
| 236 | 1 | miss | What type of volunteering have John and Maria both done? | D3:5 [C3,v206,b497,f-], D2:1 [B,v26,b609,f47] | 0/2 |
| 239 | 1 | partial | Where has Maria made friends? | D2:1 [C5,v405,b447,f-], D19:1 [C5,v79,b500,f-], D14:10 [B,v12,b333,f24] | 1/4 |
| 241 | 3 | miss | What might John's financial status be? | D5:5 [C3,v81,b253,f-] | 0/1 |
| 243 | 2 | miss | When did Maria meet Jean? | D7:1 [C4,v177,b394,f-] | 0/1 |
| 244 | 1 | miss | What people has Maria met and helped while volunteering? | D7:5 [B,v77,b2,f10], D6:5 [A,v118,b74,f-], D27:8 [A,v191,b326,f-], D21:19 [C5,v57,b561,f-] | 0/4 |
| 248 | 1 | partial | What writing classes has Maria taken? | D9:1 [A,v7,b451,f11] | 1/2 |
| 258 | 1 | partial | What European countries has Maria been to? | D8:15 [B,v5,b260,f11] | 1/2 |
| 263 | 1 | partial | What shelters does Maria volunteer at? | D2:1 [B,v31,b4,f11], D17:12 [B,v16,b99,f30] | 1/3 |
| 268 | 1 | partial | What states has Maria vacationed at? | D18:3 [B,v9,b518,f19] | 1/2 |
| 273 | 1 | partial | What are the names of John's children? | D22:7 [C3,v120,b536,f-] | 1/2 |
| 274 | 3 | miss | Does John live close to a beach or the mountains? | D22:15 [C1,v182,b71,f-] | 0/1 |
| 275 | 1 | partial | What area was hit by a flood? | D14:21 [B,v141,b10,f18] | 1/2 |
| 278 | 3 | miss | Would John be open to moving to another country? | D24:3 [C3,v157,b291,f-], D7:2 [C1,v290,b338,f-] | 0/2 |
| 280 | 1 | partial | What exercises has John done? | D10:1 [C5,v33,b561,f-], D1:4 [B,v8,b209,f14] | 2/4 |
| 282 | 1 | partial | What food item did Maria drop off at the homeless shelter? | D25:19 [C3,v617,b-,f-] | 1/2 |
| 297 | 3 | miss | What job might Maria pursue in the future? | D32:14 [C1,v281,b132,f-], D5:8 [C5,v96,b243,f-], D11:10 [C5,v327,b377,f-], D27:4 [C5,v177,b296,f-] | 0/4 |
| 307 | 4 | miss | Why did Maria sit with the little girl at the shelter event in February 2023? | D5:10 [A,v10,b28,f16] | 0/1 |
| 317 | 4 | miss | What did Maria participate in last weekend before April 10, 2023? | D10:10 [B,v29,b1,f3] | 0/1 |
| 340 | 4 | miss | What new activity did Maria start recently, as mentioned on 3 June, 2023? | D17:12 [C3,v233,b540,f-] | 0/1 |
| 347 | 4 | miss | How does John describe the support he received during his journey to becomi... | D19:12 [A,v6,b109,f12] | 0/1 |
| 366 | 4 | miss | What did John do the week before August 3, 2023 involving his kids? | D27:9 [A,v78,b32,f-] | 0/1 |
| 386 | 1 | partial | What kind of interests do Joanna and Nate share? | D3:4 [C1,v175,b190,f-], D4:9 [C5,v53,b438,f-], D10:9 [C1,v470,b203,f-], D20:2 [A,v220,b104,f-] | 3/7 |
| 390 | 1 | partial | What are Joanna's hobbies? | D2:25 [B,v26,b406,f51] | 1/2 |
| 396 | 1 | partial | What is Joanna allergic to? | D4:4 [B,v14,b248,f22] | 2/3 |
| 399 | 3 | miss | What nickname does Nate use for Joanna? | D7:1 [C3,v184,b65,f-] | 0/1 |
| 402 | 2 | miss | What movie did Joanna watch on 1 May, 2022? | D10:1 [C4,v31,b438,f-] | 0/1 |
| 407 | 2 | miss | When did Joanna start writing her third screenplay? | D12:13 [B,v32,b6,f16], D12:14 [C3,v172,b415,f-] | 0/2 |
| 408 | 1 | partial | Which of Joanna's screenplay were rejected from production companies? | D2:7 [A,v19,b136,f33] | 4/5 |
| 412 | 1 | miss | What places has Joanna submitted her work to? | D2:7 [B,v7,b207,f16], D16:1 [B,v10,b170,f21] | 0/2 |
| 419 | 1 | partial | What book recommendations has Joanna given to Nate? | D3:17 [C3,v229,b517,f-], D19:16 [A,v285,b90,f-] | 1/3 |
| 427 | 1 | partial | What movies have both Joanna and Nate seen? | D10:1 [B,v22,b263,f42], D22:8 [C3,v242,b242,f-] | 1/3 |
| 436 | 1 | partial | When did Nate get Tilly for Joanna? | D24:2 [A,v32,b142,f-] | 1/2 |
| 441 | 1 | miss | What animal do both Nate and Joanna like? | D5:6 [C1,v205,b338,f-], D26:9 [C3,v101,b241,f-] | 0/2 |
| 442 | 2 | miss | When did Joanna plan to go over to Nate's and share recipes? | D26:19 [B,v6,b25,f4] | 0/1 |
| 443 | 1 | miss | What things has Nate reccomended to Joanna? | D2:14 [C5,v364,b593,f-], D9:12 [C1,v356,b615,f-], D9:14 [C1,v372,b51,f-], D10:11 [C5,v567,b517,f-], D19:17 [C1,v314,b622,f-], D27:23 [C1,v522,b623,f-] | 0/6 |
| 444 | 1 | partial | What does Joanna do to remember happy memories? | D15:9 [B,v10,b493,f20] | 1/2 |
| 446 | 1 | partial | What mediums does Nate use to play games? | D22:2 [A,v23,b533,f40], D27:21 [A,v45,b186,f-] | 1/3 |
| 447 | 1 | miss | How many letters has Joanna recieved? | D14:1 [B,v19,b234,f37], D18:5 [B,v5,b426,f12] | 0/2 |
| 451 | 3 | miss | What alternative career might Nate consider after gaming? | D5:8 [C5,v417,b341,f-], D19:3 [C1,v144,b118,f-], D25:19 [C1,v444,b353,f-], D28:25 [C1,v329,b476,f-] | 0/4 |
| 453 | 3 | partial | How many hikes has Joanna been on? | D7:6 [B,v28,b473,f54], D28:22 [B,v9,b163,f17] | 2/4 |
| 455 | 1 | miss | What activities does Nate do with his turtles? | D25:21 [B,v14,b479,f26], D25:23 [A,v10,b490,f19], D28:31 [B,v18,b37,f32] | 0/3 |
| 459 | 1 | partial | What recommendations has Nate received from Joanna? | D3:17 [C5,v521,b425,f-], D15:14 [C5,v416,b518,f-], D15:15 [C5,v529,b95,f-], D19:15 [C1,v271,b530,f-], D19:16 [C5,v305,b76,f-], D23:26 [C1,v534,b123,f-] | 1/7 |
| 460 | 1 | partial | What are Nate's favorite desserts? | D3:12 [A,v5,b428,f14] | 3/4 |
| 463 | 1 | partial | How many video game tournaments has Nate participated in? | D19:1 [B,v14,b70,f22] | 8/9 |
| 465 | 1 | partial | How many tournaments has Nate won? | D17:1 [B,v8,b52,f15], D22:2 [B,v11,b41,f19] | 5/7 |
| 466 | 1 | partial | What recipes has Joanna made? | D19:8 [B,v22,b402,f36], D21:11 [C1,v83,b41,f-], D22:1 [B,v16,b430,f28], D21:17 [C1,v36,b68,f-] | 5/9 |
| 468 | 1 | partial | What are the skills that Nate has helped others learn? | D18:8 [C1,v127,b129,f-] | 2/3 |
| 472 | 3 | miss | What state did Nate visit? | D29:6 [B,v7,b469,f15] | 0/1 |
| 505 | 4 | miss | What creative activity does Nate joke about pursuing after being inspired b... | D11:16 [A,v24,b473,f42] | 0/1 |
| 507 | 4 | miss | What did Nate do for Joanna on 25 May, 2022? | D13:9 [A,v94,b69,f-] | 0/1 |
| 519 | 4 | miss | What recipe Nate offer to share with Joanna? | D16:10 [C3,v291,b297,f-] | 0/1 |
| 536 | 4 | miss | How did Nate celebrate winning the international tournament? | D19:9 [C3,v212,b388,f-] | 0/1 |
| 558 | 4 | miss | What encouragement does Nate give to Joanna after her setback? | D24:13 [A,v58,b229,f-] | 0/1 |
| 580 | 4 | miss | What does Nate want to do when he goes over to Joanna's place? | D28:29 [B,v210,b20,f40] | 0/1 |
| 583 | 4 | miss | What did Nate share a photo of as a part of his experimentation in November... | D29:10 [C1,v128,b109,f-] | 0/1 |
| 586 | 1 | partial | What items does John collect? | D27:20 [B,v5,b299,f13] | 2/3 |
| 587 | 3 | miss | Would Tim enjoy reading books by C. S. Lewis or John Greene? | D1:14 [C1,v59,b106,f-], D1:16 [C3,v307,b284,f-], D1:18 [C3,v96,b668,f-] | 0/3 |
| 588 | 1 | partial | What books has Tim read? | D1:14 [C1,v50,b123,f-], D6:8 [B,v62,b13,f29], D26:36 [B,v24,b346,f45], D22:13 [A,v14,b49,f30] | 3/7 |
| 589 | 3 | miss | Based on Tim's collections, what is a shop that he would enjoy visiting in ... | D2:9 [A,v155,b184,f-] | 0/1 |
| 591 | 1 | partial | Which geographical locations has Tim been to? | D3:2 [C1,v61,b525,f-], D14:16 [B,v14,b204,f26] | 1/3 |
| 595 | 1 | miss | What sports does John like besides basketball? | D1:7 [B,v20,b96,f37], D2:14 [B,v27,b160,f47], D3:1 [C1,v205,b196,f-], D3:25 [C1,v96,b381,f-] | 0/4 |
| 601 | 1 | partial | How many games has John mentioned winning? | D3:3 [C1,v32,b391,f-], D5:2 [B,v11,b441,f24], D22:4 [C1,v52,b72,f-] | 2/5 |
| 602 | 1 | partial | What authors has Tim read books from? | D1:14 [C1,v45,b161,f-], D26:36 [C5,v31,b380,f-] | 4/6 |
| 603 | 3 | partial | What is a prominent charity organization that John might want to work with ... | D3:13 [C3,v43,b167,f-], D3:15 [C1,v195,b254,f-] | 1/3 |
| 604 | 2 | partial | Which city was John in before traveling to Chicago? | D5:2 [C4,v201,b84,f-] | 3/4 |
| 608 | 2 | partial | Where was John between August 11 and August 15 2023? | D7:1 [B,v16,b46,f33] | 2/3 |
| 609 | 1 | partial | What similar sports collectible do Tim and John own? | D7:7 [B,v28,b155,f52], D7:9 [C5,v261,b232,f-] | 2/4 |
| 614 | 1 | partial | Which cities has John been to? | D27:36 [B,v11,b230,f21] | 3/4 |
| 618 | 3 | partial | What could John do after his basketball career? | D26:1 [B,v18,b137,f33], D27:26 [C3,v117,b292,f-] | 1/3 |
| 619 | 1 | partial | What outdoor activities does John enjoy? | D12:6 [B,v5,b336,f12] | 1/2 |
| 622 | 1 | partial | which country has Tim visited most frequently in his travels? | D13:1 [C3,v49,b155,f-], D18:1 [B,v23,b440,f43] | 1/3 |
| 625 | 1 | miss | What kind of fiction stories does Tim write? | D15:3 [B,v11,b287,f21], D16:1 [C3,v85,b526,f-] | 0/2 |
| 629 | 2 | miss | Which country was Tim visiting in the second week of November? | D18:1 [B,v12,b83,f21] | 0/1 |
| 633 | 1 | partial | How many times has John injured his ankle? | D18:2 [B,v8,b73,f15] | 1/2 |
| 634 | 1 | miss | Which book was John reading during his recovery from an ankle injury? | D19:20 [B,v3,b38,f11], D18:2 [B,v54,b30,f51] | 0/2 |
| 637 | 3 | partial | What other exercises can help John with his basketball performance? | D20:2 [C1,v79,b327,f-] | 1/2 |
| 645 | 1 | partial | What books has John read? | D4:10 [A,v26,b215,f37], D17:9 [C5,v37,b366,f-] | 4/6 |
| 651 | 3 | partial | What would be a good hobby related to his travel dreams for Tim to pick up? | D4:1 [C5,v252,b336,f-], D6:6 [C1,v70,b213,f-], D15:3 [C5,v51,b294,f-] | 1/4 |
| 691 | 4 | miss | What city did Tim suggest to John for the team trip next month? | D11:10 [A,v84,b13,f25] | 0/1 |
| 702 | 4 | miss | What did John's team win at the end of the season? | D13:8 [B,v9,b381,f18] | 0/1 |
| 762 | 2 | miss | Which year did Audrey adopt the first three of her dogs? | D1:7 [B,v19,b518,f35] | 0/1 |
| 764 | 1 | miss | What kind of indoor activities has Andrew pursued with his girlfriend? | D13:1 [B,v18,b301,f35], D23:1 [B,v30,b45,f56], D25:1 [B,v11,b53,f24], D19:15 [A,v23,b107,f42] | 0/4 |
| 765 | 1 | partial | What kind of places have Andrew and his girlfriend checked out around the c... | D4:2 [C1,v69,b140,f-], D6:1 [B,v14,b68,f25], D13:1 [B,v28,b499,f47], D23:3 [C1,v82,b74,f-] | 4/8 |
| 774 | 1 | partial | What outdoor activities has Andrew done other than hiking in nature? | D17:1 [C3,v65,b78,f-], D14:1 [B,v19,b103,f33] | 1/3 |
| 777 | 1 | miss | What is a shared frustration regarding dog ownership for Audrey and Andrew? | D7:8 [C3,v139,b197,f-], D10:5 [C1,v61,b146,f-] | 0/2 |
| 779 | 1 | partial | How many times did Audrey and Andew plan to hike together? | D24:13 [B,v5,b76,f11] | 2/3 |
| 780 | 1 | partial | Where did Audrey get Pixie from? | D11:4 [B,v23,b264,f39] | 1/2 |
| 781 | 3 | miss | What is an indoor activity that Andrew would enjoy doing while make his dog... | D10:12 [C3,v120,b200,f-], D12:1 [C1,v62,b343,f-] | 0/2 |
| 782 | 3 | partial | Which meat does Audrey prefer eating more than others? | D10:23 [A,v16,b264,f30] | 1/2 |
| 784 | 2 | miss | Where did Andrew go during the first weekend of August 2023? | D14:1 [B,v4,b31,f12] | 0/1 |
| 785 | 1 | miss | What are some problems that Andrew faces before he adopted Toby? | D2:12 [C2,v33,b177,f-], D5:3 [C5,v209,b156,f-], D5:5 [B,v30,b443,f51], D5:7 [C5,v221,b453,f-] | 0/4 |
| 786 | 1 | miss | Did Audrey and Andrew grow up with a pet dog? | D2:16 [C1,v85,b37,f-], D13:10 [C3,v91,b197,f-] | 0/2 |
| 793 | 1 | miss | What has Andrew done with his dogs? | D14:27 [C3,v235,b370,f-], D24:8 [A,v150,b448,f-] | 0/2 |
| 795 | 3 | partial | What can Andrew potentially do to improve his stress and accomodate his liv... | D21:5 [C3,v171,b311,f-] | 2/3 |
| 797 | 1 | miss | What are the names of Andrew's dogs? | D12:1 [B,v13,b292,f26], D24:6 [C3,v77,b396,f-], D28:8 [C3,v72,b611,f-] | 0/3 |
| 805 | 3 | miss | Which US state do Audrey and Andrew potentially live in? | D11:9 [C1,v305,b667,f-] | 0/1 |
| 806 | 3 | partial | Which national park could Audrey and Andrew be referring to in their conver... | D11:9 [C1,v124,b155,f-] | 1/2 |
| 807 | 2 | miss | How many pets will Andrew have, as of December 2023? | D12:1 [B,v25,b105,f47], D24:2 [B,v13,b52,f26], D28:6 [B,v30,b59,f56] | 0/3 |
| 808 | 2 | miss | How many pets did Andrew have, as of September 2023? | D12:1 [B,v16,b106,f30], D24:2 [B,v8,b51,f16] | 0/2 |
| 814 | 3 | partial | What is something that Andrew could do to make birdwatching hobby to fit in... | D20:5 [C5,v284,b262,f-], D23:1 [C1,v154,b225,f-], D1:14 [A,v23,b179,f41] | 1/4 |
| 815 | 3 | miss | What is a career that Andrew could potentially pursue with his love for ani... | D2:18 [B,v52,b22,f39], D3:1 [C5,v140,b60,f-], D5:7 [A,v36,b95,f-], D8:27 [C5,v86,b80,f-] | 0/4 |
| 817 | 2 | miss | When did Andrew make his dogs a fun indoor area? | D28:12 [C4,v52,b369,f-] | 0/1 |
| 818 | 1 | partial | Has Andrew moved into a new apartment for his dogs? | D28:12 [C3,v41,b74,f-] | 1/2 |
| 867 | 4 | miss | What type of games do Audrey's dogs like to play at the park? | D23:14 [B,v5,b309,f13] | 0/1 |
| 886 | 2 | miss | Which recreational activity was James pursuing on March 16, 2022? | D1:26 [C4,v84,b372,f-] | 0/1 |
| 887 | 1 | partial | Which places or events have John and James planned to meet at? | D1:36 [C2,v161,b582,f-], D21:15 [A,v28,b373,f54] | 2/4 |
| 890 | 1 | miss | What are John and James' favorite games? | D3:11 [B,v152,b26,f49], D4:16 [C1,v110,b314,f-] | 0/2 |
| 891 | 3 | miss | Does James live in Connecticut? | D5:1 [C1,v77,b60,f-] | 0/1 |
| 896 | 2 | miss | How was John feeling on April 10, 2022? | D6:7 [C4,v164,b526,f-] | 0/1 |
| 904 | 3 | miss | Was James feeling lonely before meeting Samantha? | D9:16 [C3,v104,b217,f-] | 0/1 |
| 909 | 1 | miss | What kind of games has James tried to develop? | D13:7 [C3,v135,b424,f-], D1:4 [C3,v189,b46,f-], D27:2 [C1,v41,b194,f-] | 0/3 |
| 912 | 1 | partial | What kind of classes has James joined? | D13:6 [B,v15,b107,f29] | 1/2 |
| 917 | 2 | miss | Where was James at on July 12, 2022? | D16:9 [A,v30,b193,f57] | 0/1 |
| 918 | 3 | miss | Did John and James study together? | D17:13 [C1,v490,b667,f-] | 0/1 |
| 936 | 1 | partial | Which new games did John start play during the course of the conversation w... | D5:4 [C1,v132,b71,f-], D19:7 [A,v225,b254,f-], D8:20 [C5,v60,b245,f-] | 3/6 |
| 938 | 2 | partial | How long did it take for James to complete his Witcher-inspired game? | D27:2 [A,v12,b43,f23] | 1/2 |
| 941 | 1 | partial | Which of James's family members have visited him in the last year? | D28:19 [B,v5,b157,f14] | 1/2 |
| 960 | 4 | miss | What game was James playing in the online gaming tournament in April 2022? | D4:16 [B,v22,b54,f33] | 0/1 |
| 962 | 4 | miss | What advice did James receive from the famous players he met at the tournam... | D4:12 [A,v23,b251,f43] | 0/1 |
| 1036 | 1 | partial | Which of Deborah`s family and friends have passed away? | D6:4 [B,v12,b429,f18] | 2/3 |
| 1041 | 1 | partial | What symbolic gifts do Deborah and Jolene have from their mothers? | D1:9 [A,v21,b31,f40] | 1/2 |
| 1054 | 1 | partial | How many times has Jolene been to France? | D1:8 [C1,v57,b261,f-] | 1/2 |
| 1055 | 1 | partial | Which games have Jolene and her partner played together? | D2:30 [A,v71,b436,f-], D20:1 [B,v15,b551,f30], D15:10 [A,v19,b31,f37], D19:10 [A,v14,b56,f28] | 1/5 |
| 1058 | 3 | miss | Why did Jolene sometimes put off doing yoga? | D3:11 [C3,v129,b176,f-], D2:30 [C3,v415,b167,f-] | 0/2 |
| 1071 | 3 | miss | How old is Jolene? | D8:2 [C5,v374,b100,f-], D13:5 [C1,v258,b456,f-], D21:6 [C5,v329,b171,f-], D21:8 [C5,v254,b481,f-], D22:6 [C5,v378,b191,f-], D22:14 [C5,v183,b320,f-], D24:2 [C5,v332,b132,f-], D24:14 [C5,v56,b455,f-], D25:5 [C2,v288,b261,f-], D26:6 [C5,v478,b442,f-] | 0/10 |
| 1078 | 2 | miss | When did Deborah go for a bicycle ride with Anna? | D12:1 [B,v8,b177,f15] | 0/1 |
| 1081 | 2 | partial | How long did Jolene work on the robotics project given to her by her Profes... | D12:10 [B,v4,b32,f11] | 2/3 |
| 1083 | 3 | miss | Which US state did Jolene visit during her internship? | D13:15 [A,v39,b389,f-] | 0/1 |
| 1085 | 2 | miss | Which year did Jolene start practicing yoga? | D13:17 [B,v18,b293,f34] | 0/1 |
| 1087 | 2 | miss | When did Jolene lose a lot of progress in her work? | D16:2 [B,v11,b86,f21] | 0/1 |
| 1090 | 2 | partial | Which pet did Jolene adopt more recently - Susie or Seraphim? | D2:28 [C3,v183,b188,f-] | 2/3 |
| 1113 | 2 | miss | Where did Jolene and her partner spend most of September 2023? | D2:1 [C4,v328,b335,f-] | 0/1 |
| 1119 | 1 | partial | What kind of engineering projects has Jolene worked on? | D1:2 [B,v6,b43,f16], D4:5 [B,v22,b529,f36], D17:10 [B,v2,b192,f11], D17:12 [B,v26,b531,f44] | 1/5 |
| 1121 | 1 | partial | What gifts has Deborah received? | D23:20 [C1,v94,b343,f-], D23:22 [C5,v186,b75,f-] | 3/5 |
| 1123 | 1 | partial | What activities does Deborah pursue besides practicing and teaching yoga? | D12:1 [C1,v66,b220,f-], D15:1 [C1,v279,b524,f-], D28:11 [C5,v89,b187,f-], D29:1 [C5,v126,b219,f-] | 1/5 |
| 1142 | 4 | miss | What does Deborah bring with her whenever she comes to reflect on her mom? | D4:36 [B,v37,b1,f15] | 0/1 |
| 1156 | 4 | miss | What did Jolene ask Deb to help with on 13 March, 2023? | D9:14 [C3,v167,b226,f-] | 0/1 |
| 1175 | 4 | miss | What is the favorite game Jolene plays with her partner? | D15:10 [B,v5,b74,f11] | 0/1 |
| 1193 | 4 | miss | What did Jolene participate in recently that provided her with a rewarding ... | D21:6 [B,v73,b8,f15] | 0/1 |
| 1209 | 4 | miss | Why did Jolene have to reschedule their meeting with Deborah on September 8... | D26:15 [A,v83,b176,f-] | 0/1 |
| 1214 | 4 | miss | What did Jolene recently play that she described to Deb? | D27:12 [C1,v467,b34,f-] | 0/1 |
| 1223 | 4 | miss | What outdoor activity did Jolene suggest doing together with Deborah? | D29:27 [C2,v196,b46,f-] | 0/1 |
| 1233 | 1 | partial | What new hobbies did Sam consider trying? | D2:10 [B,v9,b55,f18], D10:8 [B,v17,b363,f30], D20:6 [B,v3,b267,f11], D7:2 [C5,v97,b127,f-], D7:4 [C1,v240,b366,f-], D7:6 [C5,v153,b117,f-], D21:19 [B,v7,b349,f14] | 3/10 |
| 1240 | 1 | partial | What is Evan's favorite food? | D5:5 [C1,v44,b285,f-], D22:12 [A,v34,b447,f-] | 2/4 |
| 1244 | 1 | partial | What kind of healthy food suggestions has Evan given to Sam? | D3:5 [A,v274,b119,f-], D22:14 [A,v35,b104,f-], D24:15 [A,v5,b156,f12] | 2/5 |
| 1246 | 3 | partial | In light of the health and dietary changes discussed, what would be an appr... | D3:5 [A,v197,b16,f30], D4:10 [A,v100,b69,f-], D14:12 [A,v346,b211,f-], D5:9 [C1,v59,b40,f-], D7:12 [B,v307,b25,f45], D8:1 [B,v17,b281,f31], D8:5 [C5,v357,b437,f-], D8:7 [C1,v320,b111,f-], D8:8 [C5,v268,b347,f-], D8:12 [C5,v150,b307,f-], D9:1 [C5,v43,b47,f-] | 6/17 |
| 1248 | 2 | miss | When Evan did meet his future wife? | D5:1 [B,v5,b379,f11] | 0/1 |
| 1251 | 2 | miss | Which year did Evan start taking care of his health seriously? | D5:6 [C3,v392,b77,f-], D5:7 [B,v3,b207,f14] | 0/2 |
| 1252 | 1 | partial | What motivates Evan to take care of his health? | D5:11 [A,v131,b392,f-], D5:13 [A,v85,b258,f-] | 1/3 |
| 1258 | 1 | partial | What recurring frustration does Evan experience? | D21:20 [B,v6,b62,f12] | 1/2 |
| 1262 | 1 | partial | What kind of foods or recipes has Sam recommended to Evan? | D23:26 [C1,v164,b207,f-] | 2/3 |
| 1263 | 1 | partial | What kind of healthy meals did Sam start eating after getting a health scare? | D8:1 [A,v3,b148,f13], D18:6 [B,v30,b123,f49] | 5/7 |
| 1269 | 3 | partial | How often does Sam get health checkups? | D2:6 [B,v4,b328,f12], D7:2 [C3,v117,b83,f-] | 1/3 |
| 1275 | 1 | partial | Who was injured in Evan's family? | D7:9 [B,v8,b39,f17], D7:10 [C5,v364,b284,f-], D9:2 [C5,v197,b200,f-], D11:2 [B,v10,b67,f21], D11:3 [C5,v38,b396,f-] | 1/6 |
| 1276 | 1 | miss | What kind of hobbies does Evan pursue? | D1:14 [A,v10,b91,f22], D1:6 [A,v81,b248,f-], D4:8 [C1,v363,b163,f-], D6:1 [B,v7,b209,f16], D8:30 [B,v4,b264,f12], D9:6 [C5,v153,b376,f-], D25:8 [C1,v187,b83,f-], D25:10 [C5,v60,b358,f-] | 0/8 |
| 1277 | 3 | miss | What challenges does Sam face in his quest for a healthier lifestyle, and h... | D4:2 [C5,v141,b472,f-], D4:6 [C1,v70,b260,f-], D14:1 [C5,v35,b148,f-], D14:2 [C5,v65,b120,f-] | 0/4 |
| 1281 | 1 | partial | What recurring adventure does Evan have with strangers? | D14:2 [B,v238,b11,f21] | 1/2 |
| 1288 | 1 | partial | What health scares did Sam and Evan experience? | D14:1 [B,v23,b46,f39], D14:2 [C5,v108,b272,f-] | 2/4 |
| 1290 | 1 | partial | Which ailment does Sam have to face due to his weight? | D7:2 [C5,v34,b83,f-], D14:1 [B,v16,b41,f29] | 2/4 |
| 1296 | 2 | partial | How long did Evan and his partner date before getting married? | D5:1 [B,v6,b80,f12] | 1/2 |
| 1303 | 1 | miss | How does Evan spend his time with his bride after the wedding? | D23:15 [C5,v72,b285,f-], D23:23 [B,v16,b135,f31], D23:25 [C5,v127,b155,f-], D24:9 [C5,v316,b456,f-] | 0/4 |
| 1308 | 1 | partial | What is a stress reliever for Sam? | D16:17 [C5,v221,b51,f-] | 5/6 |
| 1316 | 4 | miss | What new suggestion did Evan give to Sam regarding his soda and candy consu... | D3:5 [B,v184,b3,f13] | 0/1 |
| 1336 | 4 | miss | What injury did Evan suffer from in August 2023? | D9:2 [C3,v41,b424,f-] | 0/1 |
| 1385 | 1 | partial | Which bands has Dave enjoyed listening to? | D23:9 [C2,v193,b323,f-] | 1/2 |
| 1392 | 2 | miss | When did Calvin's place get flooded in Tokyo? | D6:3 [B,v43,b4,f18] | 0/1 |
| 1393 | 1 | partial | What mishaps has Calvin run into? | D6:1 [B,v102,b7,f15] | 1/2 |
| 1409 | 2 | miss | Which city was Calvin visiting in August 2023? | D16:6 [A,v70,b97,f-] | 0/1 |
| 1411 | 1 | miss | What are Dave's hobbies other than fixing cars? | D5:9 [C5,v221,b279,f-], D5:11 [C5,v333,b77,f-], D8:8 [C5,v181,b287,f-], D27:2 [C5,v139,b37,f-] | 0/4 |
| 1413 | 2 | partial | Where was Dave in the last two weeks of August 2023? | D17:1 [C4,v91,b532,f-] | 1/2 |
| 1425 | 2 | miss | Which city was Calvin at on October 3, 2023? | D21:1 [C4,v72,b70,f-] | 0/1 |
| 1427 | 1 | miss | What shared activities do Dave and Calvin have? | D21:3 [B,v19,b495,f34], D21:4 [C3,v270,b485,f-] | 0/2 |
| 1438 | 1 | miss | Which cities did Dave travel to in 2023? | D14:1 [C3,v32,b68,f-], D26:2 [B,v11,b44,f20] | 0/2 |
| 1439 | 2 | miss | Which hobby did Dave pick up in October 2023? | D27:2 [C4,v63,b456,f-] | 0/1 |
| 1441 | 1 | partial | How many Ferraris does Calvin own? | D2:1 [B,v10,b389,f20] | 1/2 |
| 1445 | 1 | partial | What style of guitars does Calvin own? | D16:4 [C5,v221,b158,f-] | 3/4 |
| 1448 | 1 | partial | Do all of Dave's car restoration projects go smoothly? | D27:10 [B,v8,b161,f15], D13:7 [B,v16,b93,f29], D20:1 [B,v11,b128,f20] | 1/4 |
| 1450 | 2 | miss | When did Dave find the car he repaired and started sharing in his blog? | D28:20 [B,v8,b134,f19] | 0/1 |
| 1461 | 4 | miss | What does Dave do when he feels his creativity is frozen? | D5:11 [B,v7,b487,f15] | 0/1 |
| 1479 | 4 | miss | What does Dave say is important for making his custom cars unique? | D13:11 [B,v19,b44,f31] | 0/1 |
| 1502 | 4 | miss | What does Dave aim to do with his passion for cars? | D22:5 [C3,v43,b45,f-] | 0/1 |
| 1534 | 4 | miss | What new item did Dave buy recently? | D30:5 [C1,v201,b100,f-] | 0/1 |
| 1536 | 4 | miss | What event did Calvin attend in Boston? | D30:2 [B,v7,b53,f17] | 0/1 |