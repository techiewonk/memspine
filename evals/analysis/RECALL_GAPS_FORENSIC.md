# Why gold evidence does not reach the reader on LoCoMo: forensic analysis of three full runs

Scope: LoCoMo, 1,540 non-adversarial questions (single-hop 841, multi-hop 282, temporal 321, open-domain 96), branch `feat/local-qwen-stack`. Analysis only: nothing in the engine, harness or run folders was changed; no GPU, Ollama or eval run was used (only CPU re-embedding with Qwen3-Embedding-0.6B and a CPU BM25 for the offline what-if simulations).

Runs analysed (per-question stage logs, `evals/runs/<run>--memspine/report/per_question.jsonl`):

| label | run | config in one line | accuracy |
|---|---|---|---|
| **fix** | `qa-full-qs-eq06-fix` | grounded prompt, refusal retry, judge guards, replay window 2 before / 4 after, `relative_dates_anchored`, `temporal_leg_mentions`, no reranker | 80.1% |
| **base** | `qa-full-qs-eq06-roff-fx` | same engine, replay window +-2, no reranker | 74.5% |
| **rr** | `qa-full-qs-eq06-rq4b4-fx` | base + Qwen3-Reranker-4B (4-bit), pool = fused top-10 | 70.8% |

**Conventions.** MEASURED = read directly from the logged stage lists, or an exact replay of them (the fusion and the replay-window context were re-computed from the logged legs and reproduce the logged `fused` list and `retrieved_turns` in 1,540 / 1,540 questions for fix and for base, and the reranker context in 1,540 / 1,540 for rr). SIMULATED = an offline what-if built on those lists (fusion, window and budget logic as the engine; BM25 and embeddings re-computed on CPU; the CPU replica matches the logged vector top-30 at 97.8% and the BM25 top-30 at 97.1%, and its baseline is within +-3 questions of the real one). ESTIMATED = judgement extrapolating from measured numbers, always labelled. The cause labels in section 2 come from stated rules (heuristics); the ranks and counts under them are measured. 'Gold' = the dataset evidence turns; 8 gold ids of the 2,356 do not exist in the dataset and are dropped (2,348 gold turns remain). `conv-NN qid` are the run's own ids.

## 0. Summary

