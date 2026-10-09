# B2: audit of the "answer not literally in the gold turn" class (cause N)

Date 2026-10-10. Scope: gap B2 in `GAP_REGISTER.md`, section N of `RECALL_GAPS_FORENSIC.md` (options N1 audit, N3 HyDE).
Method: CPU only, read-only on code and runs. Data: `data/locomo10.json`, `per_question.jsonl` of the fixed config
(`qa-full-qs-eq06-fix--memspine`), appendix A of the forensic file. Labels were assigned by hand from the question,
the gold answer, the gold turn and its neighbours in the dataset. Labels are one reader's judgement (no second rater);
treat the W/X boundary as +-5 turns. No corpus text is stored in the errata file.

## 1. What class N contains

112 lost gold turns in 72 distinct questions (all but 2 are category multi-hop or single-hop list/count/aggregation
questions: "how many X", "what activities...", "which ...", "what has Y recommended"). Of the 72 questions, 41 were
answered wrongly by the judge (the forensic file says 35 wrong; this audit counts the judge outcome of
`per_question.jsonl` over every question that has at least one N turn, so the 41 vs 35 gap is a definition difference,
not a data difference). Class N was assigned by a literal-overlap test, so it is a heterogeneous bucket, not one failure mode.

## 2. Task 1: labels for the 112 turns

| label | meaning | turns | share |
|---|---|---|---|
| (a) VALID-IMPLICIT | the turn supports the answer, by implication, one part of an aggregate, or through the image caption | 77 | 69% |
| (b) WEAK-LABEL | loosely related; another turn states the answer (19 turns, 18 with a named better turn) | 19 | 17% |
| (c) WRONG-LABEL | the turn does not support the answer at all | 16 | 14% |
| (d) NEEDS-IMAGE | caption plus text still cannot support the answer | 0 pure | 0% |

Artefacts of the evidence labels (b + c): **35 of 112 turns (31%)**.

NEEDS-IMAGE: none are image-only once the BLIP caption (`[image: ...]`, which the ingest keeps) is counted. Six VALID turns
lean on the caption (e.g. 0-38 D3:14 "waterfall" for family activities, 4-30 D27:36 Eiffel tower for Paris, 8-36 D23:26 fries
covered in gravy for poutine, 9-59 D2:1 "luxury car" for the Ferrari count, which names no make in text or caption).
They are valid-implicit through the caption and would be NEEDS-IMAGE for a text-only system that drops captions.
One spelling artefact: 5-2 D23:1 says "board games", the gold says "boardgames" (the overlap test failed on tokenisation);
this one is literally present and counts as VALID.

### VALID-IMPLICIT, sub-types (77)
- Part of an aggregate (counting or listing): 3-78 and 3-80 (each tournament win is its own turn; gold says nine / seven),
  3-79 (second script, "is that your third one?"), 5-60 (three dogs, each in a different turn), 8-49 (knee, ankle),
  4-17 (six wins), 6-24. About 40 of the 77. These are really multi-hop aggregation problems (gap B1), not label problems.
- Implication needing one inference: 0-7 D3:13 ("tough breakup", "friends since I moved") for "Single"; 0-34 D3:3 (gave a talk)
  for "school speech"; 7-19 D1:8 (pendant given "in Paris") for "times in France"; 9-30 D2:10 (favourite is Aerosmith)
  for "classic rock"; 9-24 D20:1 (fixing the Mustang engine) for "Can Dave work with engines?".
- Reply or dialogue-pair dependence: 3-74 D15:15 "I would definitely recommend it!" (reply to the cork board turn D15:14),
  3-134 D16:10 "I can give it to you tomorrow" (the recipe is named one turn earlier), 7-188 D29:27 "maybe we can try it together",
  3-79 D12:13 "Is that your third one?".
- Negation: 5-56 ("No" cannot be literal), 6-3 ("do both have pets? No").