**Gold-turn funnel (fix run, 2348 gold turns).** 1404 (59.8%) are themselves one of the 10 fused hits; 427 (18.2%) reach the context only because the neighbour window of some other hit covers them (262 of those were in a leg's top-30 but outside the fused top-10, 165 were in no leg at all); **517 (22.0%) never reach the context: 280 lost at recall (in no leg's top-30) and 237 lost at fusion (in a leg's top-30, outside the fused top-10).** These 517 turns sit in 281 questions; 158 of them were answered wrong (94 primary gap recall + 64 fusion), the other 123 were answered correctly anyway (partial gold is often enough). The reader gap (148 questions: all gold in context, still wrong) is out of scope here.

**What the data says**

1. Retrieval loss is a *multi-evidence / indirect-evidence* problem, not a single-turn matching problem. Lost gold turns by question category: single-hop 31, multi-hop 347, open-domain 96, temporal 43. 87% of lost gold turns belong to questions with 2+ gold turns, 50% to questions with 4+.
2. Largest cause: **list / aggregation questions whose gold turns are instances of a category the question names generically** ("hobbies", "activities", "places", "books"): 164 lost turns (32% of the 517); 52% of all such gold turns are lost (22% overall). The turn says "kayaking" / "Valorant" / "Name of the Wind"; question and turn share no content word and the embedding does not bridge category to instance.
3. Next: **the gold turn does not literally contain the answer** (112 turns, 22%: implicit, paraphrased, or a weakly related / noisy gold label) and **open-domain inference** (96 turns, 19%: the evidence is an implication). About half of each is lost whatever the retrieval setting, and a 'perfect decomposition' oracle cannot target them.
4. Fusion loss (237 turns) is structural to RRF over a vector leg and an OR-of-every-token BM25 leg: 195 of 237 fusion-lost turns are in the vector leg only, 37 in BM25 only, and **only 1 in both**. The fused top-10 holds on average 5.9 two-leg items per question, 42% of which sit at vector rank 11 or worse (17% at 21-30), and 34% of BM25's own top-10 hits share *no content word* with the question. Re-weighting, rrf_k, reserved slots and the existing query-probe options do not fix it (sections 3 and 7); only a larger candidate pool does.
5. The neighbour window is already near its useful maximum: window 2 before / 4 after rescues 427 gold turns (+55 gold turns and +29 fully covered questions over the base run's symmetric +-2). Of the 517 lost turns, 388 (75%) sit in a session with no hit at all, so no window shape can reach them (section 5).
6. The reranker run loses accuracy not because it mis-scores gold (it keeps 97.4% of gold hits) but because pool=10 makes it a pure deleter: min-max normalisation plus `relative_floor 0.3` removes ~55% of the non-gold hits *together with their windows*; 74% of the gold turns lost to the reranker were window neighbours of a dropped hit (section 6).

**Headroom (fix run; questions with ALL gold in context; baseline 1,251 of 1,540; 158 questions are wrong with incomplete gold). SIMULATED, exact fusion / window / budget logic:**

| change | questions fully covered | of the 158 wrong+incomplete, newly covered | avg context tokens (est.) |
|---|---|---|---|
| baseline (fix) | 1,251 | 0 | 2,189 |
| `candidate_pool` x2 (pool 20), window 2/4, budget 4096 | 1,330 (+79) | 39 | 3,967 |
| pool 30, tiered window: ranks 1-10 window 2/4, ranks 11-30 window 0/1 (window-by-rank needs code) | 1,341 (+90) | 50 | 3,447 |
| pool 20, tiered window (2/4 then 0/1) | 1,308 (+57) | 32 | 2,812 |
| ORACLE reranker: best 10 of the fused top-30, window 2/4 (upper bound for pool 30 + a perfect reranker) | 1,332 (+81) | 43 | 2,177 |
| BM25 indexed like the engine `english` analyzer (stop words + stemming) and speaker names dropped from the query | 1,274 (+23) | 34 | 2,104 |

Reader accuracy given complete gold is 82.6% overall in the rr run (89% single-hop, 82% multi-hop, 72% temporal, 25% open-domain), so expected extra correct answers are about 0.8 x the newly covered wrong questions, ESTIMATED. Larger contexts may dilute the reader; the logs cannot show that (risk, section 8).

## 1. The funnel: where each gold turn ends up (MEASURED)

Gold-turn state per run. "hit" = gold turn is one of the 10 fused hits (rr: one of the hits that survive the floor); "window" = in context only through a neighbour window, split by what the turn was before (fusion: in a leg top-30; recall: in no leg top-30); "lost" = not in the context.

| run | gold turns | hit | window (was fusion-lost) | window (was recall-lost) | lost at fusion | lost at recall | lost at assembly (floor) | in context |
|---|---|---|---|---|---|---|---|---|
| base (+-2 window) | 2348 | 1401 | 231 | 142 | 267 | 307 | 0 | 75.6% |
| fix (2 before / 4 after) | 2348 | 1404 | 262 | 165 | 237 | 280 | 0 | 78.0% |
| rr (reranker, +-2) | 2348 | 1383 | 196 | 125 | 302 | 324 | 18 | 72.6% |

By question category (fix run, gold turns):

| category | gold turns | hit | window only | lost at fusion | lost at recall |
|---|---|---|---|---|---|
| single-hop | 895 | 704 (79%) | 160 (18%) | 21 (2%) | 10 (1%) |
| multi-hop | 882 | 355 (40%) | 180 (20%) | 171 (19%) | 176 (20%) |
| temporal | 374 | 284 (76%) | 47 (13%) | 29 (8%) | 14 (4%) |
| open-domain | 197 | 61 (31%) | 40 (20%) | 16 (8%) | 80 (41%) |

Question level (all gold of the question in the stage): fix run funnel from `run_summary.json`, plus primary-gap counts (earliest stage that lost a gold turn):

| category | questions | all gold in some leg top-30 | all gold in fused top-10 | all gold in context | accuracy | gap: recall | gap: fusion | gap: reader |
|---|---|---|---|---|---|---|---|---|
| single-hop | 841 | 94% | 78% | 96% | 91% | 9 | 12 | 56 |
| multi-hop | 282 | 50% | 22% | 38% | 63% | 50 | 30 | 25 |
| temporal | 321 | 90% | 78% | 88% | 77% | 10 | 19 | 46 |
| open-domain | 92 | 50% | 34% | 53% | 47% | 25 | 3 | 21 |

Base vs fix: the vector and BM25 lists are identical in all 1,540 questions (same embedder, same index); the fused top-10 changed in 76 questions (the new date-mention leg, 187 questions now carry a temporal leg vs 169), moving 4 gold turns into the hits. Almost the whole retrieval gain of the fix run is the 2/4 window: 57 fewer lost gold turns (55 turns newly covered by the window: 31 that were fusion-lost and 24 that were recall-lost; the changed hits add a net 2).

## 2. Task 1: why gold turns are lost (517 turns, fix run)

### 2.1 How turns are classified

Every lost turn gets exactly one cause by the first rule that matches (rules are heuristics over tokens; "content words" exclude stop words and the two speaker names; "stem" uses a Porter stemmer; "answer tokens" are the content words of the gold answer string):

1. **T** question category is temporal.
2. **I** question category is open-domain.
3. **N** (non-temporal, non-open) none of the gold-answer tokens occurs in the turn or its image caption: the answer is not literally there.
4. **H** the question shares no stemmed content word with the turn, the answer tokens are in the turn, and the question has 2+ gold turns (category -> instance). (If only an adjacent turn carries the question words it is still counted here: adjacency is a tag, below.)
5. **P** same as H but a single gold turn.
6. **M** the question shares a stemmed but not an exact content word with the turn.
7. **O** remaining: the turn shares an exact content word with the question and is still out-ranked.

### 2.2 Summary of causes

| cause | lost turns | lost at recall | lost at fusion | share | lost-rate (all gold turns with this label) | by category sh / mh / tp / od | questions (dominant cause), all incomplete | ... of which wrong (158) |
|---|---|---|---|---|---|---|---|---|
| **H** Category-to-instance gap (list / aggregation question) | 164 | 95 | 69 | 32% | 52% of 315 | 0 / 164 / 0 / 0 | 94 | 47 |
| **N** Answer not literally in the gold turn (paraphrase / implicit / noisy label) | 112 | 64 | 48 | 22% | 35% of 323 | 3 / 109 / 0 / 0 | 54 | 28 |
| **I** Open-domain inference (evidence is an implication) | 96 | 80 | 16 | 19% | 49% of 197 | 0 / 0 / 0 / 96 | 40 | 28 |
| **T** Temporal / date-dependent evidence | 43 | 14 | 29 | 8% | 11% of 374 | 0 / 0 / 43 / 0 | 36 | 29 |
| **O** Lexical overlap exists but the turn is outranked or crowded out | 63 | 13 | 50 | 12% | 7% of 900 | 15 / 48 / 0 / 0 | 31 | 11 |
| **M** Morphology-only overlap (BM25 without stemming misses it) | 27 | 9 | 18 | 5% | 25% of 108 | 2 / 25 / 0 / 0 | 14 | 5 |
| **P** Single-hop paraphrase gap | 12 | 5 | 7 | 2% | 9% of 131 | 11 / 1 / 0 / 0 | 12 | 9 |
| **all** | 517 | 280 | 237 | 100% | 22% of 2348 | 31 / 347 / 43 / 96 | 281 | 157 |

Question-level attribution = the most frequent cause among the question's lost turns (ties go to the earlier letter in the table order).

Non-exclusive tags measured over all 2,348 gold turns (lift = lost-rate with tag / overall lost-rate 22.0%; tags that discriminate are bold):

| tag | gold turns | lost | lost-rate | lift | share of the 517 lost |
|---|---|---|---|---|---|
| **no stemmed content-word overlap with the question** (vocabulary mismatch) | 793 | 353 | 45% | 2.02 | 68% |
| no exact content-word overlap (BM25 cannot match at all) | 954 | 404 | 42% | 1.92 | 78% |
| stem-only overlap (BM25 without stemming misses it) | 161 | 51 | 32% | 1.44 | 10% |
| **question has 2+ gold turns** | 1226 | 451 | 37% | 1.67 | 87% |
| **question has 4+ gold turns** | 527 | 260 | 49% | 2.24 | 50% |
| **gold of the question spans 2+ sessions** | 1048 | 434 | 41% | 1.88 | 84% |
| **gold answer tokens not in the turn** (non-temporal) | 447 | 174 | 39% | 1.77 | 34% |
| **short backchannel / anaphoric turn** (<=14 words, "yeah", "that", "it" opener) | 59 | 21 | 36% | 1.62 | 4% |
| turn carries an image caption | 904 | 209 | 23% | 1.05 | 40% |
| answer tokens ONLY in the caption | 67 | 15 | 22% | 1.02 | 3% |
| question overlap ONLY via the caption | 55 | 12 | 22% | 0.99 | 2% |
| relative-time phrase in the turn ("yesterday", "last week") | 752 | 159 | 21% | 0.96 | 31% |
| question names exactly one person, turn spoken by the other | 83 | 19 | 23% | 1.04 | 4% |
| question names exactly one person, turn spoken by that person | 1963 | 406 | 21% | 0.94 | 79% |
| question names both or no speaker | 302 | 92 | 30% | 1.38 | 18% |
| an adjacent turn (+-1) holds the question words | 1289 | 157 | 12% | 0.55 | 30% |

Reading: vocabulary mismatch, many gold turns per question, cross-session evidence and "answer not literally there" are what predict loss. **Image captions, relative-time phrases and speaker identity do not** (lift ~1.0): the image-caption class of the earlier simulation (RETRIEVAL_GAPS C1) is a label, not a cause. Evidence that exists *only* in the caption is 3% of the lost turns (15). Speaker confusion (Caroline vs Melanie) is also not a driver: the lost rate for a turn spoken by the non-named person is 23% vs 22% overall.

### 2.3 Evidence per cause, with examples

Rank notation: `vec 14 (cos 0.60)` = rank and cosine in the logged vector top-30; `vec ~127` = rank among *all* turns of the conversation from the CPU replica (the turn is outside the logged top-30; cos shown with the 30th-place cosine); `BM25 ~242` likewise from the CPU BM25; "own RRF" is the turn's fusion score (normalised like the log) against the score of the 10th fused item.

#### H. Category-to-instance gap: 164 lost turns (95 recall, 69 fusion), all multi-hop

MEASURED. The question asks for a set ("What kind of hobbies does Evan pursue?", "What recommendations has Nate received from Joanna?") and each gold turn names one member of the set. 52% of such gold turns are lost. Median full-conversation vector rank of the recall-lost ones is 110 (conversations have 300-700 turns); only 23 of 95 are within rank 60. In 87 of 164 cases (53%) another gold turn of the same question *is* a hit: the query reaches one or two instances and not the rest. Worst questions: "What kind of hobbies does Evan pursue?" (8 gold turns), "What things has Nate recommended to Joanna?" (6), "What recommendations has Nate received from Joanna?" (7), "What new hobbies did Sam consider trying?" (10).

- **conv-44 5-35** (multi-hop, lost at recall, 3 gold turns). Q: "What are the names of Andrew's dogs?" Gold answer: "Toby, Scout, Buddy".
  - gold D28:8 (session 28): "Andrew: It took us a while to decide, but we ended up going with 'Scout' for our pup - it seemed perfect for their adventurous spirit."
  - ranks: vec ~72 (cos 0.51; 30th = 0.56); BM25 ~610; in no leg top-30.
  - out-ranked by: "Andrew: Thanks, I think that's what I need to hear. I'll take good care of my dogs first."; "Andrew: Yeah, photos are gonna turn out great with the dogs!".
- **conv-50 9-29** (multi-hop, lost at recall, 4 gold turns). Q: "What are Dave's hobbies other than fixing cars?" Gold answer: "take a walk, go hiking, listen to favorite albums, live concerts, photography".
  - gold D8:8 (session 8): "Dave: Nah, haven't gone hiking recently, but it's awesome - being in nature and pushing yourself to new heights. Clears your mind and brings a sense of calm. Have you been to the mountains before? Heard they're super chill."
  - ranks: vec ~182 (cos 0.47; 30th = 0.58); BM25 ~289; in no leg top-30.
  - out-ranked by: "Calvin: Thanks, Dave! It was an amazing experience - the energy and love from the fans was crazy. The car in …"; "Calvin: Hey Dave! Nice to hear from you. That's cool! I totally understand the satisfaction you get from fixi…".
- **conv-42 3-30** (multi-hop, lost at recall, 4 gold turns). Q: "What kind of writings does Joanna do?" Gold answer: "Screenplays,books, online blog posts, journal".
  - gold D2:3 (session 2): "Joanna: Woo! I finally finished my first full screenplay and printed it last Friday. I've been working on for a while, such a relief to have it all done! [image: a photography of a book with a page of text on it]"
  - ranks: vec ~37 (cos 0.54; 30th = 0.55); BM25 ~194; in no leg top-30.
  - out-ranked by: "Joanna: Yeah, it does! My brother wrote it - he used to make me these cute notes when we were kids. Brings ba…"; "Joanna: Thanks Nate! Your support and encouragement mean a lot. Writing isn't always easy but moments like th…".
- **conv-26 0-61** (multi-hop, lost at fusion, 2 gold turns). Q: "What musical artists/bands has Melanie seen?" Gold answer: "Summer Sounds, Matt Patterson".
  - gold D11:3 (session 11): "Melanie: Thanks, Caroline! It was Matt Patterson, he is so talented! His voice and songs were amazing. What's up with you? Anything interesting going on?"
  - ranks: vec 14 (cos 0.51); BM25 ~52; own RRF 0.41 vs 10th fused 0.48.
  - out-ranked by: "Melanie: Nope, never been to something like that. What was it about? What made it so special?"; "Melanie: Cool! What type of music do you play?".
- **conv-49 8-50** (multi-hop, lost at fusion, 8 gold turns). Q: "What kind of hobbies does Evan pursue?" Gold answer: "painting, hiking, reading books, biking, skiing, snowboarding, ice skating, swimming, cam…".
  - gold D8:30 (session 8): "Evan: Skiing, snowboarding, and ice skating are all fun winter activities I enjoy."
  - ranks: vec 4 (cos 0.64); BM25 ~266; own RRF 0.48 vs 10th fused 0.48.
  - out-ranked by: "Evan: What other hobbies have you found for yourself?"; "Evan: Life can be hard sometimes. Do you have any hobbies or activities that make you happy?".

#### N. Answer not literally in the gold turn: 112 lost turns (64 recall, 48 fusion), 109 multi-hop

MEASURED + judgement. None of the gold-answer tokens occurs in the turn or its caption (lost-rate 35% of such turns). Three sub-types are mixed here and cannot be separated mechanically: (a) the turn implies the answer in other words ("lost a friend" for a family-members-who-died question), (b) the answer lives in the *previous* turn and the gold turn is its reply ("Thanks! The turtles might be small..."), (c) the gold label is only weakly related (e.g. a pottery question whose gold turn is "I appreciate our friendship too, Caroline"; a 'How long did it take Jon to open his studio' question whose gold is "Let's make some awesome memories tomorrow at the grand opening!"). I did not hand-label these, so the share of type (c) is not quantified; it is visibly non-trivial in the examples below. This is the part of the problem that retrieval engineering reaches least.

- **conv-49 8-11** (multi-hop, lost at recall, 19 gold turns). Q: "What health issue did Sam face that motivated him to change his lifestyle?" Gold answer: "Weight problem".
  - gold D16:3 (session 16): "Sam: Thanks, Evan! Appreciate your support. It's been a journey, and being chosen as a coach is a great step in my quest for better health."
  - ranks: vec ~44 (cos 0.52; 30th = 0.55); BM25 ~105; in no leg top-30.
  - out-ranked by: "Evan: That must have been a challenging experience, Sam. It's tough when we have to confront our own health c…"; "Evan: Hey Sam! Long time no talk! How're you doing? Life's been quite the rollercoaster lately. I had a healt…".
- **conv-44 5-3** (multi-hop, lost at recall, 8 gold turns). Q: "What kind of places have Andrew and his girlfriend checked out around the city?" Gold answer: "cafes, new places to eat, open space for hikes, pet shelter, wine tasting event, park".
  - gold D23:3 (session 23): "Andrew: Friday night's board game session was a nice break. This weekend, I'm planning to check out this cozy cafe and hang out there. [image: a photo of a group of people sitting at a table in a room]"
  - ranks: vec ~82 (cos 0.46; 30th = 0.50); BM25 ~74; in no leg top-30.
  - out-ranked by: "Audrey: Yeah, Andrew! The pups and I are loving it. Being out in nature and checking out new trails with the …"; "Andrew: Hey Audrey! What's up? Last weekend my girlfriend and I went fishing in one of the nearby lakes. It w…".
- **conv-48 7-19** (multi-hop, lost at recall, 2 gold turns). Q: "How many times has Jolene been to France?" Gold answer: "two times".
  - gold D1:8 (session 1): "Jolene: Staying connected is super important. Do you have something to remember her by? This pendant reminds me of my mother, she gave it to me in 2010 in Paris. [image: a photo of a heart shaped pendant with a bird on it]"
  - ranks: vec ~57 (cos 0.49; 30th = 0.51); BM25 ~261; in no leg top-30.
  - out-ranked by: "Jolene: Here is one more photo from Rio de Janeiro. We went on many excursions there. [image: a photo of a gr…"; "Deborah: Hey, that's Susie or Seraphim? How long has he been hanging out with you?".
- **conv-26 0-7** (multi-hop, lost at fusion, 2 gold turns). Q: "What is Caroline's relationship status?" Gold answer: "Single".
  - gold D3:13 (session 3): "Caroline: Yeah, I'm really lucky to have them. They've been there through everything, I've known these friends for 4 years, since I moved from my home country. Their love and help have been so important especially after that tough breakup.…"
  - ranks: vec 26 (cos 0.53); BM25 ~382; own RRF 0.35 vs 10th fused 0.48.
  - out-ranked by: "Melanie: Wow, Caroline. We've come so far, but there's more to do. Your drive to help is awesome! What's your…"; "Melanie: Thanks, Caroline! Family time matters to me. What's up with you lately?".
- **conv-43 4-50** (multi-hop, lost at fusion, 2 gold turns). Q: "Which book was John reading during his recovery from an ankle injury?" Gold answer: "The Alchemist".
  - gold D18:2 (session 18): "John: Hey Tim! That's awesome! Yeah, it was really cool. Oh man, it's been a tough week for me with this injury. But I'm staying positive. How about you? How's your week been? [image: a photo of a person with a bandage on their leg]"
  - ranks: vec ~54 (cos 0.40; 30th = 0.43); BM25 30; own RRF 0.34 vs 10th fused 0.50.
  - out-ranked by: "John: Last season, I had a major challenge when I hurt my ankle. It required some time off and physical thera…"; "John: I've been reading this inspiring book, it reminds me to keep dreaming.".

#### I. Open-domain inference: 96 lost turns (80 recall, 16 fusion)

MEASURED. Of 197 open-domain gold turns 96 are lost (49%), 83% of the lost ones at recall; only 55% of open-domain questions have all gold in context and, with all gold in context, the reader is right on 25% (rr run oracle). The questions are 'Would X ...', 'What might ...', 'How old is Jolene?': the evidence is a set of weakly related turns (studies, finals, an internship for age) and no single turn answers. Typical example below.

- **conv-44 5-53** (open-domain, lost at recall, 4 gold turns). Q: "What is a career that Andrew could potentially pursue with his love for animals and nature?" Gold answer: "Park ranger or a similar position working for the National Park Services.".
  - gold D3:1 (session 3): "Andrew: Hey Audrey! What's up? Missed chatting with ya! Check it out, my girl & I tried out that new cafe scene in the city last weekend! Super fun but kinda sad not being out in nature - that's when I feel like I'm really thriving. Oh man…"
  - ranks: vec ~138 (cos 0.47; 30th = 0.56); BM25 ~60; in no leg top-30.
  - out-ranked by: "Andrew: No, no pets right now. But I do love animals."; "Audrey: Hey Andrew! That hike sounds great. Nature is good for the soul, right? My week's been good - taking …".
- **conv-48 7-36** (open-domain, lost at recall, 10 gold turns). Q: "How old is Jolene?" Gold answer: "likely no more than 30; since she's in school".
  - gold D24:2 (session 24): "Jolene: Hey Deb, great to hear from you! I've been focusing on studying and my relationship with my partner. We're taking little trips to the beach, it's a great way to relax. How about you, anything new going on?"
  - ranks: vec ~332 (cos 0.45; 30th = 0.60); BM25 ~132; in no leg top-30.
  - out-ranked by: "Jolene: How old is Luna?"; "Jolene: How did you two meet?".
- **conv-26 0-69** (open-domain, lost at fusion, 3 gold turns). Q: "What personality traits might Melanie say Caroline has?" Gold answer: "Thoughtful, authentic, driven".
  - gold D7:4 (session 7): "Melanie: Wow, Caroline. We've come so far, but there's more to do. Your drive to help is awesome! What's your plan to pitch in?"
  - ranks: vec 17 (cos 0.65); BM25 ~59; own RRF 0.40 vs 10th fused 0.48.
  - out-ranked by: "Melanie: Yeah, Caroline! I'll start thinking about what we can do."; "Melanie: Wow, Caroline! What kinda jobs are you thinkin' of? Anything that stands out?".
- **conv-26 0-59** (open-domain, lost at fusion, 2 gold turns). Q: "Would Caroline be considered religious?" Gold answer: "Somewhat, but not extremely religious".
  - gold D14:19 (session 14): "Caroline: Thanks! It was made for a local church and shows time changing our lives. I made it to show my own journey as a transgender woman and how we should accept growth and change. [image: a photo of a large stained glass window in a ch…"
  - ranks: vec 12 (cos 0.47); BM25 ~305; own RRF 0.42 vs 10th fused 0.48.
  - out-ranked by: "Melanie: Wow, Caroline! That's huge! How did it feel to be around so much love and acceptance?"; "Melanie: Yes, Caroline! We can do it. Your courage is inspiring. I want to be couragous for my family- they m…".

#### T. Temporal / date-dependent evidence: 43 lost turns (14 recall, 29 fusion)

MEASURED. Only 43 of 374 temporal gold turns are lost (11%), the best category after single-hop. The loss is concentrated in questions that name an explicit date or month (18 of 57 such gold turns lost = 32%, vs 8% when the question has no explicit date): the gold turn is a relative statement ("Yesterday I met artists in Boston" said on 4 Oct) whose own text has no date, so a date in the question has nothing to match; 26 of the 43 lost turns contain a relative-time phrase. Only 187 questions had a temporal leg in the fix run (the leg fires only for absolute dates). Several lost turns are the second or third gold turn of a comparison question ("Which pet did Jolene adopt first": 21 of 43 belong to 2+ gold questions).

- **conv-50 9-57** (temporal, lost at recall, 1 gold turn). Q: "Which hobby did Dave pick up in October 2023?" Gold answer: "photography".
  - gold D27:2 (session 27): "Dave: Hey Calvin! That's cool that you've been networking with other artists. Nice! I've been getting into photography recently. I've seen some amazing places and taken some great shots. Would you like to see them?"
  - ranks: vec ~63 (cos 0.50; 30th = 0.52); BM25 ~457; in no leg top-30.
  - out-ranked by: "Dave: Hey Calvin, long time no talk! A lot has happened. I've taken up photography and it's been great - been…"; "Dave: I'm passionate about fixing up things. It's more than just a hobby - it gives me a sense of achievement…".
- **conv-43 4-20** (temporal, lost at recall, 4 gold turns). Q: "Which city was John in before traveling to Chicago?" Gold answer: "Seattle".
  - gold D5:2 (session 5): "John: Hi Tim! Nice to hear from you. Glad you could reconnect. As for me, lots of stuff happened since we last talked. Last week I had a crazy game - crazy intense! We won it by a tight score. Scoring that last basket and hearing the crowd…"
  - ranks: vec ~201 (cos 0.32; 30th = 0.42); BM25 ~84; in no leg top-30.
  - out-ranked by: "John: Thanks! Yeah, I've been there before and loved it! That place is amazing and the view from there is inc…"; "John: Thanks! It was amazing. Everywhere you go there's something new and exciting. Exploring the city and tr…".
- **conv-50 9-68** (temporal, lost at fusion, 1 gold turn). Q: "When did Dave find the car he repaired and started sharing in his blog?" Gold answer: "last week of October 2023".
  - gold D28:20 (session 28): "Dave: I found it last week, and it was in bad shape, but I saw the potential. I spent ages restoring it."
  - ranks: vec 8 (cos 0.60); BM25 ~134; own RRF 0.45 vs 10th fused 0.50.
  - out-ranked by: "Dave: Wow, Calvin, imagining how your music affects others must be incredible! Keep up the great work! By the…"; "Calvin: Yeah Dave, keep doing what you do! Your blog and car mods are inspiring and a great way to help peopl…".
- **conv-50 9-31** (temporal, lost at fusion, 2 gold turns). Q: "Where was Dave in the last two weeks of August 2023?" Gold answer: "San Francisco".
  - gold D14:1 (session 14): "Dave: Hey Cal, how's it going? Something cool happened since last we talked - I got to go to a car workshop in San Francisco! So cool to dive into the world of car restoration and see all the different techniques. People were really passio…"
  - ranks: vec ~31 (cos 0.52; 30th = 0.52); BM25 ~38; own RRF 0.33 vs 10th fused 0.33.
  - out-ranked by: "Dave: Hey Calvin! Long time no talk! Got some cool news to share - last night was a blast! My band and I were…"; "Dave: Hey Calvin! Haven't talked in a while! Last Friday I had a card-night with my friends, it was so much f…".

#### O. Overlap present but out-ranked / crowded out: 63 lost turns (13 recall, 50 fusion)

MEASURED. These turns do share a content word with the question, so they are the 'fair fight' cases: 50 of 63 are lost at fusion, in one leg only. For the fusion-lost ones the median own RRF score is 0.43 against 0.48 for the 10th fused item (a one-leg item at vector or BM25 rank ~8-25 cannot beat items present in both legs). They are one to a few slots from the cut; section 7 gives per-cause recovery under a larger pool.

- **conv-50 9-152** (single-hop, lost at recall, 1 gold turn). Q: "What new item did Dave buy recently?" Gold answer: "A vintage camera".
  - gold D30:5 (session 30): "Dave: That's amazing, Calvin! Music really does bring people together and foster creativity. Glad to hear you had such an inspiring conversation! Take a look at my new vintage camera that I bought this month, which takes awesome photos! [i…"
  - ranks: vec ~201 (cos 0.43; 30th = 0.50); BM25 ~100; in no leg top-30.
  - out-ranked by: "Calvin: Wow Dave, those headlights look great! What did you do to get them looking so good?"; "Calvin: No problem, Dave. Your enthusiasm and hard work show in everything you do. Keep coming up with new co…".
- **conv-44 5-15** (multi-hop, lost at recall, 2 gold turns). Q: "What is a shared frustration regarding dog ownership for Audrey and Andrew?" Gold answer: "Not being able to find pet friendly spots.".
  - gold D7:8 (session 7): "Andrew: I'm still on the hunt, but it's tough finding a pet-friendly spot in the city. Been checking out some places, but no luck so far. A bit discouraged but I'm determined to find the right place and dog."
  - ranks: vec ~139 (cos 0.52; 30th = 0.58); BM25 ~197; in no leg top-30.
  - out-ranked by: "Audrey: The hats don't bother them, they just put them on for fun and treats. And the dog park is great place…"; "Audrey: Hey Andrew! That hike sounds great. Nature is good for the soul, right? My week's been good - taking …".
- **conv-41 2-42** (multi-hop, lost at fusion, 2 gold turns). Q: "What area was hit by a flood?" Gold answer: "West County".
  - gold D14:21 (session 14): "John: Sure, Maria! Let's work together to make a real difference. Our neighborhood deserves it! I want to work on improving my old area, West County, too."
  - ranks: vec ~141 (cos 0.28; 30th = 0.34); BM25 10; own RRF 0.44 vs 10th fused 0.48.
  - out-ranked by: "John: I had a similar experience. Last week, there was a power cut in our area, and it made me realize the im…"; "John: Thanks! We explored the coast up in the Pacific Northwest and hit some cool national parks. The beauty …".
- **conv-50 9-20** (multi-hop, lost at fusion, 3 gold turns). Q: "Who inspired Dave's passion for car engineering?" Gold answer: "His Dad".
  - gold D12:2 (session 12): "Dave: Hey Calvin, I understand the stress of getting a car serviced. Fixing cars is like therapy for me. Growing up working on cars with my dad, refurbishing them gives me a sense of fulfillment."
  - ranks: vec 20 (cos 0.57); BM25 ~45; own RRF 0.38 vs 10th fused 0.46.
  - out-ranked by: "Dave: Thanks, Calvin! This is a dream come true for me, as I've always wanted to learn auto engineering and w…"; "Calvin: Dave, that car looks awesome! What got you into engineering cars? I'm totally into cars too and love …".

#### M. Morphology-only overlap: 27 lost turns (9 recall, 18 fusion)

MEASURED. The question shares only a stem with the turn (research / researching, camp / camping, endorsement / endorsements, dog / dogs, paint / painting). BM25 without stemming scores zero, so the turn is in the vector leg only and loses fusion to two-leg items. A simulation with Porter stemming (section 7) recovers 9 net questions; this is a small, cheap cause.

- **conv-47 6-8** (multi-hop, lost at recall, 3 gold turns). Q: "How many pets does James have?" Gold answer: "Three dogs.".
  - gold D1:14 (session 1): "James: Max and Daisy. Will be actually cool to build an app for dog walking and pet care. The goal is to connect pet owners with reliable dog walkers and provide helpful information on pet care."
  - ranks: vec ~31 (cos 0.51; 30th = 0.52); BM25 ~433; in no leg top-30.
  - out-ranked by: "James: My pets, computer games, travel and pizza are all that bring me happiness in life."; "James: Yeah, I have one. It was great! They loved it - so many trails to discover and amazing views. So fun! …".
- **conv-50 9-5** (multi-hop, lost at recall, 3 gold turns). Q: "What are Dave's dreams?" Gold answer: "open a car maintenance shop, work on classic cars, build a custom car from scratch".
  - gold D5:5 (session 5): "Dave: Thanks Calvin! Appreciate the support. I'm gonna keep learning more about auto engineering, maybe even build a custom car from scratch someday - that's the dream! For now, just gonna keep working on this project and assisting custome…"
  - ranks: vec ~81 (cos 0.50; 30th = 0.53); BM25 ~265; in no leg top-30.
  - out-ranked by: "Calvin: Go for it, Dave! Chasing your dreams is what life's about. It's awesome to see how far you've come. K…"; "Dave: Thanks, Calvin! Means a lot. I'm going to keep chasing my dreams and working hard. Conversations like t…".
- **conv-48 7-1** (multi-hop, lost at fusion, 3 gold turns). Q: "Which of Deborah`s family and friends have passed away?" Gold answer: "mother, father, her friend Karlie".
  - gold D6:4 (session 6): "Deborah: The roses and dahlias bring me peace. I lost a friend last week, so I've been spending time in the garden to find some comfort."
  - ranks: vec 12 (cos 0.57); BM25 ~429; own RRF 0.42 vs 10th fused 0.83.
  - out-ranked by: "Deborah: That's my old home. I go there now and then for my mom, who passed away. Sitting in that spot by the…"; "Deborah: Since speaking last, I reconnected with my mom's old friends. Their stories made me tear up and remi…".

#### P. Single-hop paraphrase gap: 12 lost turns

MEASURED. Rare: only 12 single-hop-style gold turns are lost this way ("What injury did Evan suffer from in August 2023?" -> "Twisted my knee last Friday": the question word 'injury' never appears, the embedding finds other injury turns first).

- **conv-49 8-110** (single-hop, lost at recall, 1 gold turn). Q: "What injury did Evan suffer from in August 2023?" Gold answer: "Twisted knee".
  - gold D9:2 (session 9): "Evan: Wow, Sam, great! Glad your new diet/exercise is going well. As for me, I've hit a sore spot lately. Twisted my knee last Friday and it's really painful, so it's been tough to stay consistent with my usual fitness routine. It's really…"
  - ranks: vec ~41 (cos 0.48; 30th = 0.50); BM25 ~425; in no leg top-30.
  - out-ranked by: "Evan: Hey Sam, what's up? It's been a few days since we talked. How have you been? Life's been tough lately -…"; "Evan: Mmm, it looks delicious! What did you put in it? I want to eat healthy, so what kind of recipes do you …".
- **conv-26 0-4** (multi-hop, lost at recall, 1 gold turn). Q: "What is Caroline's identity?" Gold answer: "Transgender woman".
  - gold D1:5 (session 1): "Caroline: The transgender stories were so inspiring! I was so happy and thankful for all the support. [image: a photo of a dog walking past a wall with a painting of a woman]"
  - ranks: vec ~189 (cos 0.42; 30th = 0.50); BM25 ~353; in no leg top-30.
  - out-ranked by: "Melanie: Wow, Caroline. We've come so far, but there's more to do. Your drive to help is awesome! What's your…"; "Melanie: Thanks, Caroline! It was Matt Patterson, he is so talented! His voice and songs were amazing. What's…".
- **conv-42 3-122** (single-hop, lost at fusion, 1 gold turn). Q: "What did Nate do for Joanna on 25 May, 2022?" Gold answer: "get her a stuffed animal".
  - gold D13:9 (session 13): "Nate: Yep, Joanna. It's great! Looky here, I got this new pup for you! [image: a photo of a stuffed animal laying on a bed]"
  - ranks: vec ~93 (cos 0.65; 30th = 0.69); BM25 ~69; own RRF 0.29 vs 10th fused 0.32.
  - out-ranked by: "Joanna: Awesome! Did you get to know the couple very well? What were they like?"; "Nate: Can't wait to see it, Joanna! I'm here to support you.".

### 2.4 Recall-lost turns: how far outside the legs are they?

MEASURED on the CPU replica (full ranking of all turns in the conversation; the replica's top-30 agrees with the logged one at 97.8%, so ranks 31-40 below can be bf16 noise). The 280 recall-lost turns:

| full-conversation cosine rank of the gold turn | turns | share |
|---|---|---|
| <=30 (replica/bf16 noise) | 2 | 1% |
| 31-60 | 66 | 24% |
| 61-100 | 52 | 19% |
| 101-200 | 71 | 25% |
| >200 | 89 | 32% |

Median vector rank 126, p25 62, p75 229 (conversations hold 300-700 turns). Cosine gap to the 30th logged hit: median 0.067; 111 of 280 within 0.05, 39 within 0.02. BM25 (full ranking): median rank 242; best of vector and BM25 within 60 for only 84 turns (30%), within 100 for 146 (52%). **Widening the legs (fetch 30 -> 60 or 100) would reach at most a quarter to a half of them and would then have to survive fusion; the rest need a different retrieval behaviour (decomposition, derived facts, entity / session routing) or are not retrievable by question similarity at all (causes N, I).**

Per cause, recall-lost turns (MEASURED, CPU replica): median full vector rank and share within rank 60:

| cause | recall-lost turns | median vector rank | within vector rank 60 | median BM25 rank |
|---|---|---|---|---|
| H | 95 | 110 | 24% | 285 |
| N | 64 | 102 | 36% | 196 |
| I | 80 | 180 | 10% | 248 |
| T | 14 | 132 | 21% | 264 |
| O | 13 | 60 | 54% | 77 |
| M | 9 | 81 | 22% | 313 |
| P | 5 | 147 | 40% | 381 |

### 2.5 Fusion-lost turns: margin and mechanism

MEASURED. 237 gold turns are in a leg's top-30 and outside the fused top-10. Leg membership: vector only 195, BM25 only 37, both 1, in the temporal leg 4.

| leg rank of the gold turn | 1-5 | 6-10 | 11-15 | 16-20 | 21-25 | 26-30 |
|---|---|---|---|---|---|---|
| vector (n=196) | 22 | 42 | 44 | 40 | 24 | 24 |
| BM25 (n=38) | 11 | 9 | 7 | 2 | 0 | 9 |

RRF margin (normalised fusion score of the 10th fused item minus the gold turn's own score): median 0.072, p25 0.037, p75 0.120; typical 10th score 0.48, typical gold turn 0.41. That is the gap between 'one leg at rank ~12' and 'both legs at ranks ~15'.

**Why single-leg items lose.** RRF with k=60 scores a one-leg item at rank r as 1/(60+r) (at most 0.0164), and a two-leg item at ranks (a, b) as 1/(60+a)+1/(60+b). An item at rank 28 in *both* legs (0.0227) already beats the vector leg's rank-1 item (0.0164) when that item is absent from BM25. The BM25 leg is an OR of every question token with no stop words and no stemming, so it matches common words ("did", "the", "what", the two speaker names) and puts 28th-ranked noise into two-leg status.

MEASURED noise: of all BM25 top-10 hits over the 1,540 questions, 5230 of 15400 (34%) share *no* content word with the question (they match on stop words / names only). In the fused top-10, 2152 of the 13997 non-gold slots (15%) are such BM25-only noise items sitting outside the vector top-10; 947 questions (61%) have at least one, 1.4 on average.

Examples where a high-ranked gold turn lost to lower-ranked two-leg items (fused top-10 listed as `(turn, vector rank, BM25 rank)`):

- **conv-43 4-50** Q: "Which book was John reading during his recovery from an ankle injury?". Gold D19:20 ("John: I recently finished rereading "The Alchemist" - it was really inspiring. It made me think again about following dreams and …"): vector rank 3, not in BM25 top-30. Fused top-10: (19:6,2,1), (4:10,8,6), (22:10,14,8), (18:13,26,2), (27:4,24,5), (5:12,9,23), (22:14,18,16), (17:2,11,26), (11:23,17,21), (11:25,1,-).
- **conv-48 7-84** Q: "What kind of engineering projects has Jolene worked on?". Gold D17:10 ("Jolene: Working on a cool project now - a prototype that could revolutionize aerial surveillance. Can't wait to see the results!"): vector rank 2, not in BM25 top-30. Fused top-10: (13:7,7,3), (13:9,13,1), (13:1,3,16), (22:6,17,4), (22:10,20,9), (22:8,14,15), (7:9,11,21), (14:13,29,6), (3:1,9,30), (7:1,21,27).
- **conv-49 8-25** Q: "Which year did Evan start taking care of his health seriously?". Gold D5:7 ("Evan: Yes, they bring me such joy. My healthy road has been a long one. I've been working on it for two years now, so there have …"): vector rank 2, not in BM25 top-30. Fused top-10: (12:2,7,10), (7:11,17,2), (14:1,15,6), (17:4,8,19), (17:2,9,21), (11:2,23,12), (7:2,10,27), (25:2,22,16), (17:5,27,15), (7:1,24,23).
- **conv-49 8-37** Q: "What kind of healthy meals did Sam start eating after getting a health scare?". Gold D8:1 ("Sam: Hey Evan, some big news: I'm on a diet and living healthier! Been tough, but I'm determined. [image: a photo of a bowl of sp…"): vector rank 3, not in BM25 top-30. Fused top-10: (8:6,2,2), (14:1,7,3), (11:1,14,10), (17:5,22,5), (10:2,12,14), (4:6,16,13), (7:3,18,15), (17:3,20,17), (7:2,11,30), (3:1,28,19).

## 3. Task 4: which leg is weak; does RRF lose items a leg ranked high?

Gold turns (fix run) by leg membership (MEASURED; "either" includes the temporal leg):

| category | gold turns | in vector top-30 | in BM25 top-30 | in either | in both | vector only | BM25 only | vector rank<=10 | BM25 rank<=10 | fused hit |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 2348 | 1766 (75%) | 1235 (53%) | 1903 (81%) | 1121 (48%) | 645 (27%) | 114 (5%) | 1405 (60%) | 956 (41%) | 1404 (60%) |
| single-hop | 895 | 769 (86%) | 641 (72%) | 840 (94%) | 586 (65%) | 183 (20%) | 55 (6%) | 669 (75%) | 533 (60%) | 704 (79%) |
| multi-hop | 882 | 586 (66%) | 287 (33%) | 626 (71%) | 247 (28%) | 339 (38%) | 40 (5%) | 386 (44%) | 171 (19%) | 355 (40%) |
| temporal | 374 | 325 (87%) | 257 (69%) | 341 (91%) | 247 (66%) | 78 (21%) | 10 (3%) | 287 (77%) | 217 (58%) | 284 (76%) |
| open-domain | 197 | 86 (44%) | 50 (25%) | 96 (49%) | 41 (21%) | 45 (23%) | 9 (5%) | 63 (32%) | 35 (18%) | 61 (31%) |

Question level, ALL gold turns of the question present:

| category | questions | all gold in vector top-30 | all in BM25 top-30 | all in either | all fused hits | all in context |
|---|---|---|---|---|---|---|
| single-hop | 841 | 721 (86%) | 603 (72%) | 790 (94%) | 658 (78%) | 810 (96%) |
| multi-hop | 282 | 124 (44%) | 36 (13%) | 141 (50%) | 62 (22%) | 108 (38%) |
| temporal | 320 | 277 (87%) | 220 (69%) | 290 (91%) | 249 (78%) | 284 (89%) |
| open-domain | 89 | 42 (47%) | 22 (25%) | 46 (52%) | 31 (35%) | 49 (55%) |

Reading. **The vector leg is the strong leg in every category** (75% of all gold turns in its top-30 vs 53% for BM25; 4.9% of gold turns are found by BM25 alone, 27.5% by the vector alone). BM25 is weakest exactly where the loss is: multi-hop (32% of gold turns in the BM25 top-30; only 36 of 282 questions have all gold there) and open-domain (25%). It is not a recall engine for LoCoMo but a precision signal for single-hop (72%) and temporal (69%). The numbers for multi-hop (50% of questions with all gold in some leg) are reproduced: 141 of 282.

**Does RRF lose items a leg ranked highly? Yes, mostly for the vector leg.** 186 gold turns have vector rank <=10 but are not fused hits (64 lost, 122 rescued only by a window); 66 have vector rank <=5, 26 rank <=3 (5 of those lost for good). For BM25, 32 gold turns with BM25 rank <=10 are not hits (15 with rank <=5). For the 66 vector-rank-<=5 turns that are not hits (22 lost + 44 window-rescued), the fused top-10 contains on average 7.6 items whose vector rank is *worse* (or absent), 6.4 of them present in both legs. The cause is agreement-by-noise, as quantified above.

Do re-weighting or other fusion settings fix it? SIMULATED with exact RRF on the logged legs (questions fully covered; baseline 1,251; "net" = gained minus lost questions; "wrong-set" = newly covered among the 158 wrong+incomplete):

| fusion variant | fully covered | net | gained / lost | wrong-set newly covered |
|---|---|---|---|---|
| baseline RRF k=60, equal weights | 1,251 | 0 | - | 1 |
| rrf_k 10 | 1,251 | 0 | +15 / -15 | 8 |
| rrf_k 30 | 1,251 | 0 | 0 / 0 | 1 |
| rrf_k 100 | 1,250 | -1 | 0 / -1 | 1 |
| vector weight 2 | 1,235 | -16 | +22 / -38 | 14 |
| BM25 weight 2 | 1,176 | -75 | +14 / -89 | 9 |
| vector leg only | 1,216 | -35 | +47 / -82 | 28 |
| BM25 leg only | 1,115 | -136 | +30 / -166 | 17 |
| reserve top-3 of each leg, fill with RRF | 1,247 | -4 | +11 / -15 | 8 |
| reserve top-5 of each leg | 1,221 | -30 | +32 / -62 | 20 |
| third BM25 leg on content words only (`read.core_terms_leg`-like) | 1,229 | -22 | +28 / -50 | 15 |
| same, stemmed | 1,249 | -2 | +44 / -46 | 27 |

Nothing here moves the total, because every re-ordering of ten slots trades one question's gold for another's. Only more slots help (section 7). The `rrf_k`, weights and reserved-slot knobs are not worth a run.

## 4. Task 2: multi-hop evidence structure and what decomposition could recover

MEASURED. 282 multi-hop questions with 882 gold turns. Number of gold turns per question: 1: 5, 2: 136, 3: 56, 4: 45, 5: 20, 6: 8, 7: 5, 8+: 7. Sessions spanned: 1: 13, 2: 164, 3: 56, 4: 28, 5+: 21. 269 of 282 questions need evidence from 2+ sessions.

| group | questions | all gold in some leg | all gold fused hits | all gold in context | >=1 gold in context | gold turns in context | answered correctly |
|---|---|---|---|---|---|---|---|
| all multi-hop | 282 | 141 (50%) | 62 (22%) | 108 (38%) | 247 (88%) | 61% | 177 (63%) |
| 1-2 gold turns | 141 | 96 (68%) | 54 (38%) | 75 (53%) | 120 (85%) | 69% | 95 (67%) |
| 3 gold turns | 56 | 23 (41%) | 7 (12%) | 20 (36%) | 50 (89%) | 65% | 36 (64%) |
| 4-5 gold turns | 65 | 20 (31%) | 1 (2%) | 13 (20%) | 59 (91%) | 57% | 41 (63%) |
| 6-99 gold turns | 20 | 2 (10%) | 0 (0%) | 0 (0%) | 18 (90%) | 46% | 5 (25%) |
| 1 session | 13 | 6 (46%) | 3 (23%) | 6 (46%) | 10 (77%) | 67% | 8 (62%) |
| 2 sessions | 164 | 101 (62%) | 54 (33%) | 87 (53%) | 140 (85%) | 69% | 114 (70%) |
| 3 sessions | 56 | 22 (39%) | 4 (7%) | 12 (21%) | 51 (91%) | 58% | 33 (59%) |
| 4+ sessions | 49 | 12 (24%) | 1 (2%) | 3 (6%) | 46 (94%) | 50% | 22 (45%) |

Reading. The ~50% of multi-hop questions with all gold in some leg is confirmed (141 of 282). Coverage falls steeply with the number of gold turns (2 turns: 68% in some leg; 6+ turns: 10%) and with sessions spanned (2 sessions: 62%; 4+: 24%), but 88% of multi-hop questions still get *at least one* gold turn into the context: the query finds one or two members of the evidence set and misses the rest. 75 questions have exactly one fused-hit gold turn while other gold turns are lost. Answer accuracy (63%) is higher than all-gold coverage (38%) because list questions are graded on partial answers.

By question form (regex classification of the question text):

| group | questions | all gold in some leg | all gold fused hits | all gold in context | >=1 gold in context | gold turns in context | answered correctly |
|---|---|---|---|---|---|---|---|
| list / aggregation (plural noun) | 127 | 61 (48%) | 25 (20%) | 43 (34%) | 111 (87%) | 59% | 75 (59%) |
| other wh- | 106 | 55 (52%) | 23 (22%) | 42 (40%) | 95 (90%) | 62% | 75 (71%) |
| count / duration | 26 | 16 (62%) | 8 (31%) | 11 (42%) | 22 (85%) | 64% | 11 (42%) |
| both / in common | 11 | 2 (18%) | 2 (18%) | 4 (36%) | 8 (73%) | 52% | 7 (64%) |
| yes / no | 8 | 4 (50%) | 3 (38%) | 5 (62%) | 7 (88%) | 68% | 6 (75%) |
| when | 4 | 3 (75%) | 1 (25%) | 3 (75%) | 4 (100%) | 86% | 3 (75%) |

**Is each gold turn retrievable by its own sub-question? (SIMULATED oracle, upper bound).** No human or LLM decomposition is available offline, so I built the best-case sub-question for every multi-hop gold turn whose text literally contains a gold-answer token (611 of 882 turns): "What did <person> say about <the answer words found in that turn>?". This is an *oracle*: it already knows the instance, which a real decomposer would have to guess or discover. Each sub-question is embedded and BM25-searched on its own and fused (RRF k=60) like the real query.

| state of the gold turn today | turns with an oracle sub-question | own sub-question: fused rank <=1 | <=3 | <=5 | <=10 |
|---|---|---|---|---|---|
| hit | 250 | 138 (55%) | 201 (80%) | 209 (84%) | 217 (87%) |
| win_fusion | 77 | 40 (52%) | 55 (71%) | 61 (79%) | 67 (87%) |
| win_recall | 52 | 23 (44%) | 32 (62%) | 37 (71%) | 42 (81%) |
| lost_fusion | 118 | 56 (47%) | 80 (68%) | 86 (73%) | 94 (80%) |
| lost_recall | 114 | 35 (31%) | 69 (61%) | 80 (70%) | 91 (80%) |

Of the 347 lost multi-hop gold turns, 232 can be targeted this way and 185 (80%) are in the top-10 of their own sub-question; 115 (33%) have no answer token in the turn, so even a perfect decomposer has no handle on them (cause N). Question level, union of today's context and the top-k of every oracle sub-question:

| oracle sub-questions retrieve | multi-hop questions with all gold in context (today 108) | gain | context cost |
|---|---|---|---|
| top-1 of each sub-question | 141 of 282 | +33 | 1 hits (each with its window) per targeted gold turn |
| top-3 of each sub-question | 161 of 282 | +53 | 3 hits (each with its window) per targeted gold turn |
| top-5 of each sub-question | 169 of 282 | +61 | 5 hits (each with its window) per targeted gold turn |
| top-10 of each sub-question | 180 of 282 | +72 | 10 hits (each with its window) per targeted gold turn |

ESTIMATE for a real decomposer: an LLM planner that does not know the answers recovers a fraction of this. Two reference points: (a) independence model with the single-hop per-gold-turn rates (fused-top-10 hit rate 78.7%, in-context 96.5%): expected multi-hop questions fully covered 142 (hits only) to 253 (with windows) of 282, but this overstates because list-instance turns are harder than single-hop turns (their lost rate is about 60%); (b) 25-50% of the oracle gain: **+18 to +36 multi-hop questions fully covered (108 -> ~126-144 of 282), i.e. roughly +15 to +30 correct answers at the 0.82 conditional reader accuracy**. Cost: 1 LLM call (the local 9B: ~1-3 s) plus 3-6 extra retrieval legs (~+100 ms).

## 5. Task 3: the neighbour window

MEASURED. In the fix run 427 gold turns (18.2%) reach the context only through a window (base: 373, 15.9%). By offset from the nearest hit that covers them (negative = before the hit): -2: 86, -1: 79, +1: 116, +2: 91, +3: 17, +4: 38. 149 of the 427 are covered by more than one hit. Offsets within +-2 account for 372 turns; the +3 / +4 positions added by the fix run account for 55.

Questions that have all their gold in context *only* because of windows: 251 of 1,540 (hits alone cover all gold for only 1,000 questions by simulation, windows raise it to 1,251).

**Where the lost gold sits relative to the hits.** Of the 517 lost turns, **388 (75%) are in a session in which no hit exists**; no window shape can help them. The other 129 lie in a session that has a hit, at offset (nearest hit): 3-4 turns before: 32; 5-8 before: 26; 9+ before: 28; 5-6 after: 9; 7-8 after: 12; 9+ after: 22. A bigger window can therefore reach at most 129 more gold turns, 79 of them within +-8.

SIMULATED window shapes on the fix run's logged fused top-10 (same session only, engine greedy order, budget 4096 estimated tokens; "full" = questions with all gold in context; baseline 1,251):

| window (before / after) | full | net | avg est. tokens | avg turns |
|---|---|---|---|---|
| 0 / 0 (hits only) | 1,000 | -251 | 458 | 10 |
| 1 / 2 | 1,186 | -65 | 1,408 | 33 |
| 2 / 2 (base run) | 1,222 | -29 | 1,670 | 39 |
| **2 / 4 (fix run)** | 1,251 | 0 | 2,189 | 53 |
| 2 / 6 | 1,259 | +8 | 2,637 | 65 |
| 3 / 5 | 1,260 | +9 | 2,643 | 64 |
| 2/4 + bridge: all turns between two hits of the same session (gap <= 8) | 1,251 | 0 | 2,192 | 53 |
| 2/4 + bridge (gap <= 20) | 1,255 | +4 | 2,261 | 55 |
| 2/4 + whole session when it holds 2+ hits | 1,270 | +19 | 3,182 | 79 |
| 2/4 + whole session when it holds 3+ hits | 1,257 | +6 | 2,507 | 61 |

**The window is saturated.** Beyond 2/4, 2 extra turns after the hit buy +8 questions for +450 tokens; session-level expansion buys +19 for +1,000 tokens. The remaining headroom is in finding more hits (more distinct sessions), not in widening the window around the same ones. With a larger pool the window must shrink by rank to stay in budget (section 7: tiered windows).

## 6. Task 5: how the reranker run loses gold (rr vs base)

MEASURED. Mechanism, verified on the logs:

1. Pool = the fused top-10 (`candidate_pool=1`); the fused list is identical to the base run in 1,540 / 1,540 questions. The reranker can only reorder and delete; **no question gained a gold turn** (full-gold questions: base 1219, rr 1183, lost 36, gained 0).
2. The engine replaces the fusion score by the *min-max normalised* reranker score (best hit = 1.0, worst = 0.0), then `assembly.relative_floor 0.3` drops every hit below 0.3: re-computing this rule reproduces the logged rr context in 1,540 / 1,540 questions (window +-2 around the surviving hits). Average hits surviving: 4.98 of 10 (distribution 1..9 hits: 102, 166, 207, 215, 203, 192, 177, 149, 129); the context shrinks from 39.3 to 19.7 turns and 1,571 to 806 tokens.
3. The reranker is a good gold detector: it keeps 1364 of 1400 gold hits (97.4%) but only 6310 of 14000 non-gold hits (45%). Drop rate by fused rank: 1: 10%, 2: 29%, 3: 40%, 4: 46%, 5: 55%, 6: 59%, 7: 62%, 8: 65%, 9: 66%, 10: 69%.
4. The damage is collateral. 70 gold turns that were in the base context are not in the rr context: only 18 (26%) were themselves hits that the floor dropped (their raw P(yes) median 0.14: the reranker really thought they were irrelevant, or they are label noise); 52 (74%) were window neighbours of a dropped hit (that hit's raw P(yes) median 0.07). Typical case: the hit is the question turn ("Which pet did you adopt?"), the answer is two turns later; scored alone the question turn is irrelevant, so hit and window go.
5. Effect on answers: 36 questions lost full gold coverage (none gained); on those, correct answers fell 23 -> 7. Overall rr is 1090 correct vs 1147 for base (base-only 107, rr-only 50); the 36 lost-coverage questions explain 16 of the net 57 loss. The remainder happens in questions whose gold coverage did not change or was already partial; the logs do not isolate why (candidates: a context half as long, dropped dated context the reader uses for temporal questions).
6. The assembly line of the rr run's primary gaps (11 questions) is exactly this: 18 gold hits that survived the rerank but fell under the floor.
7. Cost of the reranker as run: mean retrieval latency 388 ms vs 143 ms (fix) for 10 documents with the 4B 4-bit model; a 30-document pool would be ~3x the reranking part (~+0.7 s).

What this means for the retrieval-gap question: the reranker run *cannot* fix recall or fusion (nothing outside the fused 10 is ever scored). It becomes useful only with (a) `candidate_pool` 2-3, (b) a floor that does not act on min-max scores (`assembly.relative_floor: 0` or `rerank_blend` / `rerank_gate`), (c) `rerank_keep` set so the context size is controlled by count, not by a score range, and (d) ideally `rerank_context` so a question turn is scored together with its reply. See section 7 for the upper bound (ORACLE best-10-of-30: +81 questions at unchanged tokens).

## 7. What each lever recovers (SIMULATED on the logged lists)

Method: for every question the logged vector top-30, BM25 top-30 and temporal leg are re-fused (RRF k=60) exactly as the engine does (reproduces `fused` 1,540 / 1,540); the replay window and the 4,096-token budget are applied as in `Engine._read_routed` (reproduces `retrieved_turns` 1,540 / 1,540). BM25 and vector variants use the CPU replica (BM25: own implementation of Tantivy scoring, 97% top-30 agreement; vector: Qwen3-Embedding-0.6B fp32, 97.8% agreement). "Full" = questions with every gold turn in the context (baseline 1,251); "net" = questions gained minus lost; "wrong-set" = how many of the 158 wrong+incomplete questions have all gold in context (the baseline replica counts 1); tokens are an estimate (chars/4 + date prefix) and run ~6% above the logged mean (2,071). Churn matters: a variant with +40 / -30 has a net of +10 but reshuffles 70 questions.

### 7.1 More hits (pool size) and how the window is spent

| configuration | full | net (gained / lost) | wrong-set | avg tokens |
|---|---|---|---|---|
| baseline: pool 10, window 2/4 | 1,251 | - | 1 | 2,189 |
| pool 15, window 2/4 | 1,303 | +52 (+52 / -0) | 27 | 3,172 |
| pool 20, window 2/4, budget 4096 | 1,330 | +79 (+79 / -0) | 39 | 3,967 (budget binds) |
| pool 20, window 2/4, budget 8192 | 1,336 | +85 | 44 | 4,115 |
| pool 30, window 2/4, budget 4096 | 1,342 | +91 | 47 | 4,400 (budget binds hard) |
| pool 30, window 2/4, budget 8192 | 1,380 | +129 | 66 | 5,884 |
| pool 20, window 1/2 (flat) | 1,280 | +29 (+68 / -39) | 39 | 2,694 |
| pool 30, window 1/2 (flat) | 1,328 | +77 (+107 / -30) | 59 | 3,919 |
| pool 20, tiered: ranks 1-10 window 2/4, ranks 11-20 window 0/1 | 1,308 | +57 (+57 / -0) | 32 | 2,812 |
| pool 20, tiered: ranks 1-10 2/4, ranks 11-20 hit only | 1,300 | +49 (+49 / -0) | 28 | 2,513 |
| pool 30, tiered: ranks 1-10 2/4, ranks 11-30 window 0/1 | 1,341 | +90 (+90 / -0) | 50 | 3,447 |
| pool 30, tiered: ranks 1-10 2/4, ranks 11-30 hit only | 1,326 | +75 (+75 / -0) | 41 | 2,861 |
| pool 30, tiered: ranks 1-5 2/4, 6-10 1/2, 11-30 0/1 | 1,324 | +73 (+86 / -13) | 50 | 3,093 |
| ORACLE reranker: best 10 of the fused top-20, window 2/4 | 1,305 | +54 | 29 | 2,179 |
| ORACLE reranker: best 10 of the fused top-30 | 1,332 | +81 | 43 | 2,177 |
| ORACLE reranker: best 10 of the fused top-60 | 1,377 | +126 | 71 | 2,171 |
| ORACLE reranker: best 15 of the fused top-30 | 1,345 | +94 | 51 | 3,167 |

Reading. More slots are the only fusion-side lever with a large effect, and tokens are the price: a flat 2/4 window at pool 30 costs ~4,400 tokens and the 4,096 budget starts cutting evidence. Rank-tiered windows (full window for the first 10 hits, a one-turn tail for hits 11-30) recover most of the gain at ~3,450 tokens with no losses. A reranker (oracle bound) delivers +81 at the *current* token count, which is why a reranker over pool 30 is the efficient route if its real accuracy is close to the oracle; the rr run shows it keeps 97% of gold hits when it sees them, so 60-80% of the oracle (ESTIMATED: +50 to +65 questions) is plausible.

### 7.2 Lexical, vector-side and query-side options (pool 10, window 2/4 unless stated)

| option | exists as | full | net (gained / lost) | wrong-set | note |
|---|---|---|---|---|---|
| BM25 with Porter stemming | `read.lexical_analyzer: english` (also stop words) | 1,260 | +9 (+36 / -27) | 20 |  |
| BM25 stop words removed | `english` | 1,251 | 0 (+34 / -34) | 22 | pure churn |
| BM25 stop words + stemming (what `english` indexes) | `read.lexical_analyzer: english` | 1,264 | +13 (+55 / -42) | 34 |  |
| ... and speaker names dropped from the query | new code (or `core_terms_leg` as a replacement, below) | 1,274 | +23 (+56 / -33) | 34 | names are in every turn |
| ... plus pool 20 | pool = `read.candidate_pool: 2` | 1,353 | +102 (+108 / -6) | 63 | 3,860 tokens |
| BM25 indexes date words of the day said | `read.lexical_dates: true` | 1,261 | +10 (+15 / -5) | 13 | temporal 284 -> 290 |
| BM25 also indexes the dates of resolved relative phrases ("yesterday" -> that date) | new code (rule resolver exists) | 1,243 | -8 (+9 / -17) | 6 | date words match too many turns |
| third BM25 leg on content words only (names and stop words out) | `read.core_terms_leg: true` | 1,229 | -22 (+28 / -50) | 15 | harmful |
| same with stemming | `core_terms_leg` + `english` | 1,249 | -2 (+44 / -46) | 27 |  |
| embed "[date] + previous turn + turn" (and BM25 on the same text) | new code (write-time enrichment) | 1,241 / 1,244 | -10 / -7 | 21 / 29 | at window 2/2: +2 / +5 and 22% fewer tokens; at pool 20: +65 / +68 vs +85 plain |
| embed "previous turn + turn" | new code | 1,240 | -11 (+36 / -47) | 20 | pool 20: +72 |
| embed "previous + turn + next" | new code | 1,242 | -9 | 25 | pool 20: +49 |
| embed the turn text without the image caption | new code | 1,253 | +2 (+14 / -12) | 10 | captions neither help nor hurt |
| caption as its own vector unit (turn score = max of text-only and caption-only) | new code | 1,251 | 0 (+15 / -15) | 10 | pool 20: +81 vs +85 plain |
| statement-form probe (rule rewrite of the question) | `read.statement_probe: true` | 1,237 | -14 (+17 / -31) | 14 | fires on 1,162 questions |
| multi-part question split | `read.multi_intent_split: true` | 1,251 | 0 | 1 | splits only 5 of 1,540 questions |
| pseudo-relevance feedback probe | `read.prf_expansion: true` | 1,175 | -76 (+22 / -98) | 14 | harmful |
| statement probe + PRF | both | 1,215 | -36 | 24 |  |
| neighbours of the top-2 hits as extra legs | `read.cluster_expand: true` | 1,185 | -66 (+27 / -93) | 20 | harmful (30 neighbours: -73) |
| at most N hits per session from a 4x pool | `read.session_cap: 1 / 2 / 3` | 1,186 / 1,248 / 1,250 | -65 / -3 / -1 | 19 / 7 / 1 | no gain |
| per-session top-1 / top-2 retrieval, +-1 window | new code | 857 / 1,155 | -394 / -96 | - | 27.7 / 55.4 hits per question |

Reading. **Every existing rule-based query-expansion option (statement probe, PRF, cluster expansion, session cap, multi-intent split) is neutral or harmful on LoCoMo in this simulation**, and write-time context enrichment of the embedding (previous turn, date, caption handling) is neutral: it frees tokens (the enriched turns embed closer to their neighbours, so the top-10 concentrates in fewer places) but does not add gold. The only lexical options with a positive net are `lexical_analyzer: english` (+13), `lexical_dates` (+10), and names dropped (+23 combined), all small and churn-heavy. Caveat: the probes were simulated from the engine's own functions (`statement_form`, `feedback_terms`, `split_intents`) with probes embedded as queries (instruction prefix) and fused as vector + BM25 legs; a different embedding of the probe text could change the sign for `statement_probe` but is unlikely to rescue PRF or cluster expansion.

### 7.3 Per-cause recovery (gold turns, MEASURED-SIM)

| cause | lost turns | A: pool 20 tiered | B: pool 30 tiered | C: english-like BM25, names dropped | D: ORACLE rerank 10 of 30 | E: B + C |
|---|---|---|---|---|---|---|
| H Category-to-instance gap (list / aggregation q | 164 | 26 | 46 | 26 | 39 | 64 |
| N Answer not literally in the gold turn (paraphr | 112 | 21 | 37 | 23 | 30 | 55 |
| I Open-domain inference (evidence is an implicat | 96 | 7 | 14 | 14 | 8 | 25 |
| T Temporal / date-dependent evidence | 43 | 18 | 22 | 17 | 23 | 29 |
| O Lexical overlap exists but the turn is outrank | 63 | 26 | 35 | 27 | 35 | 48 |
| M Morphology-only overlap (BM25 without stemming | 27 | 9 | 13 | 15 | 12 | 18 |
| P Single-hop paraphrase gap | 12 | 3 | 6 | 6 | 5 | 7 |
| **all** | 517 | 110 | 173 | 128 | 152 | 246 |
| gold turns newly *lost* (were in context) | - | 0 | 0 | 86 | 4 | 30 |

Reading. Pool 30 with a thin tail recovers a third of the lost turns (173 of 517) with zero losses; the English BM25 variant recovers 128 but loses 86 other turns (a reshuffle, not a gain); the oracle reranker recovers 152 at constant tokens. Combining pool 30 with the lexical change (E) recovers 246 of 517 (48%): 76% of cause O, 67% of T and M, 49% of N, 39% of H and 26% of I (30 turns churn out). **About half of the lost turns is the ceiling of these retrieval-only fixes; the rest needs decomposition or derived memory (H) or cannot be reached by similarity to the question (N, I).**

## 8. Solution options per cause

Legend. **Exists** = a config key in `src/memspine/config/schema.py` / `docs/USAGE.md` (verified); **new code** = needs implementation. "Recoverable" is gold turns / fully-covered questions; MEASURED-SIM refers to section 7, ESTIMATE is judgement. Reader conversion: multiply covered questions by ~0.8 (single-hop 0.89, multi-hop 0.82, temporal 0.72, open-domain 0.25). Cost is per question at read time unless stated; risk includes reader dilution from longer contexts, which these logs cannot measure.

### H. Category-to-instance gap (164 turns, 26% of lost; 40 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| H1. Larger pool with a tiered window: `candidate_pool: 3` + window by rank (full window for hits 1-10, 0/1 beyond) | `read.candidate_pool` exists (max 10; pool 30 = 3). Window-by-rank = new code (small; the 2-before / 4-after window exists only in `memspine-fixes`) | MEASURED-SIM: 46 of 164 turns; +90 questions fully covered overall, 50 of the 158 wrong ones; multi-hop 108 -> 161 (pool 30 tiered) | tokens 2.2k -> 3.4k; ~0 ms latency | reader dilution; budget 4096 must be raised for flat windows |
| H2. Rerank a pool of 30, keep 10-15: `candidate_pool: 3`, `rerank: qwen3`, `rerank_keep: 10`, floor off | all exist (`read.rerank_keep`, `assembly.relative_floor: 0`; also `rerank_blend`, `rerank_gate`, `rerank_context`, `rerank_date_prefix`) | MEASURED-SIM oracle: 39 of 164 turns, +81 questions at constant tokens; ESTIMATE real: 60-80% of that | +0.7 s (30 docs, 4B 4-bit, as measured 245 ms for 10); GPU memory for reranker | the floor/min-max bug must be removed first (section 6); pointwise scoring cannot see that a reply answers the previous turn (use `rerank_context: 1-2`) |
| H3. Instance-aware decomposition: LLM planner writes evidence-seeking sub-queries, each retrieves its own top-k, fused as RRF legs | exists: `read.planner: llm`, `planner_version: v2/v3` (needs the `plan` LLM role), `read.compose_rewrites`, `aggregate_in_replay` + `aggregate_top_k` | MEASURED-SIM oracle upper bound: multi-hop 108 -> 180 (+72) with answer-aware sub-queries; ESTIMATE real +18 to +36 multi-hop questions | 1 LLM call per question (~1-3 s local 9B) + 3-6 retrieval legs (~+100 ms) | a decomposer that does not know the instances must guess them ("hobbies" -> painting, hiking, ...); wrong sub-queries add noise |
| H4. Derived list memory: mine atomic facts with topic tags and keep person x topic list cards (hobbies: painting, kayaking, ...) read through `read.cards: header` | exists: `consolidation.mine_facts`, `mine_multiview`, `list_cards`, `read.cards`, `read.list_cards_only_aggregate`; needs the `extract` LLM role and a sleep run | not measurable from these logs. ESTIMATE: directly targets the cause (instances collected regardless of wording); upper range is cause H + part of N; depends on miner recall | LLM per session at ingest (one-off, ~19-35 sessions x 10 conversations); no read-time cost beyond the card block | miner errors and wrong dates (documented: cards cost temporal accuracy in the 2026-10-05 smoke test); cards consume read budget; evaluating needs an LLM run |
| H5. Two-stage retrieval: round 1 finds some instances, round 2 queries "also: <category> <instance>" for each | `read.second_round` exists but seeds from names and dates only; instance-seeded round 2 = new code | not simulated; the oracle in section 4 is its upper bound (80% of targetable lost turns in the top-10 of an instance query) | 1 extra search (~50 ms) or 1 LLM call | drift to the instances already found |

### N. Answer not literally in the gold turn (112 turns, 22%; 35 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| N1. Audit the gold labels for this class (evaluation hygiene, not engine work) | new tooling; 112 turns, start with the 35 wrong questions | changes the denominator, not accuracy: any turn that does not support the answer should not count as lost | one day of reading | none; reduces false "retrieval failure" counts |
| N2. Pool / reranker as H1-H2 | exist | MEASURED-SIM: pool 30 tiered recovers 37 of 112, oracle rerank 30 | as H1 / H2 | as H1 / H2 |
| N3. HyDE-style answer hypothesis or answer-free rewrite as an extra leg | `read.compose_rewrites` is the nearest (answer-free rewrites from the `query_rewrite` role); true HyDE = new code | ESTIMATE +5 to +15 questions; implicit answers ("lost a friend") are what a hypothesis can bridge | 1 LLM call per question | hallucinated hypotheses retrieve confident noise |
| N4. Reply-aware retrieval: index a turn together with the turn it answers | new code; MEASURED-SIM of the embedding variants: net -11 to +2 | no gain at pool 10 (section 7.2) | re-index | top-10 concentrates in fewer places |
| N5. Bigger / different embedder (Qwen3-Embedding-4B is in the local HF cache) | config only (`embedding.model`, 2560-d); untested here (the 4B snapshot would not load offline) | unknown; recall-lost turns have a median vector rank of 127, so a better embedder is unlikely to pull most of them into the top-30 | ~7x embedding time, more GPU | earlier arms: bge-small and Jina lost 2.5 points to the 0.6B model |

### I. Open-domain inference (96 turns, 19%; 28 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| I1. Pool / reranker as H1-H2 | exist | MEASURED-SIM: 14 of 96 (pool 30 tiered), 8 (oracle rerank) | as H1 / H2 | reader accuracy is 25% even with all gold, so conversion is ~0.25 |
| I2. Evidence-seeking sub-queries for yes/no and "would X" questions ("Is X religious?" -> "church", "faith") | exists: `planner: llm`, `planner_version: v2` (needs `plan` role) | ESTIMATE +3 to +8 questions covered, +1 to +2 correct | 1 LLM call | little downstream conversion |
| I3. Profile / reflective memory so dispositions are stored as statements | exists: `consolidation.reflect_profile`, reflective memory; needs LLM at sleep | not measurable here | LLM at sleep | profile errors read as facts |
| I4. Deprioritise: fix the reader prompt for inference questions instead | prompt change (outside this document) | the 148 reader failures include 21 open-domain ones with all gold present | none | - |

### T. Temporal / date-dependent evidence (43 turns, 8%; 29 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| T1. Let the temporal leg fire on relative and partial dates: `read.temporal_relative`, `temporal_infer_year` | exist (off in these runs); `temporal_leg_mentions` is already on in the fix run | ESTIMATE 5-12 turns; the temporal leg fires on only 187 of the 1,540 questions today | ~0 | the leg lists the first N turns of a day unscored (all session turns share one timestamp), so it adds noise |
| T2. Date words in BM25: `read.lexical_dates: true` | exists (rebuilds the lexical index) | MEASURED-SIM: +10 questions net (+15 / -5); temporal 284 -> 290 | index +10% | small |
| T3. Index the resolved date of "yesterday" / "last week" at write time | new code (the rule resolver exists; `annotate` is used only at read time on hits) | MEASURED-SIM of the crude version (resolved date words appended to the BM25 text): -8 net; needs a smarter way than date words (e.g. a date field filtered by the temporal leg, `consolidation.mine_event_dates` + `temporal_leg_event_dates`) | sleep stage or write step | noise from date words shared by many turns |
| T4. Question-aware reranking with dates: `rerank_date_prefix: true` | exists (only with a reranker) | ESTIMATE 3-8 | negligible | - |

### O. Overlap present but out-ranked (63 turns, 12%; 11 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| O1. Pool / reranker as H1-H2 | exist | MEASURED-SIM: pool 30 tiered 35 of 63, oracle rerank 35 | as H1 / H2 | as H1 / H2 |
| O2. BM25 noise control: `lexical_analyzer: english`, drop speaker names from the query | `english` exists; name stripping = new code | MEASURED-SIM +13 / +23 questions, 27 of 63 turns, but 86 gold turns churn out | index rebuild | churn: net small, effects per conversation uneven |
| O3. Weights, rrf_k, reserved slots | exist (`leg_weights`, `rrf_k`, `short_query_lexical_weight`) | MEASURED-SIM: 0 to negative (section 3) | - | do not run |

### M / P. Morphology-only overlap (27 turns) and single-hop paraphrase (12 turns), 5% and 2%

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| M1. `read.lexical_analyzer: english` | exists | MEASURED-SIM: recovers 15 of 27 M turns and 6 of 12 P turns (config C); overall +13 net | index rebuild | churn |
| M2. Larger pool | exists | 13 of 27 and 6 of 12 (pool 30 tiered) | as H1 | as H1 |

### Cross-cutting: fusion and context assembly

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| X1. Fix the reranker floor before any reranker run: `assembly.relative_floor: 0` (or `rerank_blend` / `rerank_gate`, or `rerank_keep`) | exists | would keep the 18 floor-dropped gold hits and most of the 52 lost neighbours in the rr run (ESTIMATE: the 36 lost full-coverage questions) | - | - |
| X2. Spend hits on distinct locations: 1.87 of the 10 hits per question have a window that overlaps a higher-ranked hit | new code: skip a candidate whose turn already lies inside the 2/4 window of a chosen hit and take the next fused item (hit selection aware of the window) | MEASURED-SIM: 1,266 fully covered (+15; +28 / -13), 13 of the wrong set, 2,631 tokens (+440) | ~0 ms | needs fused items 11+ (pool > 10); modest gain |
| X3. Window 2/4 as a config key on this branch | `read.replay_window_before/after` exist in `memspine-fixes` (`schema.py:507`), not in the `feat/local-qwen-stack` ReadConfig, where `replay_window` is only a call argument (docs/PIPELINE_TREE.md 2.10) | already measured in the fix run: +29 questions over +-2 | - | - |
| X4. Budget: raise `--budget` from 4096 once the pool grows, and move truncation from the tail (latest turns) to the lowest-ranked windows | `--budget` is a harness argument; ranked truncation = new code | pool 30 flat 2/4 loses 38 of its 129 possible questions to the 4,096 budget (1,342 vs 1,380) | - | the reader context window |
| X5. Gold-label audit of the multi-hop list questions | tooling | affects metrics only | - | - |

## 9. Comparison with the earlier simulation-based analysis (`evals/analysis/RETRIEVAL_GAPS.md`)

| claim in RETRIEVAL_GAPS.md | status | what the real logs show |
|---|---|---|
| Gold: 57.8% itself a fused hit, 22.2% at fused ranks 11-60, 16.8% rescued by the window, ~20% in no leg | confirmed | base run: 59.7% hits, 21.2% in a leg but outside the fused top-10 (498 turns), 19.1% in no leg (449), window-rescued 15.9% (373); fix run: 59.8% / 21.3% / 19.0% / 18.2% |
| The top-10 cut-off (class B) is the largest class: 36% of missing gold turns; 117 of 142 vector-only, 23 BM25-only | confirmed (and larger) | fusion-lost is 237 of 517 lost turns in the fix run (46%; base 267 of 574): 195 vector-only, 37 BM25-only, 1 both. At question level `primary_gap` shows recall (94) above fusion (64) only because recall is checked first when a question has both kinds |
| Class A "near-window": window too small, 3-5 turns from a hit (14% of missing) | resolved by the fix | the 2/4 window rescued 55 more turns; what is left near a hit is 32 turns 3-4 before and 9 turns 5-6 after (section 5) |
| Image captions are a class (C1: 23% of wrong questions) | corrected | captions are not a cause: lift 1.05, answer-only-in-caption is 15 of 517 lost turns (3%); embedding without captions or with captions as their own vector unit changes nothing (+2, 0) |
| 69% of missing gold turns share no content word with the question; speaker names carry no signal | confirmed | 68% (stemmed) / 78% (exact) of the 517 lost turns; speaker identity is not a driver (lift 1.04) |
| Window 2 before / 4 after beats symmetric 3 at equal cost | confirmed and realised | fix run uses 2/4: +29 fully covered questions over the base +-2 in the replay; window is now saturated (2/6: +8, session-level: +19) |
| Pool 20 (`candidate_pool: 2`) recovers ~58 questions; pool 25 ~72 | confirmed in order of magnitude | pool 20: +79 questions fully covered (39 of the wrong set) but the 4,096 budget already binds (3,967 tokens); budget 8192: +85. A tiered window gets +57 at 2.8k tokens |
| Contextual enrichment ("[session date] + previous turn" in embedded and BM25 text) recovers ~18 net questions | NOT confirmed | re-embedded on CPU: -7 to -10 at window 2/4 (-10 vector only), +2 to +5 at the old window 2/2 with 22% fewer tokens; at pool 20 it is no better than the plain index (+65-68 vs +85). Prev+cur and prev+cur+next variants behave the same |
| Lexical weight 2, core_terms_leg, cohesion, sentence, entity_expand are net harmful | confirmed where retested | BM25 weight 2: -75; core_terms leg: -22 (-2 stemmed); vector weight 2: -16; rrf_k 10/30/100: 0, 0, -1. Newly tested existing options, all neutral or harmful: statement_probe -14, prf_expansion -76, cluster_expand -66, session_cap 1/2/3: -65/-3/-1 |
| Reranker with pool 10 can only delete; min-max + `relative_floor 0.3` drops 53% of hits (Jina) | confirmed for Qwen3-Reranker-4B | context is a subset of the base context in 1,540 / 1,540; 4.98 of 10 hits survive; gold hits kept 97.4%, non-gold 45%; 74% of lost gold (52 of 70 turns) were window neighbours; 36 questions lost full gold, 0 gained |
| Part of the reranker's cost happens with all gold still present (-29 of -47) | consistent | rr: the 36 lost-coverage questions explain -16 of the net -57 correct; the other -41 happen where coverage is unchanged or already partial |
| Multi-hop: only ~35% of questions have complete evidence in context | confirmed | 38% in the fix run (108 / 282); 50% have all gold in some leg (141 / 282) |
| C4 date-dependent turns: relative phrases need the session date to match | partly confirmed, smaller | 43 temporal lost turns; 26 contain a relative phrase; lost-rate is 32% when the question names an explicit date vs 8% otherwise; `lexical_dates` +10 net, crude resolved-date words -8 |
| Reader conditional accuracy 0.83 / 0.78 / 0.30 / 0.91 | consistent | rr run: 82% multi-hop, 72% temporal, 25% open-domain, 89% single-hop with all gold in context |

**New findings the simulation could not see:** (1) 34% of BM25 top-10 hits have no content-word overlap with the question and fill 15% of the non-gold fused slots; (2) vector rank <=5 turns are routinely lost to two-leg items at vector rank 15-30 (66 turns); (3) every rule-based query-expansion option in the engine is neutral or harmful here; (4) the lost turns are concentrated in questions with many gold turns (50% in questions with 4+), so counting questions understates the problem; (5) 75% of lost gold is in sessions that have no hit.

## Appendix A. All 517 lost gold turns (fix run)

Columns: `st` R = lost at recall (in no leg top-30), F = lost at fusion. `vec` / `bm25` = rank in the logged top-30, or `~n` = rank over the whole conversation from the CPU replica. `margin`: F rows = own normalised RRF score / score of the 10th fused item; R rows = cosine below the 30th logged vector hit. `cause` letters as in section 2. Sorted by cause, then question. Text truncated; pipes replaced by `/`.

| # | q | cat | cause | st | vec | bm25 | margin | question | gold turn | out-ranked by (top fused non-gold) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 26 0-7 | mh | H | R | ~86 | ~343 | -0.04 | What is Caroline's relationship status? | D2:14: Caroline: I'm thrilled to make a family for kids who need one. It'll be tough as a single parent, b… | Melanie: Wow, Caroline. We've come so far, but there's more to do. Yo… |
| 2 | 26 0-11 | mh | H | R | ~245 | ~34 | -0.11 | Where did Caroline move from 4 years ago? | D4:3: Caroline: Thanks, Melanie! This necklace is super special to me - a gift from my grandma in my home… | Caroline: Woohoo Melanie! I passed the adoption agency interviews las… |
| 3 | 26 0-15 | mh | H | R | ~136 | ~290 | -0.07 | What activities does Melanie partake in? | D1:12: Melanie: You'd be a great counselor! Your empathy and understanding will really help the people you… | Melanie: Wow, that sounds awesome! Your friends and community really … |
| 4 | 26 0-15 | mh | H | R | ~36 | ~228 | -0.00 | What activities does Melanie partake in? | D1:18: Melanie: Yep, Caroline. Taking care of ourselves is vital. I'm off to go swimming with the kids. Ta… | Melanie: Wow, that sounds awesome! Your friends and community really … |
| 5 | 26 0-15 | mh | H | R | ~61 | ~94 | -0.03 | What activities does Melanie partake in? | D9:1: Melanie: Hey Caroline, hope all's good! I had a quiet weekend after we went camping with my fam two… | Melanie: Wow, that sounds awesome! Your friends and community really … |
| 6 | 26 0-19 | mh | H | F | 7 | ~329 | 0.46/0.47 | What do Melanie's kids like? | D6:6: Melanie: They were stoked for the dinosaur exhibit! They love learning about animals and the bones … | Caroline: Thanks, Melanie! Your kind words really mean a lot. I'll do… |
| 7 | 26 0-24 | mh | H | F | 17 | ~363 | 0.40/0.47 | What does Melanie do to destress? | D5:4: Melanie: Wow, Caroline! That's great! I just signed up for a pottery class yesterday. It's like the… | Melanie: Thanks, Caroline! These are for running. Been running longer… |
| 8 | 26 0-32 | mh | H | R | ~43 | ~314 | -0.03 | What LGBTQ+ events has Caroline participated in? | D8:17: Caroline: Wow, nice pic! You both looked amazing. One special memory for me was this pride parade I… | Melanie: Wow, Caroline, sounds like the parade was an awesome experie… |
| 9 | 26 0-38 | mh | H | R | ~40 | ~133 | -0.01 | What activities has Melanie done with her family? | D1:18: Melanie: Yep, Caroline. Taking care of ourselves is vital. I'm off to go swimming with the kids. Ta… | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 10 | 26 0-38 | mh | H | F | 29 | ~178 | 0.34/0.48 | What activities has Melanie done with her family? | D6:4: Melanie: That's awesome, Caroline! Congrats on following your dreams. Yesterday I took the kids to … | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 11 | 26 0-38 | mh | H | R | ~122 | ~152 | -0.07 | What activities has Melanie done with her family? | D8:6: Melanie: We love painting together lately, especially nature-inspired ones. Here's our latest work … | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 12 | 26 0-38 | mh | H | R | ~31 | ~44 | -0.00 | What activities has Melanie done with her family? | D9:1: Melanie: Hey Caroline, hope all's good! I had a quiet weekend after we went camping with my fam two… | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 13 | 26 0-61 | mh | H | F | 14 | ~52 | 0.41/0.48 | What musical artists/bands has Melanie seen? | D11:3: Melanie: Thanks, Caroline! It was Matt Patterson, he is so talented! His voice and songs were amazi… | Melanie: Nope, never been to something like that. What was it about? … |
| 14 | 26 0-66 | mh | H | F | 23 | ~106 | 0.37/0.47 | What does Melanie do with her family on hikes? | D16:4: Melanie: Thanks, Caroline! It's awesome seeing the kids get excited learning something new about na… | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 15 | 30 1-3 | mh | H | F | 23 | ~294 | 0.37/0.48 | What do Jon and Gina both have in common? | D1:2: Jon: Hey Gina! Good to see you too. Lost my job as a banker yesterday, so I'm gonna take a shot at … | Gina: Yeah Jon! Let's make a difference and show 'em what we got. We … |
| 16 | 30 1-3 | mh | H | R | ~147 | ~99 | -0.09 | What do Jon and Gina both have in common? | D1:3: Gina: Sorry about your job Jon, but starting your own business sounds awesome! Unfortunately, I als… | Gina: Yeah Jon! Let's make a difference and show 'em what we got. We … |
| 17 | 30 1-3 | mh | H | R | ~202 | ~242 | -0.14 | What do Jon and Gina both have in common? | D1:4: Jon: Sorry to hear that! I'm starting a dance studio 'cause I'm passionate about dancing and it'd b… | Gina: Yeah Jon! Let's make a difference and show 'em what we got. We … |
| 18 | 30 1-3 | mh | H | F | ~162 | 5 | 0.47/0.48 | What do Jon and Gina both have in common? | D2:1: Gina: Hey Jon! Long time no see! Things have been hectic lately. I just launched an ad campaign for… | Gina: Yeah Jon! Let's make a difference and show 'em what we got. We … |
| 19 | 30 1-17 | mh | H | F | 21 | ~126 | 0.38/0.72 | Why did Gina decide to start her own clothing store? | D6:8: Gina: Thanks! I'm passionate about fashion trends and finding unique pieces. Plus, I wanted to blen… | Gina: Hey Jon! Long time no see! Things have been hectic lately. I ju… |
| 20 | 30 1-23 | mh | H | F | 27 | ~59 | 0.35/0.49 | How did Gina promote her clothes store? | D13:4: Gina: Proud of you for starting your own business! It takes strength to stay hopeful. What are you … | Gina: Hey Jon! Long time no see! Things have been hectic lately. I ju… |
| 21 | 41 2-3 | mh | H | F | 26 | ~610 | 0.35/0.49 | What type of volunteering have John and Maria both done? | D2:1: Maria: Hey John, been a few days since we chatted. In the meantime, I donated my old car to a homel… | Maria: Hey John, great news - I'm now friends with one of my fellow v… |
| 22 | 41 2-3 | mh | H | R | ~206 | ~497 | -0.09 | What type of volunteering have John and Maria both done? | D3:5: John: We held some events and got to meet some people. We went to a homeless shelter to give out fo… | Maria: Hey John, great news - I'm now friends with one of my fellow v… |
| 23 | 41 2-6 | mh | H | F | 11 | ~328 | 0.43/0.48 | Where has Maria made friends? | D14:10: Maria: Just yesterday I joined a nearby church. I wanted to feel closer to a community and my faith… | John: Yeah Maria! Friends like you make a big difference. Talk to you… |
| 24 | 41 2-6 | mh | H | R | ~79 | ~498 | -0.03 | Where has Maria made friends? | D19:1: Maria: Hey John, been good since we talked? I got some great news to share - I joined a gym last we… | John: Yeah Maria! Friends like you make a big difference. Talk to you… |
| 25 | 41 2-6 | mh | H | R | ~405 | ~456 | -0.14 | Where has Maria made friends? | D2:1: Maria: Hey John, been a few days since we chatted. In the meantime, I donated my old car to a homel… | John: Yeah Maria! Friends like you make a big difference. Talk to you… |
| 26 | 41 2-11 | mh | H | R | ~57 | ~557 | -0.03 | What people has Maria met and helped while volunteering? | D21:19: Maria: One of the shelter residents, Laura, wrote us a letter expressing their gratitude. The impac… | Maria: I started volunteering here about a year ago after witnessing … |
| 27 | 41 2-11 | mh | H | R | ~118 | ~74 | -0.08 | What people has Maria met and helped while volunteering? | D6:5: Maria: Yeah, at the event, I had a conversation with someone named David. Hearing his story of hard… | Maria: I started volunteering here about a year ago after witnessing … |
| 28 | 41 2-15 | mh | H | F | 7 | ~457 | 0.46/0.46 | What writing classes has Maria taken? | D9:1: Maria: Hey John, long time no see! I've been taking a poetry class lately to help me put my feeling… | Maria: Looks fun! What other classes have you done? |
| 29 | 41 2-25 | mh | H | F | 5 | ~260 | 0.47/0.48 | What European countries has Maria been to? | D8:15: Maria: Thanks, John! I got the idea from that trip to England a few years ago - I was mesmerized by… | Maria: Things have been tough for her lately. She had to leave and fi… |
| 30 | 41 2-28 | mh | H | R | ~50 | ~556 | -0.02 | What causes does John feel passionate about supporting? | D12:5: John: Recently, education reform and infrastructure development. Good access to quality education a… | John: I'm really passionate about making sure veterans are supported … |
| 31 | 41 2-28 | mh | H | R | ~146 | ~285 | -0.06 | What causes does John feel passionate about supporting? | D9:8: John: Thanks, Maria! Improving education and infrastructure is particularly interesting to me. It's… | John: I'm really passionate about making sure veterans are supported … |
| 32 | 41 2-32 | mh | H | F | 5 | ~180 | 0.47/0.48 | What outdoor activities has John done with his colleagues? | D18:2: John: Hey Maria, thanks for your kind words. It's still tough, but I'm finding some comfort in the … | John: Yeah, everyone got a chance to swing. It's always fun coming up… |
| 33 | 41 2-35 | mh | H | F | 9 | ~517 | 0.44/0.48 | What states has Maria vacationed at? | D18:3: Maria: Glad you're finding comfort, John. That mountaineering trip sounds amazing. Did you reach th… | Maria: Sounds like parenting has been a wonderful experience for you … |
| 34 | 41 2-40 | mh | H | R | ~120 | ~536 | -0.05 | What are the names of John's children? | D22:7: John: Thanks, Maria! That picture was from a trip we took last year for my daughter Sara's birthday… | Maria: Hey John, long time no see! Sorry I didn't get back to you soo… |
| 35 | 41 2-47 | mh | H | R | ~33 | ~561 | -0.01 | What exercises has John done? | D10:1: John: Hey Maria, I'm so excited to tell you I started a weekend yoga class with a colleague - it's … | Maria: Wow, John! You really overcame those challenges. Have you done… |
| 36 | 41 2-47 | mh | H | F | 8 | ~214 | 0.45/0.46 | What exercises has John done? | D1:4: John: Woah, Maria, that sounds cool! I'm doing kickboxing and it's giving me so much energy. | Maria: Wow, John! You really overcame those challenges. Have you done… |
| 37 | 41 2-57 | mh | H | F | 12 | ~64 | 0.42/0.48 | What causes has John done events for? | D6:12: John: Seeing the effect unemployment has on our neighbors made me decide to act. I wanted to help o… | Maria: Wow, John! You really overcame those challenges. Have you done… |
| 38 | 42 3-1 | mh | H | R | ~470 | ~203 | -0.22 | What kind of interests do Joanna and Nate share? | D10:9: Joanna: Not much is new other than the screenplay. Been working on some projects and testing out da… | Nate: Hey Joanna, I'm a big fan of them and thought it would be a fun… |
| 39 | 42 3-1 | mh | H | R | ~53 | ~440 | -0.02 | What kind of interests do Joanna and Nate share? | D4:9: Nate: Thanks, Joanna! It means a lot that you enjoy the desserts I bake. | Nate: Hey Joanna, I'm a big fan of them and thought it would be a fun… |
| 40 | 42 3-5 | mh | H | F | 23 | ~409 | 0.37/0.47 | What are Joanna's hobbies? | D2:25: Joanna: Writing and hanging with friends! That way I can express myself through stories, or just ha… | Nate: Playing video games and watching movies are my main hobbies. |
| 41 | 42 3-11 | mh | H | F | 13 | ~250 | 0.42/0.47 | What is Joanna allergic to? | D4:4: Joanna: That looks delicious! Unfortunately, I can't have dairy, so no ice cream for me. Do you hap… | Joanna: I used to have a dog back in Michigan with that name, but the… |
| 42 | 42 3-27 | mh | H | F | 6 | ~207 | 0.46/0.48 | What places has Joanna submitted her work to? | D2:7: Joanna: Woohoo, Nate! I'm feeling a rollercoaster of emotions - relief, excitement, some anxiety - … | Joanna: Yep! I actually just submitted a few more last week! Hoping t… |
| 43 | 42 3-30 | mh | H | F | 17 | ~213 | 0.40/0.48 | What kind of writings does Joanna do? | D17:14: Joanna: I will! I actually started on a book recently since my movie did well! [image: a photo of a… | Joanna: Yeah, it does! My brother wrote it - he used to make me these… |
| 44 | 42 3-30 | mh | H | R | ~37 | ~194 | -0.01 | What kind of writings does Joanna do? | D2:3: Joanna: Woo! I finally finished my first full screenplay and printed it last Friday. I've been work… | Joanna: Yeah, it does! My brother wrote it - he used to make me these… |
| 45 | 42 3-34 | mh | H | R | ~229 | ~519 | -0.12 | What book recommendations has Joanna given to Nate? | D3:17: Joanna: I just watched "Little Women" and it was amazing! It's a great story about sisterhood, love… | Joanna: Thanks, Nate! I'm so glad you're excited. I've never really t… |
| 46 | 42 3-42 | mh | H | F | 21 | ~263 | 0.38/0.47 | What movies have both Joanna and Nate seen? | D10:1: Joanna: Hey Nate, how's it going? I took your reccomendation and watched "The Lord of the Rings" Tr… | Joanna: Cool, Nate! So we both have similar interests. What type of m… |
| 47 | 42 3-42 | mh | H | R | ~242 | ~242 | -0.14 | What movies have both Joanna and Nate seen? | D22:8: Nate: I watched "Little Women" recently, and it was great! The acting was awesome and the story was… | Joanna: Cool, Nate! So we both have similar interests. What type of m… |
| 48 | 42 3-50 | mh | H | F | 10 | ~327 | 0.44/0.47 | What is something Nate gave to Joanna that brings her a lot of joy? | D13:9: Nate: Yep, Joanna. It's great! Looky here, I got this new pup for you! [image: a photo of a stuffed… | Nate: Great actually! These little guys sure bring joy to my life! Wa… |
| 49 | 42 3-56 | mh | H | R | ~205 | ~340 | -0.08 | What animal do both Nate and Joanna like? | D5:6: Nate: I'm drawn to turtles. They're unique and their slow pace is a nice change from the rush of li… | Joanna: That's great, Nate! Appreciate the small joys like that cute … |
| 50 | 42 3-58 | mh | H | R | ~568 | ~517 | -0.29 | What things has Nate reccomended to Joanna? | D10:11: Joanna: Thanks! It's dairy-free vanilla with strawberry filling and coconut cream frosting. I gotta… | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 51 | 42 3-58 | mh | H | R | ~356 | ~615 | -0.20 | What things has Nate reccomended to Joanna? | D9:12: Nate: Yeah, for sure! This trilogy is one of my faves. The world building, battles, and storytellin… | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 52 | 42 3-58 | mh | H | R | ~371 | ~51 | -0.20 | What things has Nate reccomended to Joanna? | D9:14: Nate: I love this series. It has adventures, magic, and great characters - it's a must-read! [image… | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 53 | 42 3-63 | mh | H | R | ~69 | ~447 | -0.05 | What video games does Nate play? | D27:1: Nate: Hey Joanna! Hope you’re doing alright. Crazy thing happened - I was in the final of a big Val… | Nate: Playing video games and watching movies are my main hobbies. |
| 54 | 42 3-70 | mh | H | F | 9 | ~490 | 0.44/0.47 | What activities does Nate do with his turtles? | D25:23: Nate: Yeah, it's adorable! Watching them enjoy their favorite snacks is so fun. I also like holding… | Nate: It really is, I'm not sure I'll ever understand why watching my… |
| 55 | 42 3-74 | mh | H | R | ~416 | ~515 | -0.24 | What recommendations has Nate received from Joanna? | D15:14: Nate: I really should start a cork board of my own shouldn't I. That seems like a really valuable t… | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 56 | 42 3-74 | mh | H | R | ~271 | ~526 | -0.17 | What recommendations has Nate received from Joanna? | D19:15: Nate: Good idea! How about this series? [image: a photo of a stack of books sitting on top of a woo… | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 57 | 42 3-74 | mh | H | R | ~534 | ~123 | -0.28 | What recommendations has Nate received from Joanna? | D23:26: Joanna: Sure! For one, you should get a couch that can sit multiple people so that you can lay down… | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 58 | 42 3-74 | mh | H | R | ~521 | ~428 | -0.27 | What recommendations has Nate received from Joanna? | D3:17: Joanna: I just watched "Little Women" and it was amazing! It's a great story about sisterhood, love… | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 59 | 42 3-81 | mh | H | F | 22 | ~401 | 0.37/0.49 | What recipes has Joanna made? | D19:8: Joanna: Thanks, Nate! It feels great knowing that people like my writing. I celebrated by making th… | Joanna: Awesome! I'll bring some of my recipes so we can both share d… |
| 60 | 42 3-81 | mh | H | R | ~83 | ~41 | -0.07 | What recipes has Joanna made? | D21:11: Joanna: Hey Nate, my favorite dairy-free treat is this amazing chocolate raspberry tart. It has an … | Joanna: Awesome! I'll bring some of my recipes so we can both share d… |
| 61 | 42 3-81 | mh | H | F | 16 | ~430 | 0.40/0.49 | What recipes has Joanna made? | D22:1: Joanna: Hey Nate, hi! Yesterday, I tried my newest dairy-free recipe and it was a winner with my fa… | Joanna: Awesome! I'll bring some of my recipes so we can both share d… |
| 62 | 42 3-82 | mh | H | R | ~35 | ~72 | -0.01 | What recipes has Nate made? | D3:4: Nate: Thanks, Joanna. Not much has changed for me, but I just discovered that I can make coconut mi… | Joanna: Hey Nate! I have been revising and perfecting the recipe I ma… |
| 63 | 42 3-83 | mh | H | R | ~127 | ~129 | -0.05 | What are the skills that Nate has helped others learn? | D18:8: Nate: Thanks, Joanna! Your words mean a lot. Since we last spoke, I started teaching people how to … | Nate: Thanks! It feels good to use my skills to make a difference. |
| 64 | 43 4-2 | mh | H | F | 5 | ~298 | 0.47/0.48 | What items does John collect? | D27:20: John: Cool! Glad you're enjoying that book! Do you have any favorite fantasy movies as well? These … | John: Nature does have a way of humbling us and showing us our place … |
| 65 | 43 4-4 | mh | H | R | ~50 | ~123 | -0.06 | What books has Tim read? | D1:14: Tim: It's been going well! Last week I talked to my friend who is a fan of Harry Potter and we're f… | Tim: Yes, they are still my favorites - I love how they take me to ot… |
| 66 | 43 4-7 | mh | H | F | 14 | ~202 | 0.41/0.47 | Which geographical locations has Tim been to? | D14:16: Tim: I snapped that pic on my trip to the Smoky Mountains last year. It was incredible seeing it in… | John: Hey Tim, sorry I missed you. Been a crazy few days. Took a trip… |
| 67 | 43 4-7 | mh | H | R | ~61 | ~525 | -0.03 | Which geographical locations has Tim been to? | D3:2: Tim: Congrats on your achievement! I'm so proud of you. Last week, I had a nice chat with a Harry P… | John: Hey Tim, sorry I missed you. Been a crazy few days. Took a trip… |
| 68 | 43 4-11 | mh | H | R | ~96 | ~385 | -0.06 | What sports does John like besides basketball? | D3:25: John: I had an awesome summer with my friends, surfing and riding the waves. The feeling was unreal… | Tim: Wow, John! Moments like that make us love sports, huh? I still t… |
| 69 | 43 4-14 | mh | H | R | ~36 | ~481 | -0.01 | What kind of writing does Tim do? | D2:1: Tim: Last night I joined a fantasy literature forum and had a great talk about my fave books. It wa… | Tim: I can just imagine the thrill of being in that kind of atmospher… |
| 70 | 43 4-25 | mh | H | F | 28 | ~155 | 0.35/0.48 | What similar sports collectible do Tim and John own? | D7:7: John: You're a real bookworm! It would be awesome to go to a book conference with you. Check out th… | John: Thanks, Tim! It's awesome to see how sports can unite people. B… |
| 71 | 43 4-25 | mh | H | R | ~261 | ~235 | -0.11 | What similar sports collectible do Tim and John own? | D7:9: John: Thanks! They signed it to show our friendship and appreciation. It's a great reminder of our … | John: Thanks, Tim! It's awesome to see how sports can unite people. B… |
| 72 | 43 4-35 | mh | H | F | 5 | ~333 | 0.47/0.48 | What outdoor activities does John enjoy? | D12:6: John: Yeah! Such a great day! It was so beautiful having everyone celebrating with us. I'd never fe… | John: Nature does have a way of humbling us and showing us our place … |
| 73 | 43 4-38 | mh | H | R | ~49 | ~155 | -0.03 | which country has Tim visited most frequently in his travels? | D13:1: Tim: Hey John! It's been ages since we last talked. Guess what? Last week I went to a Harry Potter … | Tim: Thanks! I'll keep you in the loop about my travels. Is there any… |
| 74 | 43 4-38 | mh | H | F | 25 | ~440 | 0.36/0.46 | which country has Tim visited most frequently in his travels? | D18:1: Tim: Hey John! Hope you're doing good. Guess what? I went to a castle during my trip to the UK last… | Tim: Thanks! I'll keep you in the loop about my travels. Is there any… |
| 75 | 43 4-41 | mh | H | F | 11 | ~287 | 0.43/0.49 | What kind of fiction stories does Tim write? | D15:3: Tim: That castle looks amazing! I hope I get to visit it someday. My writing is going well: I'm in … | Tim: I can just imagine the thrill of being in that kind of atmospher… |
| 76 | 43 4-41 | mh | H | R | ~85 | ~526 | -0.07 | What kind of fiction stories does Tim write? | D16:1: Tim: Hey John, long time no see! Hope you've been doing well. Since we last chat, some stuff's happ… | Tim: I can just imagine the thrill of being in that kind of atmospher… |
| 77 | 43 4-52 | mh | H | R | ~159 | ~322 | -0.12 | What does John do to supplement his basketball training? | D20:2: John: Hi Tim! Congrats on your success! Keep it up, you're doing great! I'm also trying out yoga to… | John: Thanks! Strength training is important for basketball because i… |
| 78 | 43 4-61 | mh | H | R | ~37 | ~363 | -0.02 | What books has John read? | D17:9: John: Yep, I just finished this amazing fantasy series. It was a wild ride with so many twists. The… | John: That's cool! I've heard it's such an inspiring book. Have you r… |
| 79 | 43 4-65 | mh | H | R | ~45 | ~412 | -0.02 | What fantasy movies does Tim like? | D8:16: Tim: Yeah, "Harry Potter and the Philosopher's Stone" is special to me. It was the first movie from… | Tim: That's amazing! Same here. There's something special about being… |
| 80 | 44 5-2 | mh | H | F | 17 | ~301 | 0.40/0.48 | What kind of indoor activities has Andrew pursued with his girlfriend? | D13:1: Andrew: Hey Audrey! How are you? My GF and I just had a great experience volunteering at a pet shel… | Andrew: Hey Audrey, how's it going? Since we last talked, a few new t… |
| 81 | 44 5-2 | mh | H | F | 22 | ~107 | 0.37/0.48 | What kind of indoor activities has Andrew pursued with his girlfriend? | D19:15: Andrew: Yeah! They really do bring so much into our lives - it's amazing to watch them interact. He… | Andrew: Hey Audrey, how's it going? Since we last talked, a few new t… |
| 82 | 44 5-3 | mh | H | F | 27 | ~499 | 0.35/0.48 | What kind of places have Andrew and his girlfriend checked out around the … | D13:1: Andrew: Hey Audrey! How are you? My GF and I just had a great experience volunteering at a pet shel… | Audrey: Yeah, Andrew! The pups and I are loving it. Being out in natu… |
| 83 | 44 5-3 | mh | H | R | ~69 | ~140 | -0.03 | What kind of places have Andrew and his girlfriend checked out around the … | D4:2: Andrew: Hey Audrey! Glad to hear from you. That hummingbird was awesome! Nature's the best. Remembe… | Audrey: Yeah, Andrew! The pups and I are loving it. Being out in natu… |
| 84 | 44 5-12 | mh | H | R | ~65 | ~78 | -0.05 | What outdoor activities has Andrew done other than hiking in nature? | D17:1: Andrew: Hey Audrey! What's up? Last weekend my girlfriend and I went fishing in one of the nearby l… | Andrew: Yeah, rock climbing was awesome - I felt so accomplished reac… |
| 85 | 44 5-15 | mh | H | R | ~62 | ~146 | -0.02 | What is a shared frustration regarding dog ownership for Audrey and Andrew? | D10:5: Audrey: Seeing them do well is super rewarding! They give me so much love and happiness. I get how … | Audrey: The hats don't bother them, they just put them on for fun and… |
| 86 | 44 5-18 | mh | H | F | 23 | ~262 | 0.37/0.49 | Where did Audrey get Pixie from? | D11:4: Audrey: Thanks! I got lucky finding a breeder nearby that has the dogs I wanted. Yeah places that a… | Audrey: Pixie's fitting in great! It took her a few days to get used … |
| 87 | 44 5-23 | mh | H | R | ~33 | ~179 | -0.00 | What are some problems that Andrew faces before he adopted Toby? | D2:12: Andrew: Thanks! Fingers crossed for the apartment and that furry friend. | Andrew: Thanks! He's doing great in his new home. Still getting used … |
| 88 | 44 5-23 | mh | H | R | ~209 | ~156 | -0.14 | What are some problems that Andrew faces before he adopted Toby? | D5:3: Andrew: Meeting all these adorable pups has been awesome! For those considering getting a pup, the … | Andrew: Thanks! He's doing great in his new home. Still getting used … |
| 89 | 44 5-23 | mh | H | F | 30 | ~446 | 0.34/0.50 | What are some problems that Andrew faces before he adopted Toby? | D5:5: Andrew: Yeah! Finding a pet-friendly place to live has been tough too. I'm contacting landlords and… | Andrew: Thanks! He's doing great in his new home. Still getting used … |
| 90 | 44 5-23 | mh | H | R | ~221 | ~453 | -0.14 | What are some problems that Andrew faces before he adopted Toby? | D5:7: Andrew: I'm looking for a place near a park or woods, so I can stay close to nature and give the do… | Andrew: Thanks! He's doing great in his new home. Still getting used … |
| 91 | 44 5-35 | mh | H | F | 12 | ~292 | 0.42/0.48 | What are the names of Andrew's dogs? | D12:1: Andrew: Hey! So much has changed since last time we talked - meet Toby, my puppy. He's a bundle of … | Andrew: Thanks, I think that's what I need to hear. I'll take good ca… |
| 92 | 44 5-35 | mh | H | R | ~72 | ~610 | -0.05 | What are the names of Andrew's dogs? | D28:8: Andrew: It took us a while to decide, but we ended up going with 'Scout' for our pup - it seemed pe… | Andrew: Thanks, I think that's what I need to hear. I'll take good ca… |
| 93 | 44 5-39 | mh | H | R | ~227 | ~352 | -0.16 | What did Audrey get wtih having so many dogs? | D2:15: Audrey: You'll love them! They're great for cuddles and companionship. | Audrey: Yep! They bring me so much joy. Can't get bored at all. [imag… |
| 94 | 44 5-49 | mh | H | F | 17 | ~408 | 0.40/0.48 | What does Audrey view her pets as? | D23:18: Audrey: I think i've said this so many times, but I really can't imagine my life without them - my … | Audrey: Yeah, my dogs make me really happy. I love them so much and I… |
| 95 | 44 5-50 | mh | H | R | ~141 | ~71 | -0.07 | What is a skill that Audrey learned to take care of her dogs? | D17:4: Audrey: Yeah they really do enjoy the mountain life. Regular grooming is essential to keep them loo… | Audrey: That's cool! I love checking out new parks with my four pups.… |
| 96 | 44 5-51 | mh | H | R | ~108 | ~279 | -0.05 | What items has Audrey bought or made for her dogs? | D18:10: Audrey: I'm so happy seeing them have a great time. Last week I even got some new beds for them, ju… | Audrey: Thanks! I got lucky finding a breeder nearby that has the dog… |
| 97 | 44 5-54 | mh | H | F | 28 | ~96 | 0.35/0.48 | What activity do Audrey's dogs like to do in the dog park? | D10:7: Audrey: They're all mutts. Two of them are Jack Russell mixes and the other two are Chihuahua mixes… | Audrey: The dog park is like paradise for them! They love socializing… |
| 98 | 47 6-2 | mh | H | R | ~161 | ~583 | -0.08 | Which places or events have John and James planned to meet at? | D1:36: James: Yeah, VR gaming is awesome! Let`s do it next Saturday! | James: That's great, John! Looks like he's having a good time in the … |
| 99 | 47 6-2 | mh | H | F | 30 | ~370 | 0.34/0.47 | Which places or events have John and James planned to meet at? | D21:15: James: Well, how about we go to McGee's pub then? I heard they serve a great stout there! | James: That's great, John! Looks like he's having a good time in the … |
| 100 | 47 6-5 | mh | H | R | ~110 | ~314 | -0.07 | What are John and James' favorite games? | D4:16: James: I've been playing my favourite game called Apex Legends with my team and it's intense! Check… | John: Cool, James! What kind of games are you excited to play on it? |
| 101 | 47 6-8 | mh | H | R | ~50 | ~429 | -0.04 | How many pets does James have? | D1:12: James: It would be cool! For example, we could write some kind of application for dogs. By the way,… | James: My pets, computer games, travel and pizza are all that bring m… |
| 102 | 47 6-8 | mh | H | F | 14 | ~169 | 0.41/0.48 | How many pets does James have? | D5:1: James: Hey John! Long time no chat - I adopted a pup from a shelter in Stamford last week and my da… | James: My pets, computer games, travel and pizza are all that bring m… |
| 103 | 47 6-27 | mh | H | F | 16 | ~107 | 0.40/0.48 | What kind of classes has James joined? | D13:6: James: It can be rough getting started, but I'm sure you'll do great. Don't be afraid to seek help … | James: Thanks! Music is a big part of my life - nothing to do with ca… |
| 104 | 47 6-38 | mh | H | R | ~72 | ~35 | -0.03 | What happened to John's job situation in 2022? | D18:7: John: Your support means a lot. Lately, I've been thinking about what truly makes me happy, and I'm… | James: Wow, John! Congrats on getting your dream job. I'm super stoke… |
| 105 | 47 6-40 | mh | H | R | ~47 | ~311 | -0.04 | What kind of tricks do James's pets know? | D14:17: James: Yeah, he loves it! We usually hit the beach or lake, and he loves playing in the water. He's… | James: My dogs are like that too - they even make dark days better. D… |
| 106 | 47 6-40 | mh | H | F | 28 | ~184 | 0.35/0.48 | What kind of tricks do James's pets know? | D14:23: James: Max is a real go-getter! He's awesome at catching frisbees in mid-air - never misses! [image… | James: My dogs are like that too - they even make dark days better. D… |
| 107 | 47 6-40 | mh | H | F | 26 | ~162 | 0.35/0.48 | What kind of tricks do James's pets know? | D17:16: James: Indeed, I remember this moment. We loved skateboards back then, sometimes we even left class… | James: My dogs are like that too - they even make dark days better. D… |
| 108 | 47 6-56 | mh | H | F | 5 | ~157 | 0.47/0.71 | Which of James's family members have visited him in the last year? | D28:19: James: I'll reach out if I need help. Thanks for the resources, really appreciate it. By the way, m… | James: In fact, I haven't visited many countries. Besides Italy, I wa… |
| 109 | 48 7-13 | mh | H | F | 12 | ~377 | 0.42/0.49 | What places give Deborah peace? | D19:17: Deborah: I love going to this park near my house - it has a nice forest trail and a beach. It's a p… | Deborah: It's amazing how it can give you peace and calm in times lik… |
| 110 | 48 7-14 | mh | H | F | 12 | ~133 | 0.42/0.49 | What were Deborah's mother's hobbies? | D12:3: Deborah: My mom was interested in art. She believed art could give out strong emotions and uniquely… | Jolene: That's awesome, Deborah! What were some of your favorite memo… |
| 111 | 48 7-14 | mh | H | F | 7 | ~490 | 0.46/0.49 | What were Deborah's mother's hobbies? | D29:7: Deborah: Thanks, Jolene! It was really special. My mom had a big passion for cooking. She would mak… | Jolene: That's awesome, Deborah! What were some of your favorite memo… |
| 112 | 48 7-20 | mh | H | F | 15 | ~551 | 0.41/0.49 | Which games have Jolene and her partner played together? | D20:1: Jolene: Long time no talk! We were given a new game for the console last week, it is Battlefield 1.… | Jolene: Well... we planned to play the console with my partner. |
| 113 | 48 7-82 | mh | H | R | ~41 | ~344 | -0.02 | Which locations does Deborah practice her yoga at? | D2:13: Deborah: That's my old home. I go there now and then for my mom, who passed away. Sitting in that s… | Deborah: Wow, cool that yoga has been helping you out! Do they also d… |
| 114 | 48 7-84 | mh | H | F | 26 | ~530 | 0.35/0.73 | What kind of engineering projects has Jolene worked on? | D17:12: Jolene: My aim is to devise a more productive and affordable aerial surveillance system. It'll help… | Jolene: The best part so far has been being able to apply what I lear… |
| 115 | 48 7-84 | mh | H | F | 22 | ~531 | 0.37/0.73 | What kind of engineering projects has Jolene worked on? | D4:5: Jolene: Thanks so much! I had to plan and research a lot to design and build a sustainable water pu… | Jolene: The best part so far has been being able to apply what I lear… |
| 116 | 48 7-86 | mh | H | R | ~185 | ~75 | -0.08 | What gifts has Deborah received? | D23:22: Deborah: This was written to me by a friend who, unfortunately, will never be able to support me. I… | Deborah: Anna also has a pendant that she wears in memory of her moth… |
| 117 | 48 7-88 | mh | H | R | ~66 | ~220 | -0.07 | What activities does Deborah pursue besides practicing and teaching yoga? | D12:1: Deborah: Hey Jolene! Great to see you! Had a blast biking nearby with my neighbor last week - was s… | Deborah: Well done! As for me, I've been focusing on teaching yoga an… |
| 118 | 48 7-88 | mh | H | R | ~279 | ~523 | -0.17 | What activities does Deborah pursue besides practicing and teaching yoga? | D15:1: Deborah: Hey Jolene! I started a running group with Anna - it's awesome connecting with people who … | Deborah: Well done! As for me, I've been focusing on teaching yoga an… |
| 119 | 48 7-88 | mh | H | R | ~90 | ~187 | -0.08 | What activities does Deborah pursue besides practicing and teaching yoga? | D28:11: Deborah: That beach is super special to me. It's where I got married and discovered my love for sur… | Deborah: Well done! As for me, I've been focusing on teaching yoga an… |
| 120 | 48 7-88 | mh | H | R | ~126 | ~218 | -0.11 | What activities does Deborah pursue besides practicing and teaching yoga? | D29:1: Deborah: Hey Jolene, I'm so excited to tell you! Yesterday, me and my neighbor ran a free gardening… | Deborah: Well done! As for me, I've been focusing on teaching yoga an… |
| 121 | 49 8-7 | mh | H | F | 20 | ~361 | 0.38/0.49 | What new hobbies did Sam consider trying? | D10:8: Sam: Wow Evan, that's an awesome painting! Good on you for finding a way to de-stress. I could real… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 122 | 49 8-7 | mh | H | F | 3 | ~261 | 0.48/0.49 | What new hobbies did Sam consider trying? | D20:6: Sam: I used to love hiking, but it's been a while since I had the chance to do it. | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 123 | 49 8-7 | mh | H | R | ~97 | ~127 | -0.05 | What new hobbies did Sam consider trying? | D7:2: Sam: Hey Evan, sorry to hear about what happened. I can imagine how hard it must have been for you.… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 124 | 49 8-7 | mh | H | R | ~240 | ~370 | -0.10 | What new hobbies did Sam consider trying? | D7:4: Sam: The cooking class has been great, I've learned awesome recipes. Last night I made this yummy g… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 125 | 49 8-7 | mh | H | R | ~154 | ~118 | -0.07 | What new hobbies did Sam consider trying? | D7:6: Sam: Thanks, Evan! I marinated it with a few different ingredients and grilled it with some veggies… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 126 | 49 8-14 | mh | H | R | ~44 | ~285 | -0.02 | What is Evan's favorite food? | D5:5: Evan: Ginger snaps are my weakness for sure! Dealing with health issues has been tough, but it's ma… | Evan: Wow, Sam, that's great to hear! Feeling more energized after me… |
| 127 | 49 8-21 | mh | H | F | 18 | ~72 | 0.39/0.48 | How does Evan describe the woman and his feelings for her that he met in C… | D23:3: Evan: Thanks, Sam! It was an awesome moment, and I feel really lucky to have found someone who gets… | Sam: Congratulations, Evan! Is that the woman from Canada? |
| 128 | 49 8-32 | mh | H | F | 6 | ~62 | 0.46/0.47 | What recurring frustration does Evan experience? | D21:20: Evan: Thanks, Sam! Glad I could motivate you. If you ever want to give it a go, I'm happy to help g… | Sam: Hey Evan, that does sound like a tough situation. I'm doing my b… |
| 129 | 49 8-37 | mh | H | F | 29 | ~123 | 0.34/0.73 | What kind of healthy meals did Sam start eating after getting a health sca… | D18:6: Sam: Exactly, it's all about finding the silver lining. Speaking of new things, I attended a Weight… | Evan: Wow, Sam, that's great to hear! Feeling more energized after me… |
| 130 | 49 8-37 | mh | H | F | 3 | ~148 | 0.48/0.73 | What kind of healthy meals did Sam start eating after getting a health sca… | D8:1: Sam: Hey Evan, some big news: I'm on a diet and living healthier! Been tough, but I'm determined. [… | Evan: Wow, Sam, that's great to hear! Feeling more energized after me… |
| 131 | 49 8-42 | mh | H | F | 19 | ~71 | 0.39/0.77 | How did Evan get into painting? | D1:14: Evan: Yep, it's a great stress-buster. I started doing this a few years back. [image: a photo of a … | Sam: Hey Evan, that sounds like a fun and unexpected event! It's alwa… |
| 132 | 49 8-42 | mh | H | F | 23 | 29 | 0.71/0.77 | How did Evan get into painting? | D1:16: Evan: My friend got me into it and gave me some advice, and I was hooked right away! | Sam: Hey Evan, that sounds like a fun and unexpected event! It's alwa… |
| 133 | 49 8-48 | mh | H | R | ~75 | ~136 | -0.06 | What new diet and lifestyle change did Sam adopt over time? | D21:9: Sam: Wish I could feel the same about love, but I've started to enjoy running in the mornings, and … | Sam: Yep, I'm reducing my soda and candy intake. It's tough, but I'm … |
| 134 | 49 8-50 | mh | H | R | ~81 | ~252 | -0.04 | What kind of hobbies does Evan pursue? | D1:6: Evan: We all hiked the trails last week - the views were amazing! | Evan: What other hobbies have you found for yourself? |
| 135 | 49 8-50 | mh | H | R | ~60 | ~355 | -0.03 | What kind of hobbies does Evan pursue? | D25:10: Evan: I had a great time kayaking and watching the sunset last summer - it was truly unforgettable.… | Evan: What other hobbies have you found for yourself? |
| 136 | 49 8-50 | mh | H | R | ~188 | ~83 | -0.08 | What kind of hobbies does Evan pursue? | D25:8: Evan: Glad to hear it! Nature really has a way of calming and reviving the soul. Last summer, I too… | Evan: What other hobbies have you found for yourself? |
| 137 | 49 8-50 | mh | H | F | 9 | ~209 | 0.44/0.48 | What kind of hobbies does Evan pursue? | D6:1: Evan: Hey Sam, long time no talk! Hope you're doing great. I just got back from a rad vacay with my… | Evan: What other hobbies have you found for yourself? |
| 138 | 49 8-50 | mh | H | F | 4 | ~266 | 0.48/0.48 | What kind of hobbies does Evan pursue? | D8:30: Evan: Skiing, snowboarding, and ice skating are all fun winter activities I enjoy. | Evan: What other hobbies have you found for yourself? |
| 139 | 49 8-50 | mh | H | R | ~152 | ~381 | -0.07 | What kind of hobbies does Evan pursue? | D9:6: Evan: Yeah, PT for my knee is on the cards. Hopefully I'll get an appointment soon. Till then, just… | Evan: What other hobbies have you found for yourself? |
| 140 | 49 8-54 | mh | H | R | ~98 | ~415 | -0.06 | What personal health incidents does Evan face in 2023? | D9:2: Evan: Wow, Sam, great! Glad your new diet/exercise is going well. As for me, I've hit a sore spot l… | Sam: That's awesome, Evan! What do you think made the biggest impact … |
| 141 | 49 8-62 | mh | H | R | ~109 | ~272 | -0.06 | What health scares did Sam and Evan experience? | D14:2: Evan: Hey Sam, sorry to hear about that. Gastritis can be tough. Taking care of ourselves is import… | Sam: Hey Evan, that does sound like a tough situation. I'm doing my b… |
| 142 | 49 8-64 | mh | H | F | 16 | ~41 | 0.40/0.50 | Which ailment does Sam have to face due to his weight? | D14:1: Sam: Hey Evan! I've been missing our chats. I had quite the health scare last weekend - ended up in… | Sam: Hey Evan, I need to talk to you. My friends were mocking my weig… |
| 143 | 49 8-72 | mh | H | R | ~60 | ~172 | -0.01 | Which activity did Sam resume in December 2023 after a long time? | D22:1: Sam: Hey Evan! I’m really getting into this healthier lifestyle—just took my friends on an epic hik… | Evan: Hey Sam, what's up? Long time no see, huh? Lots has happened. |
| 144 | 49 8-77 | mh | H | R | ~72 | ~285 | -0.03 | How does Evan spend his time with his bride after the wedding? | D23:15: Evan: Thanks, Sam! We're having a family get-together tonight and enjoying some homemade lasagna. S… | Evan: Hey Sam! Long time no see! Been up and down lately, got married… |
| 145 | 49 8-77 | mh | H | F | 16 | ~135 | 0.40/0.47 | How does Evan spend his time with his bride after the wedding? | D23:23: Evan: Thanks Sam! We're off to Canada next month for our honeymoon. So excited to create some aweso… | Evan: Hey Sam! Long time no see! Been up and down lately, got married… |
| 146 | 49 8-77 | mh | H | R | ~126 | ~155 | -0.05 | How does Evan spend his time with his bride after the wedding? | D23:25: Evan: We're planning to ski, try the local cuisine, and enjoy the beautiful views. We're really exc… | Evan: Hey Sam! Long time no see! Been up and down lately, got married… |
| 147 | 49 8-77 | mh | H | R | ~316 | ~456 | -0.13 | How does Evan spend his time with his bride after the wedding? | D24:9: Evan: Yeah, they were understanding, which was great. But it's a good reminder to be more careful. … | Evan: Hey Sam! Long time no see! Been up and down lately, got married… |
| 148 | 50 9-3 | mh | H | R | ~193 | ~322 | -0.10 | Which bands has Dave enjoyed listening to? | D23:9: Dave: The Fireworks headlined the festival. | Calvin: Cool, Dave! What tunes are you listening to these days? |
| 149 | 50 9-19 | mh | H | R | ~270 | ~209 | -0.17 | Which places or events has Calvin visited in Tokyo? | D12:7: Calvin: Aww, that's cool, Dave. Reminiscing is always fun! That pic you shared takes me back to my … | Calvin: Cool, Dave! I'm actually going to Tokyo next month after the … |
| 150 | 50 9-23 | mh | H | R | ~91 | ~176 | -0.03 | What is Dave's main passion? | D3:12: Dave: Last weekend I went to a car show. Classic cars are so charming and the dedication people put… | Dave: It was great to get away and reconnect with my passion. Reminde… |
| 151 | 50 9-23 | mh | H | F | 7 | ~290 | 0.46/0.48 | What is Dave's main passion? | D4:5: Dave: Thanks! Appreciate the support. My dream was to open a shop and it's a step towards my other … | Dave: It was great to get away and reconnect with my passion. Reminde… |
| 152 | 50 9-23 | mh | H | R | ~160 | ~309 | -0.05 | What is Dave's main passion? | D5:5: Dave: Thanks Calvin! Appreciate the support. I'm gonna keep learning more about auto engineering, m… | Dave: It was great to get away and reconnect with my passion. Reminde… |
| 153 | 50 9-29 | mh | H | R | ~139 | ~37 | -0.09 | What are Dave's hobbies other than fixing cars? | D27:2: Dave: Hey Calvin! That's cool that you've been networking with other artists. Nice! I've been getti… | Calvin: Thanks, Dave! It was an amazing experience - the energy and l… |
| 154 | 50 9-29 | mh | H | R | ~334 | ~77 | -0.17 | What are Dave's hobbies other than fixing cars? | D5:11: Dave: If I'm having trouble coming up with ideas, I usually immerse myself in something I love, lik… | Calvin: Thanks, Dave! It was an amazing experience - the energy and l… |
| 155 | 50 9-29 | mh | H | R | ~221 | ~282 | -0.12 | What are Dave's hobbies other than fixing cars? | D5:9: Dave: Yeah, I hear you! Driving with the wind in your hair is so calming. Taking a walk around is a… | Calvin: Thanks, Dave! It was an amazing experience - the energy and l… |
| 156 | 50 9-29 | mh | H | R | ~182 | ~289 | -0.11 | What are Dave's hobbies other than fixing cars? | D8:8: Dave: Nah, haven't gone hiking recently, but it's awesome - being in nature and pushing yourself to… | Calvin: Thanks, Dave! It was an amazing experience - the energy and l… |
| 157 | 50 9-45 | mh | H | F | 19 | ~495 | 0.39/0.48 | What shared activities do Dave and Calvin have? | D21:3: Calvin: Hey Dave, it was awesome talking to those artists! Our mutual friend knew we'd be a great f… | Calvin: No problem, Dave. Your enthusiasm and hard work show in every… |
| 158 | 50 9-45 | mh | H | R | ~270 | ~485 | -0.11 | What shared activities do Dave and Calvin have? | D21:4: Dave: Wow, Calvin, that car looks great! Working on cars really helps me relax, it's therapeutic to… | Calvin: No problem, Dave. Your enthusiasm and hard work show in every… |
| 159 | 50 9-46 | mh | H | R | ~116 | ~333 | -0.04 | What is Dave's favorite activity? | D21:4: Dave: Wow, Calvin, that car looks great! Working on cars really helps me relax, it's therapeutic to… | Dave: Definitely, working on cars is what I'm passionate about. Doing… |
| 160 | 50 9-46 | mh | H | R | ~73 | ~95 | -0.02 | What is Dave's favorite activity? | D22:7: Dave: Thanks, Calvin. It's been my goal since I was a kid and it's awesome to be able to do somethi… | Dave: Definitely, working on cars is what I'm passionate about. Doing… |
| 161 | 50 9-56 | mh | H | R | ~32 | ~68 | -0.00 | Which cities did Dave travel to in 2023? | D14:1: Dave: Hey Cal, how's it going? Something cool happened since last we talked - I got to go to a car … | Dave: Wow, Calvin! Congrats on the upcoming tour! Can't wait to see y… |
| 162 | 50 9-56 | mh | H | F | 11 | ~44 | 0.29/0.33 | Which cities did Dave travel to in 2023? | D26:2: Dave: Hey Calvin! Good to hear from you! Sounds like you had a blast in Boston - so much to do ther… | Dave: Wow, Calvin! Congrats on the upcoming tour! Can't wait to see y… |
| 163 | 50 9-58 | mh | H | F | 13 | ~218 | 0.42/0.46 | Which events in Dave's life inspired him to take up auto engineering? | D12:2: Dave: Hey Calvin, I understand the stress of getting a car serviced. Fixing cars is like therapy fo… | Dave: Thanks, Calvin! This is a dream come true for me, as I've alway… |
| 164 | 50 9-58 | mh | H | F | 9 | ~548 | 0.44/0.46 | Which events in Dave's life inspired him to take up auto engineering? | D12:4: Dave: Yeah, definitely! I have fond memories of working on cars with my dad as a kid. We spent one … | Dave: Thanks, Calvin! This is a dream come true for me, as I've alway… |
| 165 | 26 0-7 | mh | N | F | 26 | ~382 | 0.35/0.48 | What is Caroline's relationship status? | D3:13: Caroline: Yeah, I'm really lucky to have them. They've been there through everything, I've known th… | Melanie: Wow, Caroline. We've come so far, but there's more to do. Yo… |
| 166 | 26 0-34 | mh | N | R | ~251 | ~138 | -0.13 | What events has Caroline participated in to help children? | D3:3: Caroline: Thanks, Mel! Your backing really means a lot. I felt super powerful giving my talk. I sha… | Melanie: Wow, Caroline. We've come so far, but there's more to do. Yo… |
| 167 | 26 0-37 | mh | N | F | 20 | ~184 | 0.38/0.47 | What did Melanie paint recently? | D9:17: Melanie: Wow, Caroline! It really conveys unity and strength - such a gorgeous piece! My kids and I… | Melanie: Painting landscapes and still life is my favorite! Nature's … |
| 168 | 26 0-38 | mh | N | F | 17 | ~308 | 0.40/0.48 | What activities has Melanie done with her family? | D3:14: Melanie: I'm lucky to have my husband and kids; they keep me motivated. [image: a photo of a man an… | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 169 | 26 0-38 | mh | N | R | ~89 | ~137 | -0.06 | What activities has Melanie done with her family? | D8:4: Melanie: The kids loved it! They were so excited to get their hands dirty and make something with c… | Melanie: That was a blast! So much fun with the whole gang! Wanna do … |
| 170 | 26 0-40 | mh | N | F | ~39 | 15 | 0.27/0.56 | How many times has Melanie gone to the beach in 2023? | D6:16: Melanie: Glad you have support, Caroline! Unconditional love is so important. Here's a pic of my fa… | Melanie: Seeing my kids' faces so happy at the beach was the best! We… |
| 171 | 26 0-43 | mh | N | R | ~178 | ~161 | -0.13 | What kind of art does Caroline make? | D11:12: Caroline: Thanks, Melanie. Here's one- 'Embracing Identity' is all about finding comfort and love i… | Caroline: The room was electric with energy and support! The posters … |
| 172 | 26 0-47 | mh | N | F | ~50 | 5 | 0.47/0.48 | Who supports Caroline when she has a negative experience? | D12:1: Caroline: Hey Mel! How're ya doin'? Recently, I had a not-so-great experience on a hike. I ran into… | Caroline: Seeing my mentee's face light up when they saw the support … |
| 173 | 26 0-48 | mh | N | R | ~207 | ~364 | -0.14 | What types of pottery have Melanie and her kids made? | D12:14: Melanie: I appreciate our friendship too, Caroline. You've always been there for me. | Melanie: Hey Caroline, it's been super busy here. So much since we ta… |
| 174 | 26 0-75 | mh | N | R | ~103 | ~310 | -0.06 | How many children does Melanie have? | D18:1: Melanie: Hey Caroline, that roadtrip this past weekend was insane! We were all freaked when my son … | Caroline: That's awesome, Melanie! How have your family been supporti… |
| 175 | 30 1-27 | mh | N | R | ~36 | ~312 | -0.01 | Did Jon and Gina both participate in dance competitions? | D14:14: Gina: Way to go, Jon! Don't quit, remember, failures lead you closer to success. Here's a pic from … | Gina: Searching for the perfect dance studio's a tough job, Jon. Hang… |
| 176 | 30 1-31 | mh | N | R | ~42 | ~348 | -0.03 | How long did it take for Jon to open his studio? | D15:13: Jon: Yeah! Let's make some awesome memories tomorrow at the grand opening! [image: a photo of a man… | Gina: Hey Jon! Long time no talk! Last week, I built a new website fo… |
| 177 | 30 1-31 | mh | N | F | ~32 | 30 | 0.34/0.48 | How long did it take for Jon to open his studio? | D1:2: Jon: Hey Gina! Good to see you too. Lost my job as a banker yesterday, so I'm gonna take a shot at … | Gina: Hey Jon! Long time no talk! Last week, I built a new website fo… |
| 178 | 30 1-44 | sh | N | R | ~273 | ~189 | -0.25 | What does Gina say about the dancers in the photo? | D1:26: Jon: Yeah, they're the ones performing at the festival! They've been practicing hard and will defin… | Jon: Thanks, Gina! My dance studio and some other schools are bringin… |
| 179 | 41 2-32 | mh | N | R | ~229 | ~230 | -0.09 | What outdoor activities has John done with his colleagues? | D16:2: Maria: Hey John! Cool that it's going well - you and your friends look like a great team! I'm busy … | John: Yeah, everyone got a chance to swing. It's always fun coming up… |
| 180 | 41 2-44 | mh | N | R | ~412 | ~543 | -0.15 | What activities has Maria done with her church friends? | D28:5: John: Thanks Maria! I may have found a job at a tech company I like that needs my mechanical skills… | Maria: Hey John, I'm here for you! Staying positive makes a big diffe… |
| 181 | 41 2-49 | mh | N | R | ~617 | - | -0.32 | What food item did Maria drop off at the homeless shelter? | D25:19: John: Yeah, it's been great for me. Let me know if you need any advice to get started. | Maria: I'm still volunteering at the homeless shelter. It's fulfillin… |
| 182 | 42 3-1 | mh | N | R | ~220 | ~104 | -0.10 | What kind of interests do Joanna and Nate share? | D20:2: Joanna: Hey Nate! Cute turtles! Bummer about the setback. Any positive vibes comin' your way? I jus… | Nate: Hey Joanna, I'm a big fan of them and thought it would be a fun… |
| 183 | 42 3-1 | mh | N | R | ~175 | ~190 | -0.07 | What kind of interests do Joanna and Nate share? | D3:4: Nate: Thanks, Joanna. Not much has changed for me, but I just discovered that I can make coconut mi… | Nate: Hey Joanna, I'm a big fan of them and thought it would be a fun… |
| 184 | 42 3-51 | mh | N | R | ~32 | ~142 | -0.00 | When did Nate get Tilly for Joanna? | D24:2: Joanna: Hey Nate! I have been revising and perfecting the recipe I made for my family and it turned… | Nate: Glad to hear it! What made you name her Tilly? |
| 185 | 42 3-55 | mh | N | R | ~78 | ~241 | -0.02 | What is Joanna inspired by? | D4:6: Joanna: Yeah, definitely! I'm keen to try your recipe. Always up for something sweet. | Nate: Wow Joanna, those drawings are really incredible! What inspired… |
| 186 | 42 3-55 | mh | N | F | ~90 | 28 | 0.35/0.47 | What is Joanna inspired by? | D7:6: Joanna: That's amazing, Nate! Your boldness really inspired me. It reminded me of this gorgeous sun… | Nate: Wow Joanna, those drawings are really incredible! What inspired… |
| 187 | 42 3-56 | mh | N | R | ~101 | ~241 | -0.04 | What animal do both Nate and Joanna like? | D26:9: Joanna: Thanks, Nate! They make me think of strength and perseverance. They help motivate me in tou… | Joanna: That's great, Nate! Appreciate the small joys like that cute … |
| 188 | 42 3-58 | mh | N | R | ~364 | ~596 | -0.20 | What things has Nate reccomended to Joanna? | D2:14: Nate: Thanks! The turtles might be small, but both sure have big personalities. I really reccomend … | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 189 | 42 3-59 | mh | N | F | 9 | ~493 | 0.44/0.71 | What does Joanna do to remember happy memories? | D15:9: Joanna: Here ya go, a pic of my cork board. It's got quotes, photos, and little keepsakes. [image: … | Joanna: Yeah, it does! My brother wrote it - he used to make me these… |
| 190 | 42 3-61 | mh | N | F | 22 | ~533 | 0.37/0.48 | What mediums does Nate use to play games? | D22:2: Nate: Hey Joanna! That tart looks yummy! Lately, I've been doing great - I won a really big video g… | Nate: Yep! This is where I practice and compete. Sometimes I even use… |
| 191 | 42 3-62 | mh | N | F | 18 | ~232 | 0.39/0.48 | How many letters has Joanna recieved? | D14:1: Joanna: Nate, after finishing my screenplay I got a rejection letter from a major company. It reall… | Joanna: Awww! How long have you had them? |
| 192 | 42 3-62 | mh | N | F | 5 | ~426 | 0.47/0.48 | How many letters has Joanna recieved? | D18:5: Joanna: Yep. Last week, someone wrote me a letter after reading an online blog post I made about a … | Joanna: Awww! How long have you had them? |
| 193 | 42 3-67 | mh | N | R | ~43 | ~178 | -0.02 | What pets does Nate have? | D28:23: Nate: That sounds incredible! Nature truly has a way of reminding us to appreciate the beauty aroun… | Nate: Yep, totally! Pets make us so much happier and never let us dow… |
| 194 | 42 3-67 | mh | N | R | ~57 | ~118 | -0.04 | What pets does Nate have? | D8:3: Nate: Sounds fun! I probably also have loads of books I haven't read in years. Sounds like a blast … | Nate: Yep, totally! Pets make us so much happier and never let us dow… |
| 195 | 42 3-69 | mh | N | R | ~59 | ~106 | -0.05 | How many turtles does Nate have? | D8:3: Nate: Sounds fun! I probably also have loads of books I haven't read in years. Sounds like a blast … | Nate: Thanks! The turtles might be small, but both sure have big pers… |
| 196 | 42 3-70 | mh | N | F | 14 | ~478 | 0.41/0.47 | What activities does Nate do with his turtles? | D25:21: Nate: I love seeing them eat fruit - they get so hyped and it's so cute! [image: a photography of a… | Nate: It really is, I'm not sure I'll ever understand why watching my… |
| 197 | 42 3-74 | mh | N | R | ~529 | ~94 | -0.28 | What recommendations has Nate received from Joanna? | D15:15: Joanna: I would definitely recommend it! As long as your willing to explain what it is to your frie… | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 198 | 42 3-74 | mh | N | R | ~305 | ~76 | -0.19 | What recommendations has Nate received from Joanna? | D19:16: Joanna: That's a great one! Let me know what you think when your finished! | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 199 | 42 3-78 | mh | N | F | 14 | ~70 | 0.41/0.82 | How many video game tournaments has Nate participated in? | D19:1: Nate: Woah Joanna, I won an international tournament yesterday! It was wild. Gaming has brought me … | Nate: Yeah, I've just been practicing for my next video game tournema… |
| 200 | 42 3-79 | mh | N | R | ~63 | ~118 | -0.05 | How many screenplays has Joanna written? | D12:13: Nate: Wow, that looks great Joanna! Is that your third one? | Joanna: Woodhaven has had an interesting past with lots of cool peopl… |
| 201 | 42 3-79 | mh | N | R | ~130 | ~394 | -0.10 | How many screenplays has Joanna written? | D12:14: Joanna: Yep! I chose to write about this because it's really personal. It's about loss, identity, a… | Joanna: Woodhaven has had an interesting past with lots of cool peopl… |
| 202 | 42 3-79 | mh | N | F | 13 | ~416 | 0.42/0.48 | How many screenplays has Joanna written? | D5:1: Joanna: Hey Nate, it's been a minute! I wrapped up my second script, and the feels have been wild. … | Joanna: Woodhaven has had an interesting past with lots of cool peopl… |
| 203 | 42 3-80 | mh | N | F | 8 | ~52 | 0.45/0.48 | How many tournaments has Nate won? | D17:1: Nate: Hey Joanna, check this out! I won my fourth video game tournament on Friday! It was awesome c… | Nate: Definitely! And some old friends and teamates from other tourna… |
| 204 | 42 3-80 | mh | N | F | 11 | ~41 | 0.43/0.48 | How many tournaments has Nate won? | D22:2: Nate: Hey Joanna! That tart looks yummy! Lately, I've been doing great - I won a really big video g… | Nate: Definitely! And some old friends and teamates from other tourna… |
| 205 | 42 3-134 | sh | N | R | ~291 | ~297 | -0.19 | What recipe Nate offer to share with Joanna? | D16:10: Nate: Sure thing! I can give it to you tomorrow, how does that sound? | Nate: No problem, Joanna! Always happy to share them with you. Sendin… |
| 206 | 43 4-11 | mh | N | F | 19 | ~96 | 0.39/0.48 | What sports does John like besides basketball? | D1:7: John: I'm a shooting guard for the team and our season opener is next week - so excited! [image: a … | Tim: Wow, John! Moments like that make us love sports, huh? I still t… |
| 207 | 43 4-11 | mh | N | F | 26 | ~160 | 0.35/0.48 | What sports does John like besides basketball? | D2:14: John: That sounds awesome! So cool that you get to immerse yourself in that world. So glad you foun… | Tim: Wow, John! Moments like that make us love sports, huh? I still t… |
| 208 | 43 4-11 | mh | N | R | ~205 | ~196 | -0.12 | What sports does John like besides basketball? | D3:1: John: Hey Tim! Good to see you again. So much has happened in the last month - on and off the court… | Tim: Wow, John! Moments like that make us love sports, huh? I still t… |
| 209 | 43 4-17 | mh | N | R | ~52 | ~72 | -0.03 | How many games has John mentioned winning? | D22:4: John: That frog looks yummy! I haven't had one in ages. Been having some wild games lately, we play… | John: Winning was such a thrill, and it was an awesome moment. These … |
| 210 | 43 4-17 | mh | N | R | ~32 | ~394 | -0.01 | How many games has John mentioned winning? | D3:3: John: Thank you! Scoring those points was an incredible experience. The atmosphere was electric, an… | John: Winning was such a thrill, and it was an awesome moment. These … |
| 211 | 43 4-17 | mh | N | F | 11 | ~441 | 0.43/0.48 | How many games has John mentioned winning? | D5:2: John: Hi Tim! Nice to hear from you. Glad you could reconnect. As for me, lots of stuff happened si… | John: Winning was such a thrill, and it was an awesome moment. These … |
| 212 | 43 4-18 | mh | N | R | ~45 | ~161 | -0.03 | What authors has Tim read books from? | D1:14: Tim: It's been going well! Last week I talked to my friend who is a fan of Harry Potter and we're f… | Tim: Yes, they are still my favorites - I love how they take me to ot… |
| 213 | 43 4-18 | mh | N | R | ~31 | ~378 | -0.00 | What authors has Tim read books from? | D26:36: Tim: I'm really excited to watch this new show that's coming out called "The Wheel of Time". It's b… | Tim: Yes, they are still my favorites - I love how they take me to ot… |
| 214 | 43 4-26 | mh | N | F | 29 | ~84 | 0.34/0.49 | Which TV series does Tim mention watching? | D17:1: John: Hey Tim! Great to chat again. So much has happened! | Tim: Woo-hoo! There's a new fantasy TV series coming out next month -… |
| 215 | 43 4-26 | mh | N | R | ~209 | - | -0.09 | Which TV series does Tim mention watching? | D17:11: John: Yeah, I saw "That"! It's amazing to see those worlds and characters come alive. It's a great … | Tim: Woo-hoo! There's a new fantasy TV series coming out next month -… |
| 216 | 43 4-30 | mh | N | F | 11 | ~229 | 0.43/0.48 | Which cities has John been to? | D27:36: John: Yeah! That's why I love traveling - it's a way to learn about different cultures and places. … | Tim: Hey John, it's been a few days! I got a no for a summer job I wa… |
| 217 | 43 4-49 | mh | N | F | 9 | ~73 | 0.44/0.47 | How many times has John injured his ankle? | D18:2: John: Hey Tim! That's awesome! Yeah, it was really cool. Oh man, it's been a tough week for me with… | John: Yeah, I injured myself not too long ago. It sucked because I ha… |
| 218 | 43 4-50 | mh | N | F | ~54 | 30 | 0.34/0.50 | Which book was John reading during his recovery from an ankle injury? | D18:2: John: Hey Tim! That's awesome! Yeah, it was really cool. Oh man, it's been a tough week for me with… | John: Last season, I had a major challenge when I hurt my ankle. It r… |
| 219 | 43 4-65 | mh | N | F | 23 | ~380 | 0.37/0.48 | What fantasy movies does Tim like? | D26:28: Tim: Yeah, I have! Watching them and seeing how they compare to the books is awesome. It's amazing … | Tim: That's amazing! Same here. There's something special about being… |
| 220 | 43 4-65 | mh | N | R | ~58 | ~400 | -0.05 | What fantasy movies does Tim like? | D8:18: Tim: It was really a dream come true! Watching that movie with my family was awesome, we'd all get … | Tim: That's amazing! Same here. There's something special about being… |
| 221 | 44 5-2 | mh | N | F | 30 | ~45 | 0.34/0.48 | What kind of indoor activities has Andrew pursued with his girlfriend? | D23:1: Andrew: Hey Audrey, it's been a busy week for me. Last Tuesday, my gf, Toby, and I had a really awe… | Andrew: Hey Audrey, how's it going? Since we last talked, a few new t… |
| 222 | 44 5-3 | mh | N | R | ~82 | ~74 | -0.04 | What kind of places have Andrew and his girlfriend checked out around the … | D23:3: Andrew: Friday night's board game session was a nice break. This weekend, I'm planning to check out… | Audrey: Yeah, Andrew! The pups and I are loving it. Being out in natu… |
| 223 | 44 5-3 | mh | N | F | 14 | ~68 | 0.41/0.48 | What kind of places have Andrew and his girlfriend checked out around the … | D6:1: Andrew: Hi Audrey! I had a great hike last weekend with some friends and my girlfriend at the spot … | Audrey: Yeah, Andrew! The pups and I are loving it. Being out in natu… |
| 224 | 44 5-17 | mh | N | F | 5 | ~76 | 0.47/0.48 | How many times did Audrey and Andew plan to hike together? | D24:13: Audrey: I can't wait for our hike with the furry friends next month - it's gonna be awesome! | Audrey: It's been tough at times, but overall it's going great. We're… |
| 225 | 44 5-24 | mh | N | R | ~91 | ~197 | -0.05 | Did Audrey and Andrew grow up with a pet dog? | D13:10: Audrey: Max and I would take long walks in the neighborhood when I was a kid. We explored new paths… | Audrey: Hey Andrew! That hike sounds great. Nature is good for the so… |
| 226 | 44 5-24 | mh | N | R | ~86 | ~37 | -0.04 | Did Audrey and Andrew grow up with a pet dog? | D2:16: Andrew: It'd be so great to have a furry buddy to cuddle and hang with. Here's a photo of my family… | Audrey: Hey Andrew! That hike sounds great. Nature is good for the so… |
| 227 | 44 5-26 | mh | N | R | ~55 | ~304 | -0.04 | What is the biggest stressor in Andrew's life besides not being able to hi… | D10:16: Andrew: Thanks! I'll give it a try. Cooking has been helping me de-stress and be creative. I'm stil… | Andrew: Haven't been to the beach in a while. Miss being outdoors. It… |
| 228 | 44 5-27 | mh | N | F | 5 | ~371 | 0.47/0.49 | How does Andrew feel about his current work? | D10:16: Andrew: Thanks! I'll give it a try. Cooking has been helping me de-stress and be creative. I'm stil… | Audrey: Hey Andrew, good to hear from you. Sorry to hear about work b… |
| 229 | 44 5-27 | mh | N | F | 4 | ~49 | 0.48/0.49 | How does Andrew feel about his current work? | D12:3: Andrew: Haha yeah! Toby's definitely bringing a lot of joy. Since we last talked, work has been pil… | Audrey: Hey Andrew, good to hear from you. Sorry to hear about work b… |
| 230 | 44 5-31 | mh | N | R | ~235 | ~366 | -0.16 | What has Andrew done with his dogs? | D14:27: Andrew: Thanks! Gotta take Toby out for a small hike at the local trail. Ttyl! | Andrew: Thanks, I think that's what I need to hear. I'll take good ca… |
| 231 | 44 5-56 | mh | N | R | ~41 | ~74 | -0.01 | Has Andrew moved into a new apartment for his dogs? | D28:12: Andrew: Yeah, safety first! For now, we're keeping the new addition on a leash while they get used … | Andrew: Thanks! Fingers crossed for the apartment and that furry frie… |
| 232 | 44 5-60 | mh | N | R | ~30 | ~175 | -0.00 | How many dogs does Andrew have? | D12:1: Andrew: Hey! So much has changed since last time we talked - meet Toby, my puppy. He's a bundle of … | Andrew: Thanks! We feel so lucky to have Scout. It's been amazing hav… |
| 233 | 44 5-60 | mh | N | F | 15 | ~476 | 0.41/0.47 | How many dogs does Andrew have? | D24:2: Andrew: Hi Audrey! Pets really can make our lives better, huh? Speaking of which, I've got some awe… | Andrew: Thanks! We feel so lucky to have Scout. It's been amazing hav… |
| 234 | 44 5-60 | mh | N | F | 27 | ~487 | 0.35/0.47 | How many dogs does Andrew have? | D28:6: Andrew: No, we haven't got the chance to take them to the groomer yet. But will do that soon! So gu… | Andrew: Thanks! We feel so lucky to have Scout. It's been amazing hav… |
| 235 | 47 6-3 | mh | N | R | ~129 | ~667 | -0.07 | Do both James and John have pets? | D1:12: James: It would be cool! For example, we could write some kind of application for dogs. By the way,… | John: Yeah, the bond between us and our pets is amazing. They bring a… |
| 236 | 47 6-24 | mh | N | R | ~135 | ~424 | -0.11 | What kind of games has James tried to develop? | D13:7: John: Cool! That sounds awesome. Combining your love of gaming and coding sounds like a dream. Tell… | John: Cool, James! What kind of games are you excited to play on it? |
| 237 | 47 6-24 | mh | N | R | ~41 | ~194 | -0.01 | What kind of games has James tried to develop? | D27:2: James: Hey John, congrats! Something cool happened to me recently. I made my first game and release… | John: Cool, James! What kind of games are you excited to play on it? |
| 238 | 48 7-19 | mh | N | R | ~57 | ~261 | -0.02 | How many times has Jolene been to France? | D1:8: Jolene: Staying connected is super important. Do you have something to remember her by? This pendan… | Jolene: Here is one more photo from Rio de Janeiro. We went on many e… |
| 239 | 48 7-82 | mh | N | F | 28 | ~322 | 0.35/0.47 | Which locations does Deborah practice her yoga at? | D2:11: Deborah: This is one of the places where I do it. [image: a photo of a living room with a televisio… | Deborah: Wow, cool that yoga has been helping you out! Do they also d… |
| 240 | 48 7-86 | mh | N | R | ~95 | ~343 | -0.04 | What gifts has Deborah received? | D23:20: Deborah: Exploring historical places and learning their stories is so fun. It was a great experienc… | Deborah: Anna also has a pendant that she wears in memory of her moth… |
| 241 | 48 7-188 | sh | N | R | ~196 | ~46 | -0.13 | What outdoor activity did Jolene suggest doing together with Deborah? | D29:27: Deborah: It's okay, maybe we can try it together sometime! | Deborah: Sounds good, Jolene! When did you have in mind? That cafe ro… |
| 242 | 49 8-7 | mh | N | F | 7 | ~348 | 0.46/0.49 | What new hobbies did Sam consider trying? | D21:19: Sam: I do sketch occasionally, but I haven't created anything remarkable yet. I have a feeling I'll… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 243 | 49 8-11 | mh | N | R | ~149 | ~417 | -0.13 | What health issue did Sam face that motivated him to change his lifestyle? | D10:6: Sam: It's usually stress, boredom, or just wanting comfort. You know, those sugary treats are so te… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 244 | 49 8-11 | mh | N | R | ~135 | ~81 | -0.12 | What health issue did Sam face that motivated him to change his lifestyle? | D13:2: Sam: Hey Evan! It's been a rough week - I gave in and bought some unhealthy snacks. I feel kinda gu… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 245 | 49 8-11 | mh | N | F | 8 | ~61 | 0.45/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D14:1: Sam: Hey Evan! I've been missing our chats. I had quite the health scare last weekend - ended up in… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 246 | 49 8-11 | mh | N | F | 23 | ~77 | 0.37/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D15:1: Sam: Morning, Evan. I've been trying to keep up with my new health routine, but it's tough. My fami… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 247 | 49 8-11 | mh | N | R | ~44 | ~105 | -0.03 | What health issue did Sam face that motivated him to change his lifestyle? | D16:3: Sam: Thanks, Evan! Appreciate your support. It's been a journey, and being chosen as a coach is a g… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 248 | 49 8-11 | mh | N | R | ~45 | ~268 | -0.03 | What health issue did Sam face that motivated him to change his lifestyle? | D24:12: Sam: Thanks, Evan! Haven't seen a doctor in a while, but it's probably a good idea to get some advi… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 249 | 49 8-11 | mh | N | F | 19 | ~435 | 0.39/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D24:14: Sam: I'm gonna ask the doc about a balanced diet plan and getting advice on low-impact exercises, g… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 250 | 49 8-11 | mh | N | F | 17 | ~397 | 0.40/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D24:20: Sam: Between a healthier diet and yoga, I’m hoping for some positive changes. | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 251 | 49 8-11 | mh | N | F | ~90 | 10 | 0.44/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D25:3: Sam: Hey Evan, that does sound like a tough situation. I'm doing my best with my health. How did yo… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 252 | 49 8-11 | mh | N | R | ~408 | ~176 | -0.26 | What health issue did Sam face that motivated him to change his lifestyle? | D5:5: Evan: Ginger snaps are my weakness for sure! Dealing with health issues has been tough, but it's ma… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 253 | 49 8-11 | mh | N | R | ~112 | ~43 | -0.10 | What health issue did Sam face that motivated him to change his lifestyle? | D6:2: Sam: Hey Evan! Good to hear from you. Wow, Canada sounds amazing! That photo looks stunning. Wish I… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 254 | 49 8-11 | mh | N | F | 30 | ~450 | 0.34/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D8:1: Sam: Hey Evan, some big news: I'm on a diet and living healthier! Been tough, but I'm determined. [… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 255 | 49 8-14 | mh | N | R | ~34 | ~446 | -0.00 | What is Evan's favorite food? | D22:12: Evan: Exactly! They’re packed with nutrients and really easy to make. You also need to try these co… | Evan: Wow, Sam, that's great to hear! Feeling more energized after me… |
| 256 | 49 8-28 | mh | N | R | ~183 | ~242 | -0.09 | What kind of writing does Sam do to relax and cope with his health issues? | D11:7: Sam: Hey Evan, that sounds like a fun and unexpected event! It's always interesting how helping som… | Evan: Cool, Sam! Writing is a great way to express yourself. What kin… |
| 257 | 49 8-36 | mh | N | R | ~164 | ~207 | -0.07 | What kind of foods or recipes has Sam recommended to Evan? | D23:26: Sam: Sounds amazing, Ev! Skiing, trying local dishes, and enjoying the breathtaking views - the per… | Evan: That'd be great, Sam! I'm looking to add more vegetables to my … |
| 258 | 49 8-44 | mh | N | R | ~45 | ~36 | -0.05 | What kind of subjects does Evan enjoy painting? | D20:13: Evan: Yeah, trying something new and succeeding gives a great feeling of accomplishment. Give it a … | Sam: Wow, Evan! What type of nature do you enjoy painting the most? |
| 259 | 49 8-49 | mh | N | F | 11 | ~67 | 0.43/0.47 | Who was injured in Evan's family? | D11:2: Evan: Hey Sam! That's awesome about your healthier eating! For me, I had a setback last week - mess… | Sam: That's so lovely, Evan. Your family looks so happy. What's the s… |
| 260 | 49 8-49 | mh | N | R | ~38 | ~395 | -0.02 | Who was injured in Evan's family? | D11:3: Sam: Hey Evan, sorry to hear about your knee. It must be tough. Are there any ways to stay active w… | Sam: That's so lovely, Evan. Your family looks so happy. What's the s… |
| 261 | 49 8-49 | mh | N | R | ~364 | ~284 | -0.18 | Who was injured in Evan's family? | D7:10: Sam: Glad to hear his ankle is getting better. It's hard seeing someone we care about hurt. Look af… | Sam: That's so lovely, Evan. Your family looks so happy. What's the s… |
| 262 | 49 8-49 | mh | N | F | 8 | ~39 | 0.45/0.47 | Who was injured in Evan's family? | D7:9: Evan: Thanks, Sam. His ankle is getting better, but still sore. It was rough at first, but thank go… | Sam: That's so lovely, Evan. Your family looks so happy. What's the s… |
| 263 | 49 8-49 | mh | N | R | ~197 | ~200 | -0.09 | Who was injured in Evan's family? | D9:2: Evan: Wow, Sam, great! Glad your new diet/exercise is going well. As for me, I've hit a sore spot l… | Sam: That's so lovely, Evan. Your family looks so happy. What's the s… |
| 264 | 49 8-50 | mh | N | R | ~363 | ~163 | -0.14 | What kind of hobbies does Evan pursue? | D4:8: Evan: Go for it, Sam! It's tough at first, but you got this. Try flavored seltzer water instead. It… | Evan: What other hobbies have you found for yourself? |
| 265 | 49 8-54 | mh | N | R | ~31 | ~422 | -0.00 | What personal health incidents does Evan face in 2023? | D11:2: Evan: Hey Sam! That's awesome about your healthier eating! For me, I had a setback last week - mess… | Sam: That's awesome, Evan! What do you think made the biggest impact … |
| 266 | 49 8-64 | mh | N | R | ~34 | ~83 | -0.01 | Which ailment does Sam have to face due to his weight? | D7:2: Sam: Hey Evan, sorry to hear about what happened. I can imagine how hard it must have been for you.… | Sam: Hey Evan, I need to talk to you. My friends were mocking my weig… |
| 267 | 49 8-81 | mh | N | F | ~74 | 9 | 0.44/0.80 | What is a stress reliever for Evan? | D8:18: Evan: In painting classes, we've been learning about watercolors. The instructor stresses observing… | Evan: Hey Sam, work stress can really get to you. Have you tried anyt… |
| 268 | 49 8-82 | mh | N | R | ~221 | ~51 | -0.08 | What is a stress reliever for Sam? | D16:17: Sam: Wow, that sunset is stunning! It's so soothing just to see it. Is that a special spot you go t… | Evan: That smoothie bowl looks fantastic! How was the meeting? Yeah, … |
| 269 | 50 9-11 | mh | N | F | ~102 | 7 | 0.46/0.48 | What mishaps has Calvin run into? | D6:1: Calvin: Hey Dave! Long time no chat! Lots has gone down since we last caught up. | Calvin: The tour's been incredible! Performing and connecting with th… |
| 270 | 50 9-24 | mh | N | F | 12 | ~43 | 0.42/0.48 | Can Dave work with engines? | D20:1: Dave: Hey Calvin, good to catch up again! Had a tough time with my car project. Worked on the engin… | Dave: Cool, Cal! Working with them is a great chance - can't wait for… |
| 271 | 50 9-30 | mh | N | R | ~87 | ~245 | -0.04 | What kind of music does Dave listen to? | D2:10: Dave: Wow, there were so many great bands! If I had to pick a favorite, it would definitely be Aero… | Dave: Cool, Calvin! Music really helps me focus and be productive. Wh… |
| 272 | 50 9-59 | mh | N | F | 11 | ~389 | 0.43/0.47 | How many Ferraris does Calvin own? | D2:1: Calvin: Hey Dave, been a few days, so I wanted to let you in on some cool news. I just got a new ca… | Dave: Hey Calvin! What’s up? Last Friday I went to the car show. I sa… |
| 273 | 50 9-63 | mh | N | R | ~221 | ~159 | -0.09 | What style of guitars does Calvin own? | D16:4: Calvin: Performing on such a big stage was a dream come true! The energy was incredible and I felt … | Calvin: Thanks, dude! I dig how it's so unique. It's totally my style! |
| 274 | 50 9-66 | mh | N | F | 16 | ~93 | 0.40/0.48 | Do all of Dave's car restoration projects go smoothly? | D13:7: Dave: I've been working on this car, doing engine swaps and suspension modifications. Now I'm learn… | Dave: Yeah, the project is going great! Here's a pic of the car I'm r… |
| 275 | 50 9-66 | mh | N | F | 10 | ~128 | 0.44/0.48 | Do all of Dave's car restoration projects go smoothly? | D20:1: Dave: Hey Calvin, good to catch up again! Had a tough time with my car project. Worked on the engin… | Dave: Yeah, the project is going great! Here's a pic of the car I'm r… |
| 276 | 50 9-66 | mh | N | F | 7 | ~161 | 0.46/0.48 | Do all of Dave's car restoration projects go smoothly? | D27:10: Dave: Hey Calvin, photography has been great for me! The car project is doing well - I just finishe… | Dave: Yeah, the project is going great! Here's a pic of the car I'm r… |
| 277 | 26 0-14 | od | I | F | ~73 | 11 | 0.43/0.74 | Would Caroline still want to pursue counseling as a career if she hadn't r… | D3:5: Caroline: Thanks Mel! Your kind words mean a lot. Sharing our experiences isn't always easy, but I … | Caroline: Lately, I've been looking into counseling and mental health… |
| 278 | 26 0-42 | od | I | F | 11 | ~187 | 0.43/0.47 | Would Melanie be more interested in going to a national park or a theme pa… | D10:12: Melanie: We always look forward to our family camping trip. We roast marshmallows, tell stories aro… | Melanie: Thanks, Caroline. It's still a work in progress, but I'm doi… |
| 279 | 26 0-42 | od | I | R | ~109 | ~156 | -0.05 | Would Melanie be more interested in going to a national park or a theme pa… | D10:14: Melanie: I'll always remember our camping trip last year when we saw the Perseid meteor shower. It … | Melanie: Thanks, Caroline. It's still a work in progress, but I'm doi… |
| 280 | 26 0-50 | od | I | R | ~104 | ~112 | -0.04 | What would Caroline's political leaning likely be? | D12:1: Caroline: Hey Mel! How're ya doin'? Recently, I had a not-so-great experience on a hike. I ran into… | Melanie: That's awesome, Caroline! Glad to hear you found a great gro… |
| 281 | 26 0-59 | od | I | F | 12 | ~305 | 0.42/0.48 | Would Caroline be considered religious? | D14:19: Caroline: Thanks! It was made for a local church and shows time changing our lives. I made it to sh… | Melanie: Wow, Caroline! That's huge! How did it feel to be around so … |
| 282 | 26 0-69 | od | I | R | ~65 | ~147 | -0.02 | What personality traits might Melanie say Caroline has? | D13:16: Melanie: Wow, Caroline! That's amazing. You really care about being real and helping others. Wishin… | Melanie: Yeah, Caroline! I'll start thinking about what we can do. |
| 283 | 26 0-69 | od | I | F | 17 | ~59 | 0.40/0.48 | What personality traits might Melanie say Caroline has? | D7:4: Melanie: Wow, Caroline. We've come so far, but there's more to do. Your drive to help is awesome! W… | Melanie: Yeah, Caroline! I'll start thinking about what we can do. |
| 284 | 41 2-8 | od | I | R | ~81 | ~258 | -0.02 | What might John's financial status be? | D5:5: John: It's definitely isn't, Maria. My kids have so much and others don't. We really need to do som… | Maria: Glad I could help, John. What's up next for you? |
| 285 | 41 2-41 | od | I | R | ~182 | ~71 | -0.06 | Does John live close to a beach or the mountains? | D22:15: John: Yeah, Maria. Little things like this can make a big impact in how we think. Oh, and here's a … | John: Yeah, Maria. Taking time off for ourselves and our fam is so im… |
| 286 | 41 2-45 | od | I | R | ~157 | ~291 | -0.06 | Would John be open to moving to another country? | D24:3: John: I heard some cool stories from an elderly veteran named Samuel. It was inspiring and heartbre… | Maria: Thanks, John! Really appreciate your offer. Anything you can f… |
| 287 | 41 2-45 | od | I | R | ~290 | ~338 | -0.09 | Would John be open to moving to another country? | D7:2: John: Hey Maria! Wanted to let you know that I'm running for office again. It's been a wild ride, b… | Maria: Thanks, John! Really appreciate your offer. Anything you can f… |
| 288 | 41 2-50 | od | I | R | ~397 | ~484 | -0.12 | What attributes describe John? | D26:6: John: It was chaotic when we arrived, but we pulled together. I got a surge of energy and purpose, … | Maria: Wow, John! What a kind gesture. It's really cool seeing you ma… |
| 289 | 41 2-50 | od | I | R | ~93 | ~453 | -0.03 | What attributes describe John? | D2:14: John: Yeah, they are my rock in tough times and always cheer me on. I'm really thankful for their l… | Maria: Wow, John! What a kind gesture. It's really cool seeing you ma… |
| 290 | 41 2-50 | od | I | R | ~392 | ~545 | -0.12 | What attributes describe John? | D3:5: John: We held some events and got to meet some people. We went to a homeless shelter to give out fo… | Maria: Wow, John! What a kind gesture. It's really cool seeing you ma… |
| 291 | 41 2-50 | od | I | R | ~481 | ~235 | -0.15 | What attributes describe John? | D4:6: John: I tried to stay calm and asked for assistance, which helped me handle the situation and make … | Maria: Wow, John! What a kind gesture. It's really cool seeing you ma… |
| 292 | 41 2-64 | od | I | R | ~327 | ~377 | -0.13 | What job might Maria pursue in the future? | D11:10: Maria: I recently gave a few talks at the homeless shelter I volunteer at. It was really fulfilling… | Maria: I bet! What are your plans for the future? |
| 293 | 41 2-64 | od | I | R | ~177 | ~295 | -0.07 | What job might Maria pursue in the future? | D27:4: Maria: I started volunteering here about a year ago after witnessing a family struggling on the str… | Maria: I bet! What are your plans for the future? |
| 294 | 41 2-64 | od | I | R | ~281 | ~132 | -0.11 | What job might Maria pursue in the future? | D32:14: Maria: Hey John, I'm here for you. Last Friday, I spent some time at the shelter volunteering at th… | Maria: I bet! What are your plans for the future? |
| 295 | 41 2-64 | od | I | R | ~96 | ~243 | -0.04 | What job might Maria pursue in the future? | D5:8: Maria: I started volunteering to help make a difference. My aunt believed in volunteering, and used… | Maria: I bet! What are your plans for the future? |
| 296 | 42 3-0 | od | I | R | ~374 | ~602 | -0.19 | Is it likely that Nate has friends besides Joanna? | D1:7: Nate: The game was called Counter-Strike: Global Offensive, and me and my team had a blast to the v… | Nate: Definitely! And some old friends and teamates from other tourna… |
| 297 | 42 3-14 | od | I | R | ~184 | ~66 | -0.10 | What nickname does Nate use for Joanna? | D7:1: Nate: Hey Jo, guess what I did? Dyed my hair last week - come see! | Nate: Wow, Joanna, that sounds amazing! Keep doing what you love! |
| 298 | 42 3-66 | od | I | R | ~143 | ~118 | -0.09 | What alternative career might Nate consider after gaming? | D19:3: Nate: I'm really stoked to see all my hard work paying off! I'm super proud of what I accomplished.… | Nate: Thanks Joanna! Staying positive is key. I'm thinking of joining… |
| 299 | 42 3-66 | od | I | R | ~444 | ~353 | -0.23 | What alternative career might Nate consider after gaming? | D25:19: Nate: They eat a combination of vegetables, fruits, and insects. They have a varied diet. [image: a… | Nate: Thanks Joanna! Staying positive is key. I'm thinking of joining… |
| 300 | 42 3-66 | od | I | R | ~328 | ~475 | -0.15 | What alternative career might Nate consider after gaming? | D28:25: Nate: Turtles really bring me joy and peace. They have such an effect on us - best buddies ever! I … | Nate: Thanks Joanna! Staying positive is key. I'm thinking of joining… |
| 301 | 42 3-66 | od | I | R | ~417 | ~347 | -0.20 | What alternative career might Nate consider after gaming? | D5:8: Nate: No, not really. Just keep their area clean, feed them properly, and make sure they get enough… | Nate: Thanks Joanna! Staying positive is key. I'm thinking of joining… |
| 302 | 42 3-68 | od | I | F | 9 | ~163 | 0.44/0.47 | How many hikes has Joanna been on? | D28:22: Joanna: Thanks, Nate! I took that pic on a hike last summer near Fort Wayne. The sunset and the sur… | Joanna: Trying out different flavors like chocolate, raspberry, and c… |
| 303 | 42 3-68 | od | I | F | 28 | ~475 | 0.35/0.47 | How many hikes has Joanna been on? | D7:6: Joanna: That's amazing, Nate! Your boldness really inspired me. It reminded me of this gorgeous sun… | Joanna: Trying out different flavors like chocolate, raspberry, and c… |
| 304 | 42 3-87 | od | I | F | 7 | ~469 | 0.46/0.48 | What state did Nate visit? | D29:6: Nate: Wow Joanna, that must have been so exciting! It's incredible when you get those moments of jo… | Nate: Wow, looks great! Where did you take this picture? I love the d… |
| 305 | 43 4-3 | od | I | R | ~59 | ~106 | -0.04 | Would Tim enjoy reading books by C. S. Lewis or John Greene? | D1:14: Tim: It's been going well! Last week I talked to my friend who is a fan of Harry Potter and we're f… | John: Thanks, Tim! It's awesome to see how sports can unite people. B… |
| 306 | 43 4-3 | od | I | R | ~307 | ~284 | -0.16 | Would Tim enjoy reading books by C. S. Lewis or John Greene? | D1:16: Tim: Thanks! We'll be discussing various aspects of the Harry Potter universe, like characters, spe… | John: Thanks, Tim! It's awesome to see how sports can unite people. B… |
| 307 | 43 4-3 | od | I | R | ~95 | ~668 | -0.07 | Would Tim enjoy reading books by C. S. Lewis or John Greene? | D1:18: Tim: I went to a place in London a few years ago - it was like walking into a Harry Potter movie! I… | John: Thanks, Tim! It's awesome to see how sports can unite people. B… |
| 308 | 43 4-5 | od | I | R | ~156 | ~184 | -0.08 | Based on Tim's collections, what is a shop that he would enjoy visiting in… | D2:9: Tim: Thanks! That picture is from MinaLima. They created all the props for the Harry Potter films, … | John: Wow, Tim, that's an awesome book collection! It's cool to escap… |
| 309 | 43 4-19 | od | I | R | ~43 | ~167 | -0.02 | What is a prominent charity organization that John might want to work with… | D3:13: John: I just signed up Nike for a basketball shoe and gear deal. I'm also in talks with Gatorade ab… | John: I've thought about it a lot. I want to use my platform to make … |
| 310 | 43 4-19 | od | I | R | ~195 | ~254 | -0.09 | What is a prominent charity organization that John might want to work with… | D3:15: John: Thanks! The Nike and Gatorade deals have me stoked! I've always liked Under Armour, working w… | John: I've thought about it a lot. I want to use my platform to make … |
| 311 | 43 4-34 | od | I | F | 17 | ~136 | 0.40/0.48 | What could John do after his basketball career? | D26:1: John: Hey Tim! Great to hear from you. My week's been busy - I started doing seminars, helping peop… | John: Wow, that's awesome! I could stop by there after my season. |
| 312 | 43 4-34 | od | I | R | ~117 | ~291 | -0.09 | What could John do after his basketball career? | D27:26: John: He's a great leader and puts others first - that's why he eventually becomes king. | John: Wow, that's awesome! I could stop by there after my season. |
| 313 | 43 4-53 | od | I | R | ~79 | ~326 | -0.06 | What other exercises can help John with his basketball performance? | D20:2: John: Hi Tim! Congrats on your success! Keep it up, you're doing great! I'm also trying out yoga to… | John: Yeah, basketball is still really important to me - I practice a… |
| 314 | 43 4-67 | od | I | R | ~53 | ~294 | -0.02 | What would be a good hobby related to his travel dreams for Tim to pick up? | D15:3: Tim: That castle looks amazing! I hope I get to visit it someday. My writing is going well: I'm in … | Tim: I'm proud of researching visa requirements for countries I want … |
| 315 | 43 4-67 | od | I | R | ~252 | ~336 | -0.13 | What would be a good hobby related to his travel dreams for Tim to pick up? | D4:1: Tim: Hey John! How've you been? Something awesome happened - I'm writing articles about fantasy nov… | Tim: I'm proud of researching visa requirements for countries I want … |
| 316 | 43 4-67 | od | I | R | ~70 | ~213 | -0.05 | What would be a good hobby related to his travel dreams for Tim to pick up? | D6:6: Tim: I can just imagine the thrill of being in that kind of atmosphere. Must've been an amazing exp… | Tim: I'm proud of researching visa requirements for countries I want … |
| 317 | 44 5-19 | od | I | R | ~120 | ~200 | -0.08 | What is an indoor activity that Andrew would enjoy doing while make his do… | D10:12: Andrew: Lately I've been finding new hobbies since I can't hike. I've been getting into cooking mor… | Andrew: You bet! Can't wait to see their happy face! This was my dog … |
| 318 | 44 5-19 | od | I | R | ~62 | ~343 | -0.03 | What is an indoor activity that Andrew would enjoy doing while make his do… | D12:1: Andrew: Hey! So much has changed since last time we talked - meet Toby, my puppy. He's a bundle of … | Andrew: You bet! Can't wait to see their happy face! This was my dog … |
| 319 | 44 5-33 | od | I | R | ~171 | ~311 | -0.10 | What can Andrew potentially do to improve his stress and accomodate his li… | D21:5: Andrew: Haven't been to the beach in a while. Miss being outdoors. It's hard to find open spaces in… | Andrew: Thanks! He's doing great in his new home. Still getting used … |
| 320 | 44 5-43 | od | I | R | ~305 | ~667 | -0.12 | Which US state do Audrey and Andrew potentially live in? | D11:9: Andrew: Looking forward to seeing them have fun hiking. Let's get planning for next month! Here's t… | Audrey: Yeah, Andrew! The pups and I are loving it. Being out in natu… |
| 321 | 44 5-44 | od | I | R | ~124 | ~155 | -0.06 | Which national park could Audrey and Andrew be referring to in their conve… | D11:9: Andrew: Looking forward to seeing them have fun hiking. Let's get planning for next month! Here's t… | Audrey: Yeah, I took them for a hike before. We went to a national pa… |
| 322 | 44 5-52 | od | I | F | 22 | ~180 | 0.37/0.48 | What is something that Andrew could do to make birdwatching hobby to fit i… | D1:14: Andrew: I've always been awed by birds. Their power to soar and explore new spots is amazing. | Andrew: Yeah do that! It's really peaceful and calming. It's nice to … |
| 323 | 44 5-52 | od | I | R | ~285 | ~262 | -0.13 | What is something that Andrew could do to make birdwatching hobby to fit i… | D20:5: Andrew: Aww, they look so cute! That spot looks ideal for them to play. Where did you take them? | Andrew: Yeah do that! It's really peaceful and calming. It's nice to … |
| 324 | 44 5-52 | od | I | R | ~153 | ~225 | -0.08 | What is something that Andrew could do to make birdwatching hobby to fit i… | D23:1: Andrew: Hey Audrey, it's been a busy week for me. Last Tuesday, my gf, Toby, and I had a really awe… | Andrew: Yeah do that! It's really peaceful and calming. It's nice to … |
| 325 | 44 5-53 | od | I | F | ~51 | 20 | 0.38/0.50 | What is a career that Andrew could potentially pursue with his love for an… | D2:18: Andrew: That pic is so cute! It would be fun to hang out with a dog, cuddling away. Got me thinking… | Andrew: No, no pets right now. But I do love animals. |
| 326 | 44 5-53 | od | I | R | ~138 | ~60 | -0.09 | What is a career that Andrew could potentially pursue with his love for an… | D3:1: Andrew: Hey Audrey! What's up? Missed chatting with ya! Check it out, my girl & I tried out that ne… | Andrew: No, no pets right now. But I do love animals. |
| 327 | 44 5-53 | od | I | R | ~34 | ~95 | -0.00 | What is a career that Andrew could potentially pursue with his love for an… | D5:7: Andrew: I'm looking for a place near a park or woods, so I can stay close to nature and give the do… | Andrew: No, no pets right now. But I do love animals. |
| 328 | 44 5-53 | od | I | R | ~82 | ~80 | -0.05 | What is a career that Andrew could potentially pursue with his love for an… | D8:27: Andrew: Agreed! It's great for refreshing the mind and giving a different outlook. Whenever I'm in … | Andrew: No, no pets right now. But I do love animals. |
| 329 | 47 6-6 | od | I | R | ~77 | ~60 | -0.03 | Does James live in Connecticut? | D5:1: James: Hey John! Long time no chat - I adopted a pup from a shelter in Stamford last week and my da… | James: I actually have something new, Samantha and I have decided to … |
| 330 | 47 6-19 | od | I | R | ~104 | ~223 | -0.04 | Was James feeling lonely before meeting Samantha? | D9:16: James: My pets, computer games, travel and pizza are all that bring me happiness in life. | James: I actually have something new, Samantha and I have decided to … |
| 331 | 47 6-33 | od | I | R | ~505 | ~667 | -0.18 | Did John and James study together? | D17:13: John: Your support means a lot to me. You're a true friend! Remember this photo from elementary sch… | John: Wow, that's cool, James! Seeing them bonding and having a great… |
| 332 | 48 7-11 | od | I | R | ~196 | ~336 | -0.08 | Is Deborah married? | D19:11: Deborah: Reminds me of when I used to play games with my husband. We'd take turns and it was a grea… | Deborah: Aw, that's wonderful! How long have you been married? |
| 333 | 48 7-23 | od | I | R | ~416 | ~173 | -0.21 | Why did Jolene sometimes put off doing yoga? | D2:30: Jolene: We are planning to play "Walking Dead" next Saturday. | Jolene: They seriously saved me. I chill out and gain perspective whe… |
| 334 | 48 7-23 | od | I | R | ~129 | ~189 | -0.08 | Why did Jolene sometimes put off doing yoga? | D3:11: Jolene: Well... we planned to play the console with my partner. | Jolene: They seriously saved me. I chill out and gain perspective whe… |
| 335 | 48 7-36 | od | I | R | ~178 | ~456 | -0.07 | How old is Jolene? | D13:5: Jolene: I'm interning at a well-known engineering firm. It's been a great opportunity to test my sk… | Jolene: How old is Luna? |
| 336 | 48 7-36 | od | I | R | ~326 | ~171 | -0.15 | How old is Jolene? | D21:6: Jolene: My goal is to be successful in my field and make a positive impact. I've been studying, att… | Jolene: How old is Luna? |
| 337 | 48 7-36 | od | I | R | ~255 | ~481 | -0.10 | How old is Jolene? | D21:8: Jolene: I was thrilled to receive such positive feedback! It felt so rewarding to know that my effo… | Jolene: How old is Luna? |
| 338 | 48 7-36 | od | I | R | ~184 | ~318 | -0.07 | How old is Jolene? | D22:14: Jolene: It helps with challenges, giving balance and strength. Any tips for staying relaxed while s… | Jolene: How old is Luna? |
| 339 | 48 7-36 | od | I | R | ~378 | ~191 | -0.21 | How old is Jolene? | D22:6: Jolene: Yeah, Deborah! We've been figuring out how to add these values into our projects. As an eng… | Jolene: How old is Luna? |
| 340 | 48 7-36 | od | I | R | ~58 | ~455 | -0.02 | How old is Jolene? | D24:14: Jolene: Got a lot of finals coming up this month, so I've been studying real hard. It's been quite … | Jolene: How old is Luna? |
| 341 | 48 7-36 | od | I | R | ~332 | ~132 | -0.15 | How old is Jolene? | D24:2: Jolene: Hey Deb, great to hear from you! I've been focusing on studying and my relationship with my… | Jolene: How old is Luna? |
| 342 | 48 7-36 | od | I | R | ~288 | ~255 | -0.11 | How old is Jolene? | D25:5: Jolene: Yeah, same! It helps me stay balanced during my studies. | Jolene: How old is Luna? |
| 343 | 48 7-36 | od | I | R | ~478 | ~442 | -0.34 | How old is Jolene? | D26:6: Jolene: Having a routine helps me stay on top of everything I need to do. I have a schedule for cla… | Jolene: How old is Luna? |
| 344 | 48 7-36 | od | I | R | ~375 | ~101 | -0.20 | How old is Jolene? | D8:2: Jolene: One of my favorite dishes is lasagna! Comfort food can be a great pick-me-up. I've got a lo… | Jolene: How old is Luna? |
| 345 | 49 8-10 | od | I | R | ~273 | ~450 | -0.13 | Which type of vacation would Evan prefer with his family, walking tours in… | D19:1: Evan: Hey Sam, hope you're doing good. Wanted to share some amazing news - my partner is pregnant! … | Evan: Hey Sam, long time no talk! Hope you're doing great. I just got… |
| 346 | 49 8-10 | od | I | R | ~71 | ~47 | -0.04 | Which type of vacation would Evan prefer with his family, walking tours in… | D19:3: Evan: So excited and a bit nervous! It's been a while since I had a toddler around but I'm really l… | Evan: Hey Sam, long time no talk! Hope you're doing great. I just got… |
| 347 | 49 8-19 | od | I | R | ~324 | ~330 | -0.20 | Considering their conversations and personal growth, what advice might Eva… | D14:12: Evan: Sure thing! Our hike is going to be awesome, I can tell. I'm always here to support you. | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 348 | 49 8-19 | od | I | R | ~224 | ~340 | -0.12 | Considering their conversations and personal growth, what advice might Eva… | D22:1: Sam: Hey Evan! I’m really getting into this healthier lifestyle—just took my friends on an epic hik… | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 349 | 49 8-19 | od | I | R | ~360 | ~392 | -0.23 | Considering their conversations and personal growth, what advice might Eva… | D3:10: Sam: Yeah, you're right. It takes time, but I'm up for keep trying and making those tiny changes. | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 350 | 49 8-19 | od | I | R | ~107 | ~213 | -0.05 | Considering their conversations and personal growth, what advice might Eva… | D3:15: Evan: Sure Sam, I'd be glad to help. Let's get together and I'll show you some basic exercises. We'… | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 351 | 49 8-19 | od | I | R | ~228 | ~110 | -0.12 | Considering their conversations and personal growth, what advice might Eva… | D8:17: Sam: Cool, Evan! What have you been learning in those classes? | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 352 | 49 8-19 | od | I | R | ~309 | ~220 | -0.19 | Considering their conversations and personal growth, what advice might Eva… | D8:22: Evan: Thanks Sam! I aim to capture the vibe of nature in my paintings, conveying the peacefulness o… | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 353 | 49 8-19 | od | I | R | ~348 | ~179 | -0.22 | Considering their conversations and personal growth, what advice might Eva… | D9:11: Sam: I haven't gone on a road trip in ages, but I love being surrounded by nature. It's so tranquil… | Sam: For sure, Evan! I'm here for ya. Life can be tough sometimes, bu… |
| 354 | 49 8-20 | od | I | R | ~59 | ~40 | -0.04 | In light of the health and dietary changes discussed, what would be an app… | D5:9: Evan: Thanks Sam! My family motivates me to stay healthy. Well, it helps a lot with my health goals… | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 355 | 49 8-20 | od | I | R | ~307 | ~25 | -0.17 | In light of the health and dietary changes discussed, what would be an app… | D7:12: Sam: I have been feeling a mix of emotions - somewhat concerned about my health but also motivated … | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 356 | 49 8-20 | od | I | F | 17 | ~281 | 0.40/0.48 | In light of the health and dietary changes discussed, what would be an app… | D8:1: Sam: Hey Evan, some big news: I'm on a diet and living healthier! Been tough, but I'm determined. [… | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 357 | 49 8-20 | od | I | R | ~150 | ~307 | -0.08 | In light of the health and dietary changes discussed, what would be an app… | D8:12: Evan: Thanks Sam! I'll give it a shot and let you know how it went. Trying out new recipes is a gre… | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 358 | 49 8-20 | od | I | R | ~357 | ~437 | -0.20 | In light of the health and dietary changes discussed, what would be an app… | D8:5: Sam: Yes, there are many, such as more energy and less sluggishness after eating. This is really en… | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 359 | 49 8-20 | od | I | R | ~320 | ~111 | -0.18 | In light of the health and dietary changes discussed, what would be an app… | D8:7: Sam: Sure, I'm loving this recipe I found. It's a flavorful and healthy grilled chicken and veggie … | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 360 | 49 8-20 | od | I | R | ~268 | ~347 | -0.15 | In light of the health and dietary changes discussed, what would be an app… | D8:8: Evan: Mmm, looks yummy! Is the sauce a family secret? I'm always down to try new recipes! | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 361 | 49 8-20 | od | I | R | ~43 | ~47 | -0.03 | In light of the health and dietary changes discussed, what would be an app… | D9:1: Sam: Hey Evan! Exciting news: I started a new diet and exercise routine last Monday and it's made a… | Evan: I made some dietary changes, like cutting down on sugary snacks… |
| 362 | 49 8-43 | od | I | F | 5 | ~331 | 0.47/0.48 | How often does Sam get health checkups? | D2:6: Sam: Thanks, Evan. Appreciate the offer, but had a check-up with my doctor a few days ago and, yike… | Evan: Hey Sam! Long time no talk! How're you doing? Life's been quite… |
| 363 | 49 8-43 | od | I | R | ~116 | ~83 | -0.06 | How often does Sam get health checkups? | D7:2: Sam: Hey Evan, sorry to hear about what happened. I can imagine how hard it must have been for you.… | Evan: Hey Sam! Long time no talk! How're you doing? Life's been quite… |
| 364 | 49 8-51 | od | I | R | ~35 | ~148 | -0.00 | What challenges does Sam face in his quest for a healthier lifestyle, and … | D14:1: Sam: Hey Evan! I've been missing our chats. I had quite the health scare last weekend - ended up in… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 365 | 49 8-51 | od | I | R | ~64 | ~120 | -0.04 | What challenges does Sam face in his quest for a healthier lifestyle, and … | D14:2: Evan: Hey Sam, sorry to hear about that. Gastritis can be tough. Taking care of ourselves is import… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 366 | 49 8-51 | od | I | R | ~141 | ~475 | -0.10 | What challenges does Sam face in his quest for a healthier lifestyle, and … | D4:2: Evan: Hey Sam, sorry about that. Don't worry, progress takes time. Let's work on it together. | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 367 | 49 8-51 | od | I | R | ~70 | ~260 | -0.05 | What challenges does Sam face in his quest for a healthier lifestyle, and … | D4:6: Evan: I made some dietary changes, like cutting down on sugary snacks and eating more veggies and f… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 368 | 50 9-4 | od | I | R | ~257 | ~133 | -0.12 | Which country do Calvin and Dave want to meet in? | D3:10: Dave: Sounds like an amazing plan, Cal! I can't wait for your trip to Boston. I'll show you around … | Calvin: Yeah Dave, someone even noticed my performance and now we're … |
| 369 | 50 9-4 | od | I | F | 27 | ~244 | 0.35/0.48 | Which country do Calvin and Dave want to meet in? | D3:9: Calvin: Thanks, Dave! I'm dreaming of touring the world, performing for different people and connec… | Calvin: Yeah Dave, someone even noticed my performance and now we're … |
| 370 | 50 9-37 | od | I | F | 17 | ~401 | 0.40/0.47 | Does Calvin love music tours? | D16:2: Calvin: Hey Dave! The tour was amazing! I was so pumped from all the energy from the audience. This… | Dave: That sounds awesome, Calvin! Live music is the best. I'm sure y… |
| 371 | 50 9-37 | od | I | F | 23 | ~366 | 0.37/0.47 | Does Calvin love music tours? | D18:7: Calvin: Thanks Dave! Lots of cool stuff happening. Next up, a tour - so excited! After that, I'm of… | Dave: That sounds awesome, Calvin! Live music is the best. I'm sure y… |
| 372 | 50 9-37 | od | I | F | 16 | ~399 | 0.40/0.47 | Does Calvin love music tours? | D7:1: Calvin: Hey Dave! Been ages since we chatted. So much has gone down. Touring with Frank Ocean last … | Dave: That sounds awesome, Calvin! Live music is the best. I'm sure y… |
| 373 | 26 0-35 | tp | T | F | 11 | ~37 | 0.43/0.48 | When did Melanie go camping in July? | D9:1: Melanie: Hey Caroline, hope all's good! I had a quiet weekend after we went camping with my fam two… | Melanie: Thanks, Caroline. It's still a work in progress, but I'm doi… |
| 374 | 30 1-38 | tp | T | F | ~51 | 1 | 0.50/0.73 | When did Gina go to a dance class with a group of friends? | D19:6: Gina: Hah, yeah!) But really having a creative space for dancers is so important. Last Friday at da… | Gina: I love being around friends and having such a great time. Can't… |
| 375 | 41 2-10 | tp | T | R | ~177 | ~400 | -0.06 | When did Maria meet Jean? | D7:1: Maria: Hey John, how's it going? Just wanted to give you the heads up on what's been happening late… | John: Thanks, Maria! It was an adrenaline rush, and I couldn't have d… |
| 376 | 42 3-22 | tp | T | F | ~32 | 6 | 0.46/0.49 | When did Joanna start writing her third screenplay? | D12:13: Nate: Wow, that looks great Joanna! Is that your third one? | Joanna: Yeah Nate, your cooking is amazing! I can't stop thinking abo… |
| 377 | 42 3-22 | tp | T | R | ~172 | ~415 | -0.12 | When did Joanna start writing her third screenplay? | D12:14: Joanna: Yep! I chose to write about this because it's really personal. It's about loss, identity, a… | Joanna: Yeah Nate, your cooking is amazing! I can't stop thinking abo… |
| 378 | 42 3-57 | tp | T | F | 6 | ~25 | 0.46/0.49 | When did Joanna plan to go over to Nate's and share recipes? | D26:19: Joanna: Thanks so much, Nate! Sure! I'll come over tomorrow if that's fine. [image: a photo of a bo… | Nate: No problem, Joanna! Always happy to share them with you. Sendin… |
| 379 | 43 4-20 | tp | T | R | ~201 | ~84 | -0.10 | Which city was John in before traveling to Chicago? | D5:2: John: Hi Tim! Nice to hear from you. Glad you could reconnect. As for me, lots of stuff happened si… | John: Thanks! Yeah, I've been there before and loved it! That place i… |
| 380 | 43 4-24 | tp | T | F | 17 | ~46 | 0.26/0.33 | Where was John between August 11 and August 15 2023? | D7:1: John: Hey Tim! We had a wild few days since we talked. I met back up with my teammates on the 15th … | Tim: Hey John, no worries! I get how life can be busy. Where did you … |
| 381 | 43 4-45 | tp | T | F | 12 | ~83 | 0.42/0.47 | Which country was Tim visiting in the second week of November? | D18:1: Tim: Hey John! Hope you're doing good. Guess what? I went to a castle during my trip to the UK last… | Tim: Thanks! I'll keep you in the loop about my travels. Is there any… |
| 382 | 43 4-55 | tp | T | F | 12 | ~154 | 0.42/0.47 | When did John start playing professionally? | D1:3: John: That's great! I just signed with a new team - excited for the season! | John: Thanks! Basketball has been a part of my life ever since I was … |
| 383 | 44 5-0 | tp | T | F | 19 | ~519 | 0.39/0.48 | Which year did Audrey adopt the first three of her dogs? | D1:7: Audrey: I've had them for 3 years! Their names are Pepper, Precious and Panda. I can't live without… | Audrey: Pixie's fitting in great! It took her a few days to get used … |
| 384 | 44 5-8 | tp | T | F | ~85 | 12 | 0.28/0.33 | Did Andrew have a pet dog during March 2023? | D2:8: Andrew: That's great to hear! I'm considering getting a dog too, but it can be challenging finding … | Andrew: No, no pets right now. But I do love animals. |
| 385 | 44 5-16 | tp | T | R | ~36 | ~52 | -0.01 | When is Andrew going to go hiking with Audrey? | D11:7: Andrew: Yeah definitely ! I'm down for a hike with you and your furry friends. Let's do it next mon… | Audrey: Yeah, I need to go on a hike with them, it's going to be a gr… |
| 386 | 44 5-22 | tp | T | F | 4 | ~31 | 0.32/0.49 | Where did Andrew go during the first weekend of August 2023? | D14:1: Andrew: Hey, Audrey! I can't wait for the weekend. My girlfriend, Toby and I are going camping. It'… | Andrew: This weekend I'm heading to a nature reserve to reconnect wit… |
| 387 | 44 5-45 | tp | T | F | 24 | ~105 | 0.36/0.47 | How many pets will Andrew have, as of December 2023? | D12:1: Andrew: Hey! So much has changed since last time we talked - meet Toby, my puppy. He's a bundle of … | Andrew: Pets are more than just pets - they become friends and confid… |
| 388 | 44 5-45 | tp | T | F | 13 | ~52 | 0.42/0.47 | How many pets will Andrew have, as of December 2023? | D24:2: Andrew: Hi Audrey! Pets really can make our lives better, huh? Speaking of which, I've got some awe… | Andrew: Pets are more than just pets - they become friends and confid… |
| 389 | 44 5-45 | tp | T | F | 30 | ~59 | 0.34/0.47 | How many pets will Andrew have, as of December 2023? | D28:6: Andrew: No, we haven't got the chance to take them to the groomer yet. But will do that soon! So gu… | Andrew: Pets are more than just pets - they become friends and confid… |
| 390 | 44 5-46 | tp | T | F | 16 | ~106 | 0.27/0.33 | How many pets did Andrew have, as of September 2023? | D12:1: Andrew: Hey! So much has changed since last time we talked - meet Toby, my puppy. He's a bundle of … | Andrew: Pets are more than just pets - they become friends and confid… |
| 391 | 44 5-46 | tp | T | F | 8 | ~51 | 0.30/0.33 | How many pets did Andrew have, as of September 2023? | D24:2: Andrew: Hi Audrey! Pets really can make our lives better, huh? Speaking of which, I've got some awe… | Andrew: Pets are more than just pets - they become friends and confid… |
| 392 | 44 5-47 | tp | T | F | 12 | ~537 | 0.42/0.46 | How many months passed between Andrew adopting Buddy and Scout | D24:2: Andrew: Hi Audrey! Pets really can make our lives better, huh? Speaking of which, I've got some awe… | Andrew: Thanks! We feel so lucky to have Scout. It's been amazing hav… |
| 393 | 44 5-55 | tp | T | R | ~49 | ~369 | -0.02 | When did Andrew make his dogs a fun indoor area? | D28:12: Andrew: Yeah, safety first! For now, we're keeping the new addition on a leash while they get used … | Andrew: Wow you really went in huh!? Now they have a great place to p… |
| 394 | 47 6-11 | tp | T | R | ~164 | ~526 | -0.06 | How was John feeling on April 10, 2022? | D6:7: John: That's awesome. Real-life experiences can be so inspiring. It's like the virtual world is con… | John: Feeling the tug of emotion lately. Determined and passionate on… |
| 395 | 47 6-32 | tp | T | F | 26 | ~192 | 0.24/0.31 | Where was James at on July 12, 2022? | D16:9: James: I also love to read, especially while snuggled under the covers on a cold winter day. But no… | James: I plan to return on July 20, I’ll definitely bring you some ki… |
| 396 | 47 6-53 | tp | T | F | 13 | ~43 | 0.42/0.48 | How long did it take for James to complete his Witcher-inspired game? | D27:2: James: Hey John, congrats! Something cool happened to me recently. I made my first game and release… | James: Hey John! Glad you had a great week meeting new people! Someth… |
| 397 | 48 7-43 | tp | T | F | 8 | ~177 | 0.45/0.48 | When did Deborah go for a bicycle ride with Anna? | D12:1: Deborah: Hey Jolene! Great to see you! Had a blast biking nearby with my neighbor last week - was s… | Deborah: Yes, but this brought us closer to Anna! We supported each o… |
| 398 | 48 7-46 | tp | T | F | 5 | ~32 | 0.47/0.48 | How long did Jolene work on the robotics project given to her by her Profe… | D12:10: Jolene: Just so you know, I've been working on a big project lately - it's been tough but also real… | Jolene: Hey Deb, long time no talk. A lot's happened! On Friday I had… |
| 399 | 48 7-50 | tp | T | F | 21 | ~289 | 0.38/0.48 | Which year did Jolene start practicing yoga? | D13:17: Jolene: I've been doing them sporadically for about 3 years now and they've had a real positive eff… | Jolene: I've been trying to squeeze in some me-time. Last Friday, I d… |
| 400 | 48 7-52 | tp | T | F | 11 | ~86 | 0.43/0.48 | When did Jolene lose a lot of progress in her work? | D16:2: Jolene: Hey Debs! Congrats on your project for the community! As for me, life's been a rollercoaste… | Jolene: Hey Deb, long time no talk. A lot's happened! On Friday I had… |
| 401 | 48 7-54 | tp | T | F | 4 | ~215 | 0.48/0.77 | Which pet did Jolene adopt first - Susie or Seraphim? | D16:6: Jolene: I adopted her two years ago when I was feeling lonely and wanted some company. | Jolene: I was playing video games and my pet just slinked out of her … |
| 402 | 48 7-54 | tp | T | R | ~99 | ~165 | -0.04 | Which pet did Jolene adopt first - Susie or Seraphim? | D2:28: Jolene: Even as a child I learned to play on my own. | Jolene: I was playing video games and my pet just slinked out of her … |
| 403 | 48 7-55 | tp | T | R | ~183 | ~192 | -0.07 | Which pet did Jolene adopt more recently - Susie or Seraphim? | D2:28: Jolene: Even as a child I learned to play on my own. | Jolene: I was playing video games and my pet just slinked out of her … |
| 404 | 48 7-78 | tp | T | R | ~328 | ~335 | -0.14 | Where did Jolene and her partner spend most of September 2023? | D2:1: Deborah: Hey Jolene, sorry to tell you this but my dad passed away two days ago. It's been really t… | Jolene: Wow, festivals sound so fun! Here's me and my partner at one … |
| 405 | 49 8-22 | tp | T | F | 6 | ~379 | 0.46/0.48 | When Evan did meet his future wife? | D5:1: Evan: Hey Sam, how's it going? Last week I went on a trip to Canada and something unreal happened -… | Sam: Wow, Evan, you look great! How did you manage the change? |
| 406 | 49 8-25 | tp | T | R | ~392 | ~77 | -0.24 | Which year did Evan start taking care of his health seriously? | D5:6: Sam: It looks like your kids are having a great time! And how long have you been prioritizing your … | Evan: Hey Sam, tough news. Yeah, our health can really put a damper o… |
| 407 | 49 8-25 | tp | T | F | 2 | ~208 | 0.49/0.73 | Which year did Evan start taking care of his health seriously? | D5:7: Evan: Yes, they bring me such joy. My healthy road has been a long one. I've been working on it for… | Evan: Hey Sam, tough news. Yeah, our health can really put a damper o… |
| 408 | 49 8-70 | tp | T | F | 6 | ~80 | 0.46/0.48 | How long did Evan and his partner date before getting married? | D5:1: Evan: Hey Sam, how's it going? Last week I went on a trip to Canada and something unreal happened -… | Evan: Hey Sam! Long time no see! Been up and down lately, got married… |
| 409 | 50 9-10 | tp | T | F | ~43 | 4 | 0.48/0.74 | When did Calvin's place get flooded in Tokyo? | D6:3: Calvin: Hey Dave, not everything has been going smoothly. I had an incident last week where my plac… | Dave: Wow, Calvin! I bet playing for an eager audience was an incredi… |
| 410 | 50 9-27 | tp | T | R | ~70 | ~97 | -0.04 | Which city was Calvin visiting in August 2023? | D16:6: Calvin: Cool! Last weekend I started shooting a video for my new album - can't wait for you to chec… | Calvin: Not yet, been pretty busy with rehearsals and traveling. But … |
| 411 | 50 9-31 | tp | T | F | ~31 | ~38 | 0.33/0.33 | Where was Dave in the last two weeks of August 2023? | D14:1: Dave: Hey Cal, how's it going? Something cool happened since last we talked - I got to go to a car … | Dave: Hey Calvin! Long time no talk! Got some cool news to share - la… |
| 412 | 50 9-31 | tp | T | R | ~91 | ~532 | -0.03 | Where was Dave in the last two weeks of August 2023? | D17:1: Dave: Hey Calvin! Been a while, what's up? I'm tied up with car stuff lately, yesterday I came back… | Dave: Hey Calvin! Long time no talk! Got some cool news to share - la… |
| 413 | 50 9-48 | tp | T | R | ~36 | ~64 | -0.00 | What was Dave doing in the first weekend of October 2023? | D22:1: Dave: Hey Calvin! What’s up? Last Friday I went to the car show. I saw some awesome cars and got to… | Dave: Hey Calvin! Long time no talk! Got some cool news to share - la… |
| 414 | 50 9-57 | tp | T | R | ~63 | ~457 | -0.02 | Which hobby did Dave pick up in October 2023? | D27:2: Dave: Hey Calvin! That's cool that you've been networking with other artists. Nice! I've been getti… | Dave: Hey Calvin, long time no talk! A lot has happened. I've taken u… |
| 415 | 50 9-68 | tp | T | F | 8 | ~134 | 0.45/0.50 | When did Dave find the car he repaired and started sharing in his blog? | D28:20: Dave: I found it last week, and it was in bad shape, but I saw the potential. I spent ages restorin… | Dave: Wow, Calvin, imagining how your music affects others must be in… |
| 416 | 26 0-19 | mh | O | F | 22 | ~135 | 0.37/0.47 | What do Melanie's kids like? | D4:8: Melanie: It was an awesome time, Caroline! We explored nature, roasted marshmallows around the camp… | Caroline: Thanks, Melanie! Your kind words really mean a lot. I'll do… |
| 417 | 26 0-34 | mh | O | F | ~36 | 29 | 0.34/0.48 | What events has Caroline participated in to help children? | D9:2: Caroline: Hey Melanie! That sounds great! Last weekend I joined a mentorship program for LGBTQ yout… | Melanie: Wow, Caroline. We've come so far, but there's more to do. Yo… |
| 418 | 26 0-43 | mh | O | R | ~33 | ~77 | -0.00 | What kind of art does Caroline make? | D11:8: Caroline: That pic is cool! Representing inclusivity and diversity in my art is important to me. I … | Caroline: The room was electric with energy and support! The posters … |
| 419 | 26 0-43 | mh | O | F | ~34 | 26 | 0.35/0.49 | What kind of art does Caroline make? | D9:14: Caroline: Check out my painting for the art show! Hope you like it. [image: a photography of a pain… | Caroline: The room was electric with energy and support! The posters … |
| 420 | 26 0-149 | sh | O | F | ~63 | 26 | 0.35/0.48 | What do Melanie's family give her? | D18:9: Melanie: They're really amazing. Wish I was that resilient too. But they give me the strength to ke… | Melanie: I'll never forget the day my youngest took her first steps. … |
| 421 | 30 1-23 | mh | O | F | 19 | ~49 | 0.39/0.49 | How did Gina promote her clothes store? | D8:4: Gina: Oof, that's tough, Jon. I got some new offers and promotions going on my online store to try … | Gina: Hey Jon! Long time no see! Things have been hectic lately. I ju… |
| 422 | 30 1-45 | sh | O | F | ~98 | 5 | 0.47/0.48 | What is Jon's attitude towards being part of the dance festival? | D1:28: Jon: Yeah, awesome! Glad to be part of it. | Jon: Hey Gina! Congrats on the new fashion piece! Looks like your sto… |
| 423 | 41 2-11 | mh | O | F | ~77 | 3 | 0.48/0.49 | What people has Maria met and helped while volunteering? | D7:5: Maria: Wow John, your enthusiasm for making a better future is inspiring. Making a positive impact … | Maria: I started volunteering here about a year ago after witnessing … |
| 424 | 41 2-30 | mh | O | F | ~31 | 4 | 0.48/0.48 | What shelters does Maria volunteer at? | D2:1: Maria: Hey John, been a few days since we chatted. In the meantime, I donated my old car to a homel… | Maria: Gonna explore more and volunteer at shelters next month. Can't… |
| 425 | 41 2-42 | mh | O | F | ~141 | 10 | 0.44/0.48 | What area was hit by a flood? | D14:21: John: Sure, Maria! Let's work together to make a real difference. Our neighborhood deserves it! I w… | John: I had a similar experience. Last week, there was a power cut in… |
| 426 | 41 2-84 | sh | O | F | ~29 | 1 | 0.33/0.48 | What did Maria participate in last weekend before April 10, 2023? | D10:10: Maria: Last weekend I did something new that had an impact on me. I participated in a 5K charity ru… | Maria: Yeah, last weekend I had a picnic with some friends from churc… |
| 427 | 42 3-55 | mh | O | F | ~128 | 27 | 0.35/0.47 | What is Joanna inspired by? | D26:3: Joanna: Thanks, Nate! The meetings went really well. I felt confident discussing my script and visi… | Nate: Wow Joanna, those drawings are really incredible! What inspired… |
| 428 | 42 3-55 | mh | O | F | ~322 | 8 | 0.45/0.47 | What is Joanna inspired by? | D26:7: Joanna: Yup, I still remember this story from when I was 10. It was about a brave little turtle who… | Nate: Wow Joanna, those drawings are really incredible! What inspired… |
| 429 | 42 3-70 | mh | O | F | 18 | ~37 | 0.39/0.47 | What activities does Nate do with his turtles? | D28:31: Nate: Sounds good. Well I'll make sure I give the turtles a bath before you get here so they're rea… | Nate: It really is, I'm not sure I'll ever understand why watching my… |
| 430 | 42 3-81 | mh | O | R | ~36 | ~68 | -0.01 | What recipes has Joanna made? | D21:17: Joanna: Hey Nate! Here's another recipe I like. It's a delicious dessert made with blueberries, coc… | Joanna: Awesome! I'll bring some of my recipes so we can both share d… |
| 431 | 43 4-4 | mh | O | F | ~62 | 13 | 0.42/0.49 | What books has Tim read? | D6:8: Tim: Thanks! "The Name of the Wind" is great. It's a fantasy novel with a great magician and musici… | Tim: Yes, they are still my favorites - I love how they take me to ot… |
| 432 | 43 4-13 | mh | O | F | 26 | ~38 | 0.35/0.48 | What does Tim do to escape reality? | D3:30: Tim: That's awesome! I don't surf, but reading a great fantasy book helps me escape and feel free. … | Tim: They really fire up my imagination and take me to alternate real… |
| 433 | 43 4-14 | mh | O | F | 13 | ~36 | 0.42/0.48 | What kind of writing does Tim do? | D15:3: Tim: That castle looks amazing! I hope I get to visit it someday. My writing is going well: I'm in … | Tim: I can just imagine the thrill of being in that kind of atmospher… |
| 434 | 43 4-50 | mh | O | F | 3 | ~38 | 0.48/0.50 | Which book was John reading during his recovery from an ankle injury? | D19:20: John: I recently finished rereading "The Alchemist" - it was really inspiring. It made me think aga… | John: Last season, I had a major challenge when I hurt my ankle. It r… |
| 435 | 44 5-2 | mh | O | F | 11 | ~53 | 0.43/0.48 | What kind of indoor activities has Andrew pursued with his girlfriend? | D25:1: Andrew: Hi Audrey! How have you been lately? My girlfriend and I went to this awesome wine tasting … | Andrew: Hey Audrey, how's it going? Since we last talked, a few new t… |
| 436 | 44 5-9 | mh | O | F | 5 | ~53 | 0.47/0.48 | What kind of classes or groups has Audrey joined to take better care of he… | D6:2: Audrey: Hey Andrew! That hike sounds great. Nature is good for the soul, right? My week's been good… | Audrey: Not much has changed since we last talked. I'm busy taking ca… |
| 437 | 44 5-12 | mh | O | F | 19 | ~103 | 0.39/0.48 | What outdoor activities has Andrew done other than hiking in nature? | D14:1: Andrew: Hey, Audrey! I can't wait for the weekend. My girlfriend, Toby and I are going camping. It'… | Andrew: Yeah, rock climbing was awesome - I felt so accomplished reac… |
| 438 | 44 5-15 | mh | O | R | ~139 | ~197 | -0.07 | What is a shared frustration regarding dog ownership for Audrey and Andrew? | D7:8: Andrew: I'm still on the hunt, but it's tough finding a pet-friendly spot in the city. Been checkin… | Audrey: The hats don't bother them, they just put them on for fun and… |
| 439 | 44 5-26 | mh | O | F | 19 | ~102 | 0.39/0.48 | What is the biggest stressor in Andrew's life besides not being able to hi… | D16:1: Andrew: Hey Audrey, hope you're doing good! So I've decided to take a break from work yesterday and… | Andrew: Haven't been to the beach in a while. Miss being outdoors. It… |
| 440 | 44 5-40 | mh | O | F | 6 | ~261 | 0.46/0.50 | What is a good place for dogs to run around freely and meet new friends? | D14:2: Audrey: That's awesome! That must be fun! I just started agility classes with my pups at a dog park… | Audrey: My dogs go crazy for Fetch and Frisbee, and they love to run … |
| 441 | 44 5-42 | mh | O | F | ~56 | 12 | 0.42/0.48 | What technique is Audrey using to discipline her dogs? | D26:5: Audrey: The behaviorist gave me tips on how to handle it and suggested some changes in their routin… | Audrey: Pepper took a bit to get used to her, but now they're always … |
| 442 | 44 5-49 | mh | O | F | 8 | ~65 | 0.45/0.48 | What does Audrey view her pets as? | D15:15: Audrey: Yep, pets are family. It's so sweet to see the connection between them. Here's a photo of m… | Audrey: Yeah, my dogs make me really happy. I love them so much and I… |
| 443 | 44 5-54 | mh | O | R | ~56 | ~413 | -0.03 | What activity do Audrey's dogs like to do in the dog park? | D13:8: Audrey: Thanks! That one is Max, my childhood dog. He had lots of energy and loved a game of fetch.… | Audrey: The dog park is like paradise for them! They love socializing… |
| 444 | 44 5-54 | mh | O | R | ~74 | ~32 | -0.06 | What activity do Audrey's dogs like to do in the dog park? | D27:12: Audrey: You got it! There are lots of ways to keep them happy in the city. Make sure to socialize a… | Audrey: The dog park is like paradise for them! They love socializing… |
| 445 | 44 5-54 | mh | O | F | 14 | ~34 | 0.41/0.48 | What activity do Audrey's dogs like to do in the dog park? | D4:21: Audrey: When I take them out, we usually play fetch with a ball or frisbee. They love chasing it! W… | Audrey: The dog park is like paradise for them! They love socializing… |
| 446 | 47 6-5 | mh | O | F | ~152 | 26 | 0.35/0.46 | What are John and James' favorite games? | D3:11: John: I played my favorite CS:GO game in an intense tournament. It was awesome to see all the skill… | John: Cool, James! What kind of games are you excited to play on it? |
| 447 | 47 6-24 | mh | O | R | ~189 | ~46 | -0.13 | What kind of games has James tried to develop? | D1:4: James: I'm totally into The Witcher 3 right now. The story and atmosphere are amazing. Have you tri… | John: Cool, James! What kind of games are you excited to play on it? |
| 448 | 47 6-51 | mh | O | R | ~132 | ~71 | -0.10 | Which new games did John start play during the course of the conversation … | D5:4: John: I'm playing this new RPG that has a really cool story and world. It's kinda like getting tran… | James: Thanks for the suggestion, John. What games are you currently … |
| 449 | 47 6-51 | mh | O | R | ~60 | ~245 | -0.05 | Which new games did John start play during the course of the conversation … | D8:20: John: Awesome that you have them! I'm currently playing AC Valhalla, it's cool. Are you playing any… | James: Thanks for the suggestion, John. What games are you currently … |
| 450 | 47 6-75 | sh | O | F | 23 | ~54 | 0.24/0.54 | What game was James playing in the online gaming tournament in April 2022? | D4:16: James: I've been playing my favourite game called Apex Legends with my team and it's intense! Check… | John: Hey James! Congrats on winning the online gaming tournament! It… |
| 451 | 48 7-8 | mh | O | F | ~39 | 6 | 0.46/0.49 | What helped Deborah find peace when grieving deaths of her loved ones? | D15:29: Deborah: Nature helps me find peace every day - it's so refreshing! | Deborah: What was it like when you found her? I can imagine the relie… |
| 452 | 48 7-13 | mh | O | F | 7 | ~64 | 0.46/0.49 | What places give Deborah peace? | D4:34: Deborah: That sounds great, Jolene. Nature's calming for sure. Guess it helps us forget the daily c… | Deborah: It's amazing how it can give you peace and calm in times lik… |
| 453 | 48 7-41 | mh | O | R | ~27 | ~168 | --0.01 | What ways do Deborah and Jolene use to enhance their yoga practice? | D28:16: Jolene: I like to create my own serene yoga space with candles and oils for extra chill vibes. Also… | Jolene: My partner and I plan a camping trip to connect with nature a… |
| 454 | 48 7-82 | mh | O | F | 11 | ~109 | 0.43/0.47 | Which locations does Deborah practice her yoga at? | D4:12: Deborah: When things get tough, just take a deep breath and remember why you're doing this. This is… | Deborah: Wow, cool that yoga has been helping you out! Do they also d… |
| 455 | 48 7-82 | mh | O | F | 17 | ~127 | 0.40/0.47 | Which locations does Deborah practice her yoga at? | D6:10: Deborah: I've been blessed to travel to a few places and Bali last year was one of my favs. It was … | Deborah: Wow, cool that yoga has been helping you out! Do they also d… |
| 456 | 48 7-84 | mh | O | F | 6 | ~43 | 0.46/0.73 | What kind of engineering projects has Jolene worked on? | D1:2: Jolene: Hi Deb! Good to meet you! Yeah, my week's been busy. I finished an electrical engineering p… | Jolene: The best part so far has been being able to apply what I lear… |
| 457 | 48 7-85 | mh | O | F | 6 | ~34 | 0.46/0.75 | Which community activities have Deborah and Anna participated in? | D4:12: Deborah: When things get tough, just take a deep breath and remember why you're doing this. This is… | Deborah: Anna also has a pendant that she wears in memory of her moth… |
| 458 | 48 7-107 | sh | O | F | ~37 | 1 | 0.50/0.82 | What does Deborah bring with her whenever she comes to reflect on her mom? | D4:36: Deborah: Do you remember this amulet from her? Whenever I come here, I bring it with me. It's how I… | Deborah: My mom's house had a special bench near the window. She love… |
| 459 | 48 7-140 | sh | O | F | 5 | ~74 | 0.47/0.48 | What is the favorite game Jolene plays with her partner? | D15:10: Jolene: Yeah, we love playing "It takes two" together! It's a fun team-strategy game and it's compe… | Jolene: Yeah, you`re right! What's your favorite game to play with th… |
| 460 | 48 7-158 | sh | O | F | ~72 | 8 | 0.45/0.47 | What did Jolene participate in recently that provided her with a rewarding… | D21:6: Jolene: My goal is to be successful in my field and make a positive impact. I've been studying, att… | Jolene: Hey Deborah! Been a few days since we last talked so I wanted… |
| 461 | 48 7-179 | sh | O | R | ~467 | ~34 | -0.25 | What did Jolene recently play that she described to Deb? | D27:12: Deborah: Having a supportive community definitely helps. We can motivate and encourage each other! … | Jolene: Wow Deb, that's great! I'd love to experience that every day. |
| 462 | 49 8-7 | mh | O | F | 9 | ~55 | 0.44/0.49 | What new hobbies did Sam consider trying? | D2:10: Sam: Thanks, Evan. Like you said, I've been looking for a hobby to stay motivated. I've been thinki… | Evan: No worries, Sam! Super pumped for you! Let's catch up soon and … |
| 463 | 49 8-11 | mh | O | F | 5 | ~97 | 0.47/0.48 | What health issue did Sam face that motivated him to change his lifestyle? | D12:1: Sam: Hey Evan, hope you're doing okay. I wanted to chat about something that's been bothering me la… | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 464 | 49 8-11 | mh | O | R | ~41 | ~110 | -0.02 | What health issue did Sam face that motivated him to change his lifestyle? | D25:1: Sam: Hey Evan, been a few days since we last chatted. Hope you're doing OK. A lot's happened since … | Evan: That must have been a challenging experience, Sam. It's tough w… |
| 465 | 49 8-42 | mh | O | F | ~113 | 1 | 0.50/0.77 | How did Evan get into painting? | D1:15: Sam: Wow, that's impressive! How did you get into watercolor painting? | Sam: Hey Evan, that sounds like a fun and unexpected event! It's alwa… |
| 466 | 49 8-55 | mh | O | F | ~238 | 11 | 0.43/0.47 | What recurring adventure does Evan have with strangers? | D14:2: Evan: Hey Sam, sorry to hear about that. Gastritis can be tough. Taking care of ourselves is import… | Evan: Ready for an adventure? Where will you go? |
| 467 | 49 8-62 | mh | O | F | 23 | ~46 | 0.37/0.50 | What health scares did Sam and Evan experience? | D14:1: Sam: Hey Evan! I've been missing our chats. I had quite the health scare last weekend - ended up in… | Sam: Hey Evan, that does sound like a tough situation. I'm doing my b… |
| 468 | 49 8-90 | sh | O | F | ~186 | 3 | 0.48/0.49 | What new suggestion did Evan give to Sam regarding his soda and candy cons… | D3:5: Evan: Yeah, breaking habits can be tough. Making small changes can have a big impact later on. Have… | Evan: Go for it, Sam! It's tough at first, but you got this. Try flav… |
| 469 | 50 9-6 | mh | O | F | 12 | ~55 | 0.42/0.50 | Which types of cars does Dave like the most? | D3:12: Dave: Last weekend I went to a car show. Classic cars are so charming and the dedication people put… | Calvin: That's awesome, Dave! Pursuing your passion for auto engineer… |
| 470 | 50 9-20 | mh | O | F | 20 | ~45 | 0.38/0.46 | Who inspired Dave's passion for car engineering? | D12:2: Dave: Hey Calvin, I understand the stress of getting a car serviced. Fixing cars is like therapy fo… | Dave: Thanks, Calvin! This is a dream come true for me, as I've alway… |
| 471 | 50 9-20 | mh | O | F | 10 | ~173 | 0.44/0.46 | Who inspired Dave's passion for car engineering? | D12:4: Dave: Yeah, definitely! I have fond memories of working on cars with my dad as a kid. We spent one … | Dave: Thanks, Calvin! This is a dream come true for me, as I've alway… |
| 472 | 50 9-30 | mh | O | F | 25 | ~48 | 0.36/0.47 | What kind of music does Dave listen to? | D10:11: Dave: Nope, never been to Japan but I'm so keen to go one day. I've heard it's full of vibes, good … | Dave: Cool, Calvin! Music really helps me focus and be productive. Wh… |
| 473 | 50 9-97 | sh | O | F | 19 | ~44 | 0.39/0.48 | What does Dave say is important for making his custom cars unique? | D13:11: Dave: Thanks, Calvin! It's all about those small details that make it unique and personalized. | Calvin: That's awesome, Dave! Pursuing your passion for auto engineer… |
| 474 | 50 9-118 | sh | O | F | ~34 | 6 | 0.46/0.49 | What do Calvin and Dave use to reach their goals? | D21:15: Calvin: Agreed, Dave! Progress is what keeps us motivated and pushing for more. Let's never give up… | Dave: Glad to help, Calvin! Eager to see what you do. Keep at it and … |
| 475 | 50 9-120 | sh | O | R | ~43 | ~45 | -0.02 | What does Dave aim to do with his passion for cars? | D22:5: Dave: Thanks Calvin! I've spent a lot of time and effort on it. It's not just a hobby, it's a passi… | Calvin: That's awesome, Dave! Pursuing your passion for auto engineer… |
| 476 | 50 9-124 | sh | O | F | 16 | ~72 | 0.40/0.47 | What activity does Dave find fulfilling, similar to Calvin's passion for m… | D23:11: Dave: Yeah, Calvin! The crowd had such a buzz. Music brings people together in such an amazing way,… | Calvin: Wow Dave, sounds awesome! Music festivals bring so much joy a… |
| 477 | 50 9-152 | sh | O | R | ~201 | ~100 | -0.07 | What new item did Dave buy recently? | D30:5: Dave: That's amazing, Calvin! Music really does bring people together and foster creativity. Glad t… | Calvin: Wow Dave, those headlights look great! What did you do to get… |
| 478 | 50 9-154 | sh | O | F | 7 | ~53 | 0.46/0.77 | What event did Calvin attend in Boston? | D30:2: Calvin: Hey Dave, it's great to hear from you! Can't wait to see your pics. I went to a fancy gala … | Calvin: Yeah, for sure! I'll let you know when I'm in Boston. See you… |
| 479 | 26 0-3 | mh | M | F | 9 | ~188 | 0.44/0.49 | What did Caroline research? | D2:8: Caroline: Researching adoption agencies — it's been a dream to have a family and give a loving home… | Melanie: Wow, that's cool, Caroline! What happened that was so awesom… |
| 480 | 26 0-18 | mh | M | F | 17 | ~225 | 0.40/0.47 | Where has Melanie camped? | D6:16: Melanie: Glad you have support, Caroline! Unconditional love is so important. Here's a pic of my fa… | Melanie: It was one of those moments where I felt tiny and in awe of … |
| 481 | 26 0-37 | mh | M | F | 7 | ~212 | 0.46/0.47 | What did Melanie paint recently? | D8:6: Melanie: We love painting together lately, especially nature-inspired ones. Here's our latest work … | Melanie: Painting landscapes and still life is my favorite! Nature's … |
| 482 | 26 0-51 | mh | M | F | 10 | ~217 | 0.44/0.48 | What has Melanie painted? | D8:6: Melanie: We love painting together lately, especially nature-inspired ones. Here's our latest work … | Melanie: Yeah, I painted that lake sunrise last year! It's special to… |
| 483 | 26 0-60 | mh | M | F | 8 | ~208 | 0.45/0.46 | What instruments does Melanie play? | D2:5: Melanie: Yeah, it's tough. So I'm carving out some me-time each day - running, reading, or playing … | Caroline: Thanks, Melanie! Appreciate it. You play any instruments? |
| 484 | 26 0-150 | sh | M | F | 19 | ~176 | 0.39/0.48 | How did Melanie feel about her family supporting her? | D18:13: Melanie: Thanks, Caroline. They're a real support. Appreciate them a lot. | Caroline: That's awesome, Melanie! How have your family been supporti… |
| 485 | 41 2-30 | mh | M | F | 15 | ~98 | 0.41/0.48 | What shelters does Maria volunteer at? | D17:12: Maria: John, that's such a great idea! It gives the pup a loving home and teaches your kids importa… | Maria: Gonna explore more and volunteer at shelters next month. Can't… |
| 486 | 41 2-37 | mh | M | F | 12 | ~144 | 0.42/0.50 | What events for veterans has John participated in? | D24:1: John: Hey Maria, last week was really eye-opening. I visited a veteran's hospital and met some amaz… | John: I'm really passionate about making sure veterans are supported … |
| 487 | 41 2-107 | sh | M | F | ~233 | ~538 | 0.28/0.32 | What new activity did Maria start recently, as mentioned on 3 June, 2023? | D17:12: Maria: John, that's such a great idea! It gives the pup a loving home and teaches your kids importa… | Maria: Yeah, I did! I tried my hand at surfing for the first time- it… |
| 488 | 42 3-27 | mh | M | F | 15 | ~170 | 0.41/0.48 | What places has Joanna submitted her work to? | D16:1: Joanna: Hey Nate, long time no see! How have you been? I just got done submitting my recent screenp… | Joanna: Yep! I actually just submitted a few more last week! Hoping t… |
| 489 | 42 3-55 | mh | M | F | ~43 | 18 | 0.39/0.47 | What is Joanna inspired by? | D11:11: Joanna: Nature totally inspires me and it's so calming to be surrounded by its beauty. Hiking has o… | Nate: Wow Joanna, those drawings are really incredible! What inspired… |
| 490 | 42 3-58 | mh | M | R | ~314 | ~622 | -0.18 | What things has Nate reccomended to Joanna? | D19:17: Nate: Sure thing! And since your recommending me a book, I thought I should do the same! I'd really… | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 491 | 42 3-58 | mh | M | R | ~521 | ~623 | -0.26 | What things has Nate reccomended to Joanna? | D27:23: Nate: Yep! I'm currently playing this awesome fantasy RPG called "Xeonoblade Chronicles" and it's b… | Joanna: Thanks Nate, your support really means a lot. I put a lot of … |
| 492 | 43 4-4 | mh | M | F | 24 | ~343 | 0.36/0.49 | What books has Tim read? | D26:36: Tim: I'm really excited to watch this new show that's coming out called "The Wheel of Time". It's b… | Tim: Yes, they are still my favorites - I love how they take me to ot… |
| 493 | 43 4-9 | mh | M | F | 20 | ~103 | 0.38/0.48 | Which endorsement deals has John been offered? | D25:2: John: Yo Tim! Great to hear from you. Things have been wild! Last week I got this amazing deal with… | John: Yup, on the court, I'm getting better at my overall game. Money… |
| 494 | 44 5-9 | mh | M | R | ~43 | ~250 | -0.01 | What kind of classes or groups has Audrey joined to take better care of he… | D16:6: Audrey: I took a dog grooming course and learned lots of techniques. Would you like to hear some ti… | Audrey: Not much has changed since we last talked. I'm busy taking ca… |
| 495 | 44 5-14 | mh | M | F | 15 | ~70 | 0.41/0.75 | What is something that Andrew really misses while working in the city? | D9:20: Andrew: Haven't gone through the photos yet. Maybe soon! It was lovely being out in the open, heari… | Andrew: Hey Audrey! What's up? Missed chatting with ya! Check it out,… |
| 496 | 44 5-35 | mh | M | R | ~77 | ~396 | -0.05 | What are the names of Andrew's dogs? | D24:6: Andrew: I named him Buddy because he's my buddy and I hope him and Toby become buddies! | Andrew: Thanks, I think that's what I need to hear. I'll take good ca… |
| 497 | 44 5-42 | mh | M | R | ~78 | ~313 | -0.03 | What technique is Audrey using to discipline her dogs? | D6:4: Audrey: I know right? I saw this workshop flyer at my local pet store. It was a positive reinforcem… | Audrey: Pepper took a bit to get used to her, but now they're always … |
| 498 | 47 6-8 | mh | M | R | ~31 | ~433 | -0.01 | How many pets does James have? | D1:14: James: Max and Daisy. Will be actually cool to build an app for dog walking and pet care. The goal … | James: My pets, computer games, travel and pizza are all that bring m… |
| 499 | 47 6-9 | mh | M | F | 12 | ~365 | 0.42/0.74 | What are the names of James's dogs? | D5:1: James: Hey John! Long time no chat - I adopted a pup from a shelter in Stamford last week and my da… | James: My dogs are like that too - they even make dark days better. D… |
| 500 | 47 6-51 | mh | M | R | ~225 | ~254 | -0.15 | Which new games did John start play during the course of the conversation … | D19:7: John: I'm playing "The Witcher 3"! There's this awesome monster hunter with a cool story, and I'm t… | James: Thanks for the suggestion, John. What games are you currently … |
| 501 | 47 6-54 | mh | M | F | 17 | ~286 | 0.40/0.48 | What kind of programming-related events has John hosted? | D27:1: John: Hey James! How's it going? I had a blast last week when my programmer friends and I organized… | James: Hey John, that sounds awesome! Combining your two loves - gami… |
| 502 | 48 7-1 | mh | M | F | 12 | ~429 | 0.42/0.83 | Which of Deborah`s family and friends have passed away? | D6:4: Deborah: The roses and dahlias bring me peace. I lost a friend last week, so I've been spending tim… | Deborah: That's my old home. I go there now and then for my mom, who … |
| 503 | 48 7-84 | mh | M | F | 2 | ~192 | 0.49/0.73 | What kind of engineering projects has Jolene worked on? | D17:10: Jolene: Working on a cool project now - a prototype that could revolutionize aerial surveillance. C… | Jolene: The best part so far has been being able to apply what I lear… |
| 504 | 49 8-26 | mh | M | R | ~85 | ~258 | -0.05 | What motivates Evan to take care of his health? | D5:13: Evan: I'm motivated by a thirst for adventure on interesting hikes, that's pretty cool! [image: a p… | Evan: That sucks, Sam. It's tough when our health holds us back. I be… |
| 505 | 50 9-5 | mh | M | R | ~81 | ~265 | -0.03 | What are Dave's dreams? | D5:5: Dave: Thanks Calvin! Appreciate the support. I'm gonna keep learning more about auto engineering, m… | Calvin: Go for it, Dave! Chasing your dreams is what life's about. It… |
| 506 | 26 0-4 | mh | P | R | ~189 | ~353 | -0.08 | What is Caroline's identity? | D1:5: Caroline: The transgender stories were so inspiring! I was so happy and thankful for all the suppor… | Melanie: Wow, Caroline. We've come so far, but there's more to do. Yo… |
| 507 | 30 1-60 | sh | P | F | 8 | ~85 | 0.45/0.48 | What does Jon's dance make him? | D9:5: Jon: Yeah, Gina! It's been tough, but I'm living my true self. Dancing makes me so happy, and now I… | Jon: Wow, that's great! What made you combine clothing biz and dance? |
| 508 | 30 1-75 | sh | P | F | 22 | ~297 | 0.37/0.49 | What does Gina say to Jon about the grand opening? | D15:12: Gina: I'll be right by your side, Jon. Let's live it up and make some great memories tomorrow. So e… | Gina: Can't wait for tomorrow's grand opening! |
| 509 | 42 3-122 | sh | P | F | ~93 | ~69 | 0.29/0.32 | What did Nate do for Joanna on 25 May, 2022? | D13:9: Nate: Yep, Joanna. It's great! Looky here, I got this new pup for you! [image: a photo of a stuffed… | Joanna: Awesome! Did you get to know the couple very well? What were … |
| 510 | 42 3-135 | sh | P | R | ~147 | ~409 | -0.08 | What did Joanna plan to do with the recipe Nate promised to share? | D16:11: Joanna: Awesome! I'm going to make it for my family this weekend - can't wait! | Nate: No problem, Joanna! Always happy to share them with you. Sendin… |
| 511 | 42 3-151 | sh | P | R | ~212 | ~381 | -0.10 | How did Nate celebrate winning the international tournament? | D19:9: Nate: I'm taking some time off this weekend to chill with my pets. Anything cool happening with you? | Joanna: Way to go, Nate! Winning the tournament and earning cash is a… |
| 512 | 42 3-173 | sh | P | R | ~58 | ~228 | -0.04 | What encouragement does Nate give to Joanna after her setback? | D24:13: Nate: Bummer, Joanna. Is this the one you sent to a film contest? Rejections suck, but don't forget… | Joanna: Thanks, Nate! Appreciate the encouragement. I won't give up, … |
| 513 | 42 3-195 | sh | P | F | ~210 | 12 | 0.42/0.47 | What does Nate want to do when he goes over to Joanna's place? | D28:29: Nate: Definitely! I'd love to have you over again. Maybe we can watch one of your movies together o… | Nate: Can't wait to see it, Joanna! I'm here to support you. |
| 514 | 48 7-121 | sh | P | F | ~167 | ~227 | 0.27/0.33 | What did Jolene ask Deb to help with on 13 March, 2023? | D9:14: Jolene: I'm having a hard time dealing with my Engineering assignments. It's a lot to manage and I'… | Jolene: Thanks, Deb. Any tips on studying or time management? |
| 515 | 49 8-110 | sh | P | R | ~41 | ~425 | -0.02 | What injury did Evan suffer from in August 2023? | D9:2: Evan: Wow, Sam, great! Glad your new diet/exercise is going well. As for me, I've hit a sore spot l… | Evan: Hey Sam, what's up? It's been a few days since we talked. How h… |
| 516 | 50 9-79 | sh | P | F | 7 | ~489 | 0.46/0.48 | What does Dave do when he feels his creativity is frozen? | D5:11: Dave: If I'm having trouble coming up with ideas, I usually immerse myself in something I love, lik… | Calvin: Yeah Dave, keep doing what you do! Your blog and car mods are… |
| 517 | 50 9-119 | sh | P | F | 20 | ~280 | 0.38/0.87 | What does working on cars represent for Dave? | D22:5: Dave: Thanks Calvin! I've spent a lot of time and effort on it. It's not just a hobby, it's a passi… | Dave: Definitely, working on cars is what I'm passionate about. Doing… |

## Appendix B. Method notes and caveats

- Data: `per_question.jsonl` of the three runs (logged stages: `vector` 30, `lexical` 30, `extra_legs`, `fused` 10, `pool`, `rerank_scores`, `final`, `context`), the dataset `data/locomo10.json` for turn text, sessions and adjacency (the engine's derived sessions equal the dataset sessions here; the replay reproduction confirms it).
- Reproduction checks: fused top-10 recomputed from the logged legs = logged `fused` (1,540 / 1,540, fix and base); context from hits + window = logged `retrieved_turns` (1,540 / 1,540 fix with 2/4, base with 2/2); rr context from min-max rule + floor 0.3 + window 2/2 = logged (1,540 / 1,540).
- CPU replica: Qwen3-Embedding-0.6B fp32, the arm's query instruction, documents "Speaker: text [image: caption]"; top-30 agreement with the logged bf16-GPU run 97.8%, top-1 agreement 1,492 / 1,540. BM25: k1 1.2, b 0.75, lower-cased alphanumeric tokens, OR of all query tokens; top-30 agreement 97.1%, top-1 1,493 / 1,540. Replica baseline: 1,251-1,252 fully covered vs the exact 1,251, i.e. +-3 questions of noise on any delta below about 5.
- Not tested: any option that needs an LLM at read or write time (planner, HyDE, fact mining, list cards, session summaries); the Qwen3-Embedding-4B embedder (snapshot would not load offline); the reader's reaction to larger contexts. Where a number depends on those it is marked ESTIMATE.
- Cause labels are token-overlap heuristics, not human judgement. The N class in particular mixes implicit answers, reply-turns and weak labels; a manual pass over it (action N1) would sharpen both this analysis and the benchmark numbers.
- Gold ids that do not exist in the dataset (8) are dropped; 2 questions have no usable gold. Question ids refer to the run files; open `per_question.jsonl` (fields `gold_turns[].ranks`, `stages`, `top_non_gold`, `context_text`) for the full record of any row.