### WEAK-LABEL (19), with the better turn where named
0-37 D9:17 (finished "another painting like our last one"; sunset is in D8:6 / D1:12);
0-43 D11:12 (the painting is of a woman, "abstract" is in D17:13, which was in context);
1-44 D1:26 (Jon's reply; what Gina said is D1:25);
2-32 D16:2 (Maria's reply; John's hiking photo is D16:1);
2-49 D25:19 (small talk; the cakes are in D25:20, and the gold list has D26:1 which only says "baked");
5-26 and 5-27 D10:16 (cooking as stress relief; work stress is D12:3);
7-82 D2:11 (image reply; "her old home" is D2:13);
7-86 D23:20 (photo post; the quote from a friend is D23:22);
8-11 D24:12 / D24:14 / D24:20 / D25:3 / D6:2 (seeing a doctor, diet, "health"; "weight" is D2:6, D4:1, D20:8);
8-14 D22:12 (Evan shares cookies; ginger snaps are D5:5);
8-64 D7:2 (doctor's appointment; gastritis is D14:1);
9-11 D6:1 (a greeting; the flooding is D6:3, two turns later);
8-7 D21:19 and 8-44 D20:13 (no replacement found, answer lists need other turns).

### WRONG-LABEL (16)
0-48 D12:14 ("I appreciate our friendship" for the pottery question); 2-44 D28:5 (John's job, for Maria's church friends);
3-51 D24:2 (recipe small talk, for "when did Nate get Tilly"); 3-55 D4:6; 3-61 D22:2 (a tournament, for gaming platforms);
3-67 and 3-69 D8:3 (books, for Nate's pets and turtle count; D28:25 "tank big enough for three" is the real line);
4-11 D1:7, D2:14, D3:1 (all basketball, for "sports besides basketball"; the supporting turns D3:25/D3:27 are
already in the gold list); 4-18 D26:36 (Wheel of Time, but the author is not in the gold list); 4-26 D17:1 (greeting);
6-24 D13:7; 8-11 D5:5 (Evan speaking, not Sam); 8-28 D11:7; 9-63 D16:4.

## 3. Task 2: recomputed coverage

Method: remove each WEAK/WRONG turn from the question's gold set, add the named better turn, and test membership in the
logged `retrieved_turns` (the context that the reader saw, neighbours included).

- Turn level: 35 of the 112 "lost" turns are label artefacts. For the 18 WEAK turns with a named better turn, only
  **4** better turns were already in context (0-43 via D17:13, 3-69 via D28:25, 5-26 via D12:3, 8-11 D6:2 via D2:6).
  The other 14 are real misses, but of a different turn than the one that was counted.
- Question level, 72 questions in the class: 21 have only artefact N-turns (8 wrong, 13 right).
  Among the 8 wrong ones, **1** (3-61) has all its remaining gold in context, so its failure is a reader or label
  problem and not retrieval; **7** stay retrieval failures because the true answer turn (D1:25, D16:1, D25:20, D2:13,
  D23:22, D14:1, D3:25) is not in context. So the class-N artefacts remove about 1 question from the "retrieval failure"
  count, not 8; they mainly relabel which turn was lost.
- Net: the N1 audit changes the *turn* denominator by 35 (7% of the 517 lost turns) and the *question* count by at most 1-2.
  It is hygiene, not a lever. The 41 wrong questions have 33 with at least one VALID-IMPLICIT lost turn, so the bulk is real.
- Reader side: two questions (2-49 cakes, 1-44 graceful) are unanswerable from the labelled evidence even with perfect
  retrieval, because the labelled turn does not contain the answer. These are label errors that cost the system a point.

## 4. Task 3: errata additions
`evals/analysis/locomo_errata.json`: 17 entries added (26 -> 43), top-level `updated` set, note extended. Tag
`evidence_label_error` is new (the gold answer is right, a labelled evidence turn does not support it; the correct turn
is in `reason`): 1-44, 2-32, 2-49, 2-44 (borderline), 3-69, 3-67 (borderline), 3-51 (borderline), 4-11, 5-26 (b), 5-27 (b),
7-82, 7-86, 8-14 (b), 8-91 (b), 9-11, 0-48 (b). One `gold_error` (borderline): 8-54 lists "twisted ankle" twice while the
transcript has a twisted knee. No `needs_image` added: no N case is image-only once captions are counted. Entries are
conservative: WRONG-label turns whose correct turn I could not identify (3-55, 3-61, 4-18, 6-24, 8-28, 9-63) are not
listed, since the errata file needs ids.

## 5. Task 4: HyDE design and oracle bound

Setup: Qwen/Qwen3-Embedding-0.6B, fp32, CPU, documents "Speaker: text [image: caption]", query with the arm's
instruction (as in the forensic replica), vector-only rank over all turns of the conversation (no BM25, no fusion with the
logged system). Query = the question. **Oracle hypothesis** = "<speaker named in the question>: <GOLD answer>" (this uses
the gold answer, so it is an upper bound on what an answer-guessing LLM could do, and not a result for a real HyDE).

### 5.1 Oracle results (77 VALID-IMPLICIT turns; top-10 / top-30 / median rank)

| query | top-10 | top-30 | median rank |
|---|---|---|---|
| question (baseline) | 10 | 30 | 42 |
| oracle "Speaker: gold answer" as document | 6 | 16 | 169 |
| same with the retrieval instruction | 8 | 16 | 138 |
| RRF(question, oracle) | 10 | 24 | 55 |

(All 112 turns: question 12 / 41, oracle 6 / 17, RRF 11 / 32.) Reading: **the gold-answer oracle does not help**. The
answer string ("three", "No", "Weight problem", "a list of 6 activities") carries little of the turn's vocabulary, and the
question already mentions the entity. Only 3 VALID turns flip from rank above 30 to rank 10 or better
(3-55 D7:6, 3-74 D19:16, 3-79 D12:13), and 5 turns that the question had in the top-10 fall out under RRF. This is a
negative result for "embed the answer" and agrees with the forensic estimate that most of class N is not a pure
vector-similarity problem. A *question + answer* concatenation was not tested.

### 5.2 What an LLM hypothesis would look like (hand-written, a leaky upper bound)
The better design is a one-sentence hypothetical *turn*, not the answer. I wrote six by hand for VALID-IMPLICIT cases
(the writer knew the corpus, so they are optimistic; a model without the corpus would hit fewer):

| question | hypothetical turn | gold-turn ranks, question -> hypothesis |
|---|---|---|
| 5-60 How many dogs does Andrew have? | "Andrew: I just adopted another puppy from a shelter, so now I have more dogs at home." | D12:1 31->8, D24:2 16->1, D28:6 29->4 |
| 3-79 How many screenplays has Joanna written? | "Joanna: I finished my second script, and now I have written a third one." | D5:1 12->8, D12:13 64->17, D12:14 137->278 |
| 5-2 Indoor activities with his girlfriend | "Andrew: My girlfriend and I played board games and went to a wine tasting, and we volunteered at a pet shelter." | D23:1 30->2, D25:1 9->3, D13:1 17->1 |
| 4-18 Which authors has Tim read | "Tim: I love reading fantasy like Harry Potter, Game of Thrones and Patrick Rothfuss." | D1:14 46->26, D4:7 63->4, D5:15 19->3 |
| 3-62 How many letters has Joanna received? | "Joanna: I got a letter in the mail, someone wrote to me." | D14:1 18->32, D18:5 5->2 |
| 0-7 Caroline's relationship status | "Caroline: I am single since my breakup, and would raise a child as a single parent." | D3:13 24->57, D2:14 84->1 |

Of 16 gold turns, the question puts 2 in the top-10 and 10 in the top-30; the hypothetical turn puts 11 and 13 there.
So a hypothesis written as a turn in the speaker's voice works for aggregate and implicit questions, while an
answer-string hypothesis does not. The risk is the one in the register: a hallucinated hypothesis ("three screenplays")
retrieves confident noise, and D14:1 / D12:14 show cases where it moves the gold turn away.

### 5.3 Estimate
Of the 41 wrong questions in class N, about 33 have a VALID lost turn. Their failures are mostly multi-hop (a list needs
3-9 turns, and a hypothesis recovers some but not all). If a real LLM hypothesis recovers at half the leaky rate above,
and gains need every needed turn, my estimate is +3 to +8 questions, below the register's +5 to +15. This is an
ESTIMATE; the oracle runs do not measure a model.

## 6. Recommendation
1. N1: treat as hygiene. Report accuracy with and without the 43 errata entries and drop the 35 artefact turns from the
   cause table (turn counts only); it will not move accuracy.
2. N3: do not build a gold-answer-style or answer-string HyDE (oracle bound is below the baseline). If built, the
   hypothesis must be a first-person *turn-shaped* sentence, run as a third RRF leg and capped by the existing pool, and
   only for list/count/"what did X recommend" questions. Gate by the B1 list-question work, which targets the same 40 aggregates.
3. Prefer B1 (list/aggregation decomposition) over N3 for the VALID aggregates: about 40 of the 77 valid turns belong to
   count or list questions where each item sits in a separate turn.
4. Re-score the label artefacts at the next full run: 1-44, 2-49, 7-82, 7-86, 9-11 are unanswerable or mislabelled
   regardless of the engine.

## Files
- `evals/analysis/locomo_errata.json` (43 entries, `updated` 2026-10-10)
- Scratch scripts (not in repo): the labels, ranks and hypotheses are reproducible from the tables above.
