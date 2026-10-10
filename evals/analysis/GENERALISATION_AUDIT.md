# Generalisation audit: where the gaps come from, what is LoCoMo-specific, what we have never measured (2026-10-10)

Scope: read-and-analysis pass over `GAP_REGISTER.md`, `RETRIEVAL_GAPS.md`, `READER_GAPS.md`, `READER_GAPS_FORENSIC.md`,
`RECALL_GAPS_FORENSIC.md`, `DEV_GAP_REASONING_2026-10-10.md`, `B1_LIST_QUESTIONS_DESIGN.md`, `B2_EVIDENCE_AUDIT.md`,
`ENGINEERING_GAPS.md`, `docs/PIPELINE_TREE.md`, `docs/USAGE.md`, `evals/arms/BEST_dev_2026-10-10.json`, the read path
(`engine.py`, `core/query_shape.py`, `core/temporal_query.py`, `core/temporal_resolve.py`), `readers.py`, `judge.py`,
`date_check.py`, `refusal.py` and the dataset loaders. No engine code changed, no run started, no held-out conversation
(conv-43/44/47/48/49/50) opened.

**The user requirement behind this document:** an enhancement is accepted only if it generalises across all memory
benchmarks, not only LoCoMo. Every number in the gap docs was measured on LoCoMo categories 1-4 (1,540 questions, ten
conversations, one reader/judge model, one embedder). Nothing in them was measured on BEAM, ConvoMem,
PrefEval, PersonaBench, LaMP, MemoryAgentBench, HaluMem or LoCoMo category 5 with the current stack; I found no such
number in the docs read, and I state "unmeasured" below rather than guess.

Citation keys: [GR] GAP_REGISTER, [RF] RECALL_GAPS_FORENSIC, [DF] READER_GAPS_FORENSIC, [DR] DEV_GAP_REASONING,
[B1] B1_LIST_QUESTIONS_DESIGN, [B2] B2_EVIDENCE_AUDIT, [EG] ENGINEERING_GAPS, [PT] PIPELINE_TREE. "Dev" = conv-26/30/41/42
(584 questions of category 1-4; the 233-question screen is conv-26/30). Estimates that are mine, not measured, are marked EST.

---------------------------------------------------------------------------------------------------

## 1. Where and why gaps appeared: stage-by-stage origin map

Order of the pipeline: dataset/gold -> write/ingest -> embedding -> lexical/BM25 -> extra legs -> fusion -> rerank ->
assembly/window -> reader prompt -> reader model -> judge. A gap is listed at the stage where it **originates**; the last
column says how it travels. Funnel for the reference run (2,348 gold turns, [RF] section 0): 1,404 are fused hits, 427
reach the context only through a neighbour window, 517 never reach it (280 lost at recall, 237 lost at fusion). Of the
1,540 answers, 148 have all gold in context and are still wrong; about 48 of those 148 are not reader errors ([DF] section 0).

| Stage | Gap ids | Root cause (one line) | How it cascades downstream | Evidence |
|---|---|---|---|---|
| **0. Dataset / gold labels** | A5, B2 (35 label artefacts), R2-5, stable failures 0-5, 0-23, 1-9, 1-44, 0-70, 1-43, 0-151, 1-48 | Gold answers or evidence turn ids are wrong, point at the question turn instead of the answer turn, or rest on a photo | Looks like a retrieval loss (gold turn "never retrieved") and then like a reader loss (right answer judged WRONG); inflates both stage counts and caps the ceiling at about 96.5-97.5% | [DF] section 5: 21 gold errors + 3 photo-only; [B2] 31% of the 112 class-N turns are label artefacts (77 valid-implicit, 19 weak, 16 wrong); errata 47 entries [GR A5] |
| **1. Write / ingest** | F1-F5, B2 (reply-pair), N4, ENGINEERING INJ-2/3/5 | One whole turn = one record = one vector. Reply turns ("I would definitely recommend it!") carry no content words; the speaker name is stored as text in every turn; a session shares one stamp, so intra-session order is write order only; quarantine was invisible | Anaphoric turns cannot be matched by embedding or BM25, so the evidence is only reachable through the window of another hit; a quarantined turn looks exactly like a recall miss | [RF] 2.2: short backchannel/anaphoric turns lose 36% (lift 1.62, 59 turns); [B2] reply/dialogue-pair dependence; [PT] section 1 (`text = "Name: text"`, role `user` for every turn); F1: 5,882/5,882 written, clean; reply-aware indexing measured -11..+2 [GR B2] |
| **2. Embedding** | B1 (H: 164 turns), B2/N (112), B3/I (96), B2 N5 | A question names a category ("hobbies", "activities"); the evidence turn names an instance ("kayaking"); one 0.6B embedding does not bridge category to instance or implication | Lost at recall: 280 turns in no leg's top-30. Those turns sit in sessions with no hit (75%), so no window can recover them; the reader then sees 1 of 4 list items and either answers partially (credited by the lenient judge, see 9) or wrongly | [RF] 2.2: H 164 turns (52% lost, all multi-hop), I 96, N 112; 68% of lost turns share no stemmed content word with the question; 388 of 517 lost turns (75%) sit in a session with no hit [RF] section 5; Qwen3-Embedding-4B unlikely, median vector rank 127 [GR B2] |
| **3. Lexical / BM25** | B5, B6, B3, B14 | Tantivy default analyzer: OR of every query token, no stemming, no stop words; the speaker name occurs in nearly every turn | BM25 top-10 is 34% noise; consensus of two noisy legs outranks a strong vector-only hit, which feeds the fusion loss (stage 5). BM25 alone finds only 4.9% of gold turns that the vector leg misses | [RF] section 3: 34% of BM25's own top-10 share no content word; BM25 top-30 holds 53% of gold vs vector 75%; english analyzer +7/-8 and strip-names +3/-6, both within noise [GR B5, B6] |
| **4. Extra legs** | B4, B8, B15, B16, R2-1, R2-1b, R2-2, R2-3 | The legs add recall only where a cue exists: absolute dates in the question (187 of 1,540), one named speaker (list mode), a bridge phrase (about 1 in 80). The rest dilute fusion | A leg that fires on every question (session leg, bridge hop) adds distractors and cost (bridge 3x search time) and moves answers on questions that needed nothing | Temporal leg fires on 187/1,540 [GR B4]; session leg fires on 233/233, +1/-2 [GR B8]; word vector leg -4 to -7 net [GR B15]; bridge hop fires 233/233, +5/-7, 3x search [GR R2-1]; list-mode speaker vote adopted, +7 net on 584 dev q [GR B1] |
| **5. Fusion (RRF, cut to top-10)** | B7, B1, X2 | RRF over a vector leg and a noisy BM25 leg is rank-based; an item present in both legs at ranks 5/5 beats a vector rank 3 present in one leg. Then the fused list is cut to 10 | 237 gold turns are in a leg's top-30 but cut at 10; 195 are vector-only, 37 BM25-only, 1 in both. Re-weighting, rrf_k and reserved slots only trade one question's gold for another's | [RF] section 3: 237 lost at fusion; rrf_k 10/30/100 and weights net -16..-136 or 0; only a larger pool helps (pool 30 + tiered window +90 fully covered q) |
| **6. Rerank** | B9, B10, B11, C1 (indirect) | With pool 1 the reranker sees only the fused top-10 and can only delete; min-max normalisation plus `relative_floor 0.3` removed about half of the hits together with their windows | rr run 70.8% vs 74.5%: reranker kept 97.4% of gold hits but 74% of the gold it lost were window neighbours of a dropped hit. After the fix (pool 2, keep 10, floor skip) coverage is best so far but accuracy gain is inside noise | [RF] section 6; [GR B9]: 84.5% vs 84.1% (+12/-11), all gold in context 83.1 -> 86.6% |
| **7. Assembly / window / budget** | B12, B7, B8, D1, D2, C9, DIST | The window (2 before / 4 after, in turns) adds 427 gold turns but also +32% context; a bigger context gives the reader distractors and shorter-answer pressure. Token counters disagreed (up to 1.25x) | 49 answers that were correct before the fix are now wrong (lists, detail, distractors); 9 of them are "a later line about the same topic". The window is saturated: 2/6 buys +8 questions for +450 tokens | [RF] section 5 (window 2/4 +55 gold turns; saturated); [DF] 2.3: 49 regressions, DIST 9 of them from 53 lines vs 39; [GR D1]: top_k 20 overflowed -> 48.7% |
| **8. Reader prompt** | C1-C3, C5-C8, C10, REF, DIST, DET, LIST, IMG | The baseline prompt's blanket "say you do not know" produced refusals with gold present (236 refusals); the grounded prompt fixed that but now commits to an answer (shorter, 32.4 -> 18.1 words) | Refusals fell 236 -> 30 (+154 gains) at the price of 67 losses: lists, detail, captions, hypotheticals answered "No". Every later prompt variant landed within noise or below (grounded_v2 +5/-17, v3 +3/-5) | [DF] 0 and 2: 154 gains / 67 losses; retry fired 107 times, 31 end correct (29%); [GR C3, C8] |
| **9. Reader model (9B Q4)** | C4, C11, A9, INF 23, DARITH 13, IMG 8, HOP 4 | World-knowledge hops, two-event date arithmetic and "would X" inference are beyond the 9B reader; ceiling about 88% even with all gold | Retrieval gains convert at about 0.8 (0.82 conditional accuracy) and only 25% for open-domain with all gold present; thinking mode does not help (63.2% vs 88.8%, 43 answers hit the 4,096-token reasoning budget) | [RF] section 0 (reader accuracy given complete gold 82.6%; open-domain 25%); [GR A9, C11]; [DF] 1: INF 23, DARITH 13 |
| **10. Judge** | A1-A4, A7, A8, A10, A16, JFN 24 | Reader and judge are the same 9B model, with a home-made rubric tuned on LoCoMo failures; no context seen by the judge | False negatives 24-50 (7 of 20 exact resolved dates rejected), false positives 45-60 (partial lists credited); they cancel in the headline (net 0 to +2 points overstated) but not per category. 45 of 233 dev questions flip across configurations, so deltas within about +-5 are noise | [DF] 4.1-4.4; [GR A16]: 171 always right, 17 always wrong, 45 flip |

**Cascade pattern in one sentence.** The three biggest losses are all "evidence is one hop from the question": category-to-
instance lists (stage 2, 164 turns), implicit or paraphrased answers (stage 2/3, 112 turns) and open-domain inference (stage
2, 96 turns); each of them is made worse downstream by a rank-based fusion that cannot rescue vector-only hits (stage 5),
a window that cannot reach sessions with no hit (stage 7), a reader that can use only a fraction of what it is given
(stage 9) and a judge that credits partial lists and rejects exact dates (stage 10). The fixes adopted so far
(window 2/4, fixed reranker with pool 2, list mode, date check) each repair one stage and all were screened on the same
two to four LoCoMo conversations, which is the root of the generalisation risk in section 2.

---------------------------------------------------------------------------------------------------

## 2. Overfitting audit

Columns: **assumption** = what the feature takes for granted about LoCoMo; **user-assistant chat / cat 5 / third** = expected behaviour on
user-assistant chat data (BEAM, HaluMem; first-person questions, `user:`/`assistant:` text), LoCoMo category 5 adversarial, and a third benchmark of a different shape (BEAM long contexts, PrefEval
preferences, ConvoMem without timestamps). Expectations are from reading the code and the loaders; none was measured.
Risk: H / M / L. "In best" = in `BEST_dev_2026-10-10.json` or in the fixed run flags named in [GR] section 0 (grounded
prompt, refusal retry, judge guards, date check).

### 2.1 Engine read path

| Feature / key | Status | LoCoMo-specific assumption | Expected on user-assistant chat / cat 5 / third benchmark | Risk | How to make it general |
|---|---|---|---|---|---|
| `read.list_mode` + `list_trigger=set_question` (`is_set_question`) | adopted in best | English regex whose set-noun list (activities, hobbies, books, ...) and verb list were written from LoCoMo questions ("What activities does Melanie partake in?"); precision 40% against the list set, fires on 304/1,540 (20%), 183 of them outside the list set [B1 sections 1, 4]; the B1 doc itself notes the regex and lexicon were written after reading all ten conversations, so the dev/held-out split was not blind | Question-text evidence (no run): on LongMemEval questions the trigger fires on 7/500 (0/133 multi-session) against 93/584 on LoCoMo dev, misses "How many tops have I bought from H&M so far?" and false-fires on the single-answer "What type of camera lens did I purchase most recently?" - the vocabulary is LoCoMo's. Where it does fire, the pool widening (x3 candidates) and single-turn tier apply with the speaker vote inert; cat 5: "What did Caroline research?" fires the trigger and widens the pool with distractors for a question whose right answer is a refusal; BEAM/PrefEval: unmeasured | H | Replace the regex by a learned or embedding-based shape classifier calibrated per benchmark; log fired-rate per slice (see I4); keep the trigger conservative by default; make the tier (10 full windows + singles) a function of turn length |
| `speaker_vector_leg` / `comparison_speaker_legs` (speaker vote, R2-2) | adopted (vote); R2-2 inert on dev | Exactly one or two named people; speaker read from a capitalised `Name:` text prefix (`_SPEAKER_LINE` needs `[A-Z]`) or a `speaker:` tag that needs `subject_tagging`; assumes 96% of gold is spoken by the named person [B1 section 1] | BEAM/HaluMem text is `user: ...` / `assistant: ...` (loader sets speaker = role): the prefix is lowercase, the vote returns nothing, list mode degrades to pool + tier. Neutral, not harmful, but the benefit is not transferred. Cat 5 wrongly attributes an event to the other person: the vote would pull that person's turns, which is the distractor | M | Speaker as metadata (`spk`/`role`) set by the adapter, not parsed from text; vote by role for 1:1 chats, by NER entity for third parties; N speakers, not 1-2 |
| `replay_window_before/after = 2/4` | adopted | LoCoMo turns are short and the answer follows the question; the window is counted in turns, not tokens; the reply follows the question turn by 1-4 turns [RF] section 5 | Assistant turns in chat data are long, so the 4,096-token budget binds and neighbours that do not fit are skipped (engine: "a neighbour that does not fit is skipped"); PrefEval/ConvoMem filler turns are noise inside the window; BEAM 100K+ histories need a different budget | M-H | Radius in tokens, or by shape (the window produced 49 of 154 gains and 9 distractor regressions, so it must be per shape, [DF] 2.3); test symmetric 1/1 as the dialogue-agnostic default |
| `candidate_pool=2`, `rerank_keep=10`, `rerank_floor=skip`, Qwen3-Reranker-4B 4-bit | adopted (coverage, not accuracy) | Tuned on 2-4 conversations; accuracy +12/-11 inside the band; doc length is a LoCoMo turn (about 25 words) | Long assistant turns: document truncation in the reranker is unmeasured; pool 20 is a fixed 20 documents whether the haystack has 600 turns or 50,000; latency and 4-bit quality at scale unmeasured | M | Retrieval-only screens on BEAM/ConvoMem/PrefEval with rerank on/off before QA; set rerank max length; pool as a function of haystack size |
| `embedding.query_instruction` ("question about a user's past conversations") | adopted | Questions about past conversations | Fits chat questions about the past; PrefEval/LaMP queries are requests or movie descriptions, not questions about the past | L-M | Instruction per data shape; test no instruction |
| BM25 default analyzer, OR of all tokens (`lexical_analyzer`, `lexical_strip_names`) | english analyzer neutral, strip-names rejected | The "34% noise" and the speaker-name effect come from two named speakers whose names open every turn [RF] section 3 | Chat data (BEAM) has no name tokens and long keyword-rich turns: the verdict "neutral/rejected" may flip; stop-word noise differs | L (default) / M (the rejection) | Do not treat the rejected options as globally rejected; re-screen retrieval-only on each benchmark |
| `resolve_relative_dates` + `relative_dates_anchored` ("the week before <d>") | adopted | `temporal_resolve` docstring: "LoCoMo's convention" for week-level golds; anchor = the session stamp; English phrases only | Chat benchmarks anchor relative questions on a question time or a session anchor, not LoCoMo's wording; the annotation is harmless but costs tokens. ConvoMem/PrefEval/LaMP have no timestamps: per [PT] section 1 and `parse_turn_stamp`, a missing stamp becomes `valid_from=now`, so the reader would see `[2026-10-10]` on every line and `[= ...]` resolved against today (EST: not run on a no-timestamp dataset, verify) | M-H | A `has_timestamps` flag per item: no date prefix and no annotation when false; `anchored` only for LoCoMo-style golds |
| `temporal_leg` (base.yaml) + `temporal_leg_mentions`; `temporal_relative`, `temporal_infer_year`, `lexical_dates` | leg adopted; relative/infer-year neutral, lexical_dates rejected (-3.1 temporal) | Fires only for absolute dates in the question (187/1,540); "rejected/neutral" verdicts rest on 11 vs 7 dev questions [GR B4] | BEAM temporal-reasoning probes ask relative to a time anchor; the leg is off for them and the only relative-date test was on LoCoMo where questions rarely contain relative phrases | M | Anchor = question time; re-test `temporal_relative` on BEAM temporal-reasoning before declaring it neutral |
| `order_by_time_for_ordering` (`is_ordering`) | adopted | English first/last/earliest regex | Fits event-order questions in any dialogue data; low risk | L | Keep; add per-benchmark ordering metric |
| `query_shape` regexes (`is_temporal`, `is_count`, `is_aggregation`, `is_inference`, `is_personal`, `is_novelty`, `is_duration`, `rule_read_mode`, `question_shape`) | used by routed prompt, per-shape weights, list trigger (candidates) | English, written against LoCoMo and example wording; `is_inference` fires on "could/might/would" anywhere before `?` | "Could you recommend ..." (PrefEval, assistant-directed questions) is routed to the inference variant; non-English inputs match nothing | M | Learned shape classifier; per-benchmark calibration of the precision of each rule |
| Bridge hop gate (`bridge_hop_gate`: `cue`, `weak`, `cue_or_weak`; `has_bridge_cue`) | candidate, screen pending | Family-relation and place-noun vocabulary (`_CUE_REL`, `_CUE_THING`), threshold 0.4 on the Qwen3 reranker's raw P(yes), both tuned on one sample (cue 7/584, weak 31/233, wins 2/5 and 1/5) [GR R2-1b] | `weak` is model-specific and its distribution on long contexts is unknown; `cue` fires about 1 in 80 on LoCoMo, unknown elsewhere; 3x search cost when it fires | H (if adopted) | Calibrate the threshold as a quantile of the benchmark's own score distribution; measure cue precision on every benchmark first; keep opt-in |
| `list_trigger=set_question_wide` (`_HOW_MULTI`, `_PLURAL_HEAD`) | neutral on dev, opt-in | Verb list (promote, celebrate, decorate, ...) and plural-head rule built from LoCoMo wording | Fires on any plural head noun in any benchmark ("What gifts did X buy?"); precision unmeasured outside LoCoMo | H (if adopted) | Same as the base trigger; do not adopt without a cross-benchmark fired-rate report |
| `session_leg`, `word_vector_leg`, `rerank_balanced`, `hits_first`/`hit_blocks`, `mark_hits` | rejected or neutral | Verdicts on conversations with about 27 sessions and about 590 turns each (5,882 turns / 10 conversations, [GR F1]) | A session-level leg may matter at many sessions (BEAM); word vectors may help paraphrase-heavy data. Rejections are "rejected on LoCoMo" only | L (default off) | Record the verdicts as per-benchmark; re-screen retrieval-only where sessions are many |
| Firewall default-on (embedding outlier, 96-char prefix repeat) | default | LoCoMo has no repeated boilerplate; F2 now logs `n_quarantined` | Repetitive assistant openings in BEAM chat may quarantine real evidence; unmeasured | M | Assert `n_quarantined / n_turns` per benchmark in the summary; fail the run above a threshold |
| Adapter writes `role="user"` for every turn [PT section 1] | harness | LoCoMo has two peers, no assistant | Role-aware features (`role_aware`, assistant leg, recommendation tag, trust matrix, `spk/sub/ask` axes) cannot work on assistant-information questions in BEAM/HaluMem or on PrefEval; they are not "tested and neutral", they are untestable through this adapter | M | Pass the dataset role to `write_messages`; add a role-aware retrieval-only screen |
| Neighbour sessions derived by a 30-minute gap (`SESSION_GAP_MINUTES`) | engine | LoCoMo sessions are days apart and share one stamp | BEAM carries a day-level anchor forward, so adjacent sessions on one day merge into one derived session; no-timestamp data becomes one session | L-M | Use the adapter's `session_id`/`group_id` for windows |
| Budget 4,096 tokens, top_k 10, reader window 8,192/16,384 | harness | Chosen after top_k 20 overflowed to 48.7% [GR D1] | BEAM haystacks need different budgets; per-benchmark budget must be recorded | L | Budget in the shape profile |

### 2.2 Reader prompts and reader behaviour

| Feature / key | Status | LoCoMo-specific assumption | Expected on user-assistant chat / cat 5 / third benchmark | Risk | How to make it general |
|---|---|---|---|---|---|
| `grounded` QA prompt | adopted | Worded around LoCoMo: "[YYYY-MM-DD] is the date it was said", answer dates "in the wording the memories use (for example 'the week before 9 June 2023')"; "say it is not mentioned only when nothing in the memories bears on the question" | The answer date format is not other benchmarks', there is no `question_date` in this prompt (the `question_dated` variant has it), and no "latest statement wins" rule (only in `dated_world`), so BEAM knowledge-update and contradiction probes depend on the reader's luck. Cat 5 and BEAM abstention probes: the "best-supported answer, even if indirect" clause pushes toward an answer where a refusal is correct | H | Compose the prompt from capability clauses (dates present, question_date present, conflicts possible, abstention allowed) declared by the adapter; keep one base prompt; measure abstention as a guard slice |
| `--retry-refusal` (`RETRY_INSTRUCTION`) | adopted in the fixed run | The retry tells the reader "the memories above do contain information relevant to this question, so do not reply that it is unknown" - true for every non-adversarial LoCoMo question, false by construction on cat 5 and BEAM abstention. [DF] 3.2 itself says it is "only a risk for a metric that rewards abstention (cat 5 is not in these 1,540)" | It converts a correct refusal into a fabrication. Fired on 107 of 1,540 (6.9%), open-domain 44%; 31 end correct [DF] 3.1. On an abstention slice the expected effect is negative; unmeasured | H | Retry only when retrieval confidence is high (reranker top score above a per-benchmark quantile) or the question is not abstention-eligible; make a no-retry run the default for any dataset with abstention; report cat-5 delta with retry on/off |
| `is_refusal` regex (`REFUSAL`, `DENIAL`) | adopted (drives retry and the date check) | "there is no", "does not mention", capitalised-name "X did not" opening | "There is no change in ..." is a valid answer elsewhere and is classed as a refusal; "I did not" matches `DENIAL` (capital I) | M | Classifier or short LLM check with the question; unit tests on non-LoCoMo answers |
| `ABSTAIN_QA_PROMPT`, `abstention-v1` judge | exists, reported apart (A13) | Cat 5 gold "Not mentioned in the conversation" | Never run with the best config (no cat-5 number in any doc read) | H (coverage) | See section 3 and I1/I2 |
| Reader = Qwen3.5-9B Q4, thinking off | adopted | Reader capability ceiling ~88% with all gold present | Other benchmarks (BEAM, ConvoMem) publish stronger readers: scores not comparable; style shortening interacts with the judge | M | Always report the reader id with the number; compare only paired deltas on the same reader |

### 2.3 Judge side

| Feature / key | Status | LoCoMo-specific assumption | Where it can inflate one benchmark or hurt abstention | Risk | How to make it general |
|---|---|---|---|---|---|
| `--judge-date-check` (`date_check.date_equivalent`) | adopted | Gold "the Friday before 15 July 2023" resolves to one day; credits an answer whose FIRST date equals it; refusals and negations never credited. Dev 85.0 -> 86.7, full 80.1 -> 80.6 [GR A2] | Inflates LoCoMo only (other benchmarks have few relative-day golds). It cannot reward an abstention (refusal/negation excluded) so it does not hurt cat 5. "First date in the answer" can credit an answer that cites two dates when the question asks about the second. The parser is imported from `evals/failure_buckets.py` and silently returns False when `evals/` is not on `sys.path` (`_parser()`), i.e. the check can switch itself off, the same import-isolation problem as H6 | M | Report every benchmark with and without it; move `parse_interval` into the package; log how many verdicts it flipped per benchmark |
| `--judge-guards`: empty-answer guard and `RUBRIC_GUARDED_BINARY_PROMPT` | adopted | Prompt contains LoCoMo date examples and accepts "hedging or extra detail as long as it does not contradict the gold fact"; WRONG for "does not know" unless gold says unavailable | Lenient on lists and hedges on every benchmark (65 of 177 list questions credited with items missing, mean list recall 0.63 [GR A3]); strict benchmarks (alias substring in MemoryAgentBench, nugget-style) will score the same answers lower. For abstention gold the rule works only if the gold string states unavailability | M | Use the benchmark's official judge/prompt for published comparisons (`--judge-prompt mem0-official`); rubric only for paired deltas |
| Judge = reader model (A1 deferred) | open | Shared blind spots on date arithmetic and long lists | A change in answer length or style can move the verdict without moving correctness; the effect differs per benchmark | H | Gate adoption on deterministic metrics (R@k, coverage, alias match) first; second judge on disputed rows (A1) before any QA-only claim |
| Rubric prompt tuned on LoCoMo failures (A4) | open | Written on LoCoMo failures | Calibration set was 8 cases (8/8) on a pilot; no kappa on any other benchmark | H | Hand-label 150 rows per benchmark slice; report kappa |
| Errata file (47 entries) and lenient list convention | adopted for reporting | LoCoMo only | PrefEval gold mapping is verbatim for only 92 of 1,000 rows, MemoryAgentBench carries `ambiguous`/`unmapped` gold; no gold-quality audit exists for the other loaders | M | `gold_quality` tag and errata per benchmark; report with and without |
| Cat 5 excluded from the headline (A13) | by design | Mem0 convention | All adopted features were selected without any abstention signal | H (structural) | A cat-5 slice is a guard in the adoption rule (section 4) |

### 2.4 The ten highest-risk items (for quick reference)

1. Refusal retry (`RETRY_INSTRUCTION` asserts the evidence exists) vs every abstention slice.
2. Grounded prompt: LoCoMo date wording, "best-supported answer even if indirect", no latest-wins or question-date clause.
3. List-mode trigger: LoCoMo-vocabulary English regex, 40% precision, fires on 20% of questions.
4. Cat 5 never measured under any adopted configuration.
5. Judge = reader (A1 deferred) plus a rubric tuned on LoCoMo failures: style changes can be rewarded by the judge.
6. Speaker identity parsed from a capitalised `Name:` text prefix and `role=user` on every turn (speaker vote inert, role-aware features untestable on chat benchmarks).
7. Window 2/4 counted in turns, budget 4,096 tokens (long assistant turns, long histories).
8. Relative-date anchoring and `[date]` prefix for data with no or different timestamps (ConvoMem/PrefEval/LaMP get `valid_from=now`).
9. Bridge-hop gate and `set_question_wide`: vocabulary and thresholds tuned on one sample; opt-in, must not be adopted unmeasured.
10. Date check and lenient list credit inflating LoCoMo comparisons (and the date-check import that can disable itself).

---------------------------------------------------------------------------------------------------

## 3. Category and scenario coverage gaps

What the harness can already measure (from the gap docs and loaders): LoCoMo categories 1-4 accuracy with cluster-bootstrap
CI, `list_recall`, errata-adjusted accuracy, coverage/sufficiency, `recall_at_10_hits`, `n_adversarial`/`adversarial` block
(A13), `rerank_check`, ctx guard, ingest audit; loaders for BEAM (ten abilities, retrieval-only, `R_all@k` for two-statement abilities, `stale_turn_ids`), ConvoMem, PrefEval,
PersonaBench, LaMP (kNN vote proxy), MemoryAgentBench (supersession-order metric), HaluMem (QA only), LoCoMo-Plus.

| Question type / scenario | Never measured with the current stack | Missing harness metric |
|---|---|---|
| **LoCoMo category 5 (446 q; 173 dev, 273 held-out)** | No number under any adopted config; A13 only separates it from the headline [GR A13], [EG] EVAL-6 "abstention behaviour is not measured at all in the headline" | Refusal rate on unanswerable (recall of abstention), false-refusal rate on answerable (over-refusal), a combined score; per-feature cat-5 delta paired with the cat 1-4 delta; wrong-person detection rate |
| **Knowledge update / contradictions** | The harness writes every turn as `episodic` through `write_messages`, so the supersede/conflict ladder (semantic path) never runs [PT section 1]; whether the right (latest) value reaches the reader is untested outside the MemoryAgentBench `mab_fc` prompt | A stale-versus-current metric for every benchmark: rate at which an old value ranks above the new one (exists only as `mab_gold.supersession_order_rate`; BEAM keeps `stale_turn_ids` but no metric uses them in the QA path); contradiction surfaced (BEAM `contradiction_resolution` needs both statements) |
| **Preference following** | PrefEval (explicit, choice-based, persona; filler sweep), BEAM preference ability. Reader prompts are factual-QA; only `CONVERSE_QA_PROMPT` (LoCoMo-Plus) answers as an assistant | Adherence judge for a personalised response (not "is the fact correct"), preference-turn R@k under filler, violation rate |
| **Multi-user / tenant** | Every item is one namespace; no test that two users with disjoint facts stay disjoint; PersonaBench has several people per community | Cross-namespace leakage rate, per-tenant R@k when tenants share a store, shared-grant behaviour |
| **Long-horizon scale** | LoCoMo conversations are about 590 turns; BEAM 100K/500K/1M is orders of magnitude longer. Pool 20-30, budget 4,096, window 2/4, bridge 3x cost and `session_leg` embedding all depend on haystack size. BEAM 10M not held | Retrieval-only scale curve (R_all@k and coverage vs haystack size), p95 latency and cost per question per benchmark |
| **Instruction following, summarisation, event ordering** (BEAM abilities) | No QA path or judge for them in the current stack | Per-ability judge or rubric; ordering accuracy (Kendall tau) |
| **Hallucinated memory / extraction** (HaluMem) | Only the memory-QA task is adapted; our write path stores raw turns, so extraction metrics do not apply | Memory-point recall/precision for any derived-memory feature (mined facts, list cards) |
| **Implicit recall** (LoCoMo-Plus, 401 items) | Constraint judge exists; no run with the best config | Constraint-adherence rate vs retrieval of the cue turn |
| **Data without timestamps or names** (ConvoMem, PrefEval, LaMP) | Behaviour of the date prefix, annotation, speaker features and shape regexes on such data is untested (section 2.1) | `fired_rate` of every trigger per slice; `has_timestamps` in the manifest |
| **Non-English, multimodal** | All shape and date regexes are English; image information is only the BLIP caption string; photo-only questions are errata | Language flag; no loader to measure |
| **Judge validity per benchmark** | Judge audit (150 hand labels, kappa) exists as a proposal for LoCoMo only [DF] 4.5 | Per-benchmark kappa vs hand labels; official-judge vs local-judge agreement |
| **Forgetting / deletion compliance, as-of reads** | `as_of` is never passed by the harness [PT section 2.2] | Deleted-fact leakage rate; as-of accuracy |

---------------------------------------------------------------------------------------------------

## 4. Proposed cross-benchmark protocol

### 4.1 Slices

One fixed dev slice (used for screening and decisions) and one held-out slice (opened only by a script, never read
per-question, run once per adoption batch) per benchmark, saved as `evals/analysis/<benchmark>_split.json` in the format
of `locomo_split.json` (dataset id, content hash, item ids, seed, date). LoCoMo already has its split.

Noise floor estimate (EST): A16 measured about +-5 net on 233 questions with a judge. Assuming independent flips, the band
scales with the square root of n: `floor_q = 0.328 * sqrt(n)` (0.328 = 5 / sqrt(233)), `floor_pts = 100 * floor_q / n`.
The first repeat-run per slice (I16) replaces these estimates. Note that +7 net on 584 dev questions (B1 adoption) is
at the edge of this scaled band (7.9), so B1 rests on its other evidence (held on conv-41/42 at +6/-3; simulated coverage
interval +20 to +41 fully covered list questions [B1 section 3]) rather than on the paired delta alone.

| Benchmark | n (total) | Dev slice | Held-out slice | Dev floor | Held-out floor | Run type |
|---|---|---|---|---|---|---|
| LoCoMo cat 1-4 | 1,540 | 584 (conv-26/30/41/42) | 956 (6 conversations) | 7.9 q = 1.4 pts | 10.1 q = 1.1 pts | retrieval-only, then QA |
| LoCoMo cat 5 | 446 | 173 | 273 | 4.3 q = 2.5 pts | 5.4 q = 2.0 pts | QA only (abstention judge); no gold turns |
| BEAM 100K | 400 probes / 20 conversations | 5 conversations = 100 probes | 15 conversations = 300 probes | 3.3 q = 3.3 pts | 5.7 q = 1.9 pts | retrieval-only (`R@k`, `R_all@k`); QA only for abilities with a judge |
| MemoryAgentBench FactConsolidation | per loader | 25% of rows | 75% | by formula | by formula | deterministic alias-contains QA plus supersession-order rate (no judge noise) |
| ConvoMem | stratified, `per_stratum` | 5 per (type, k) stratum | the next 15 | by formula | by formula | retrieval-only first; exact/semantic match QA |
| PrefEval | 3 forms x 20 topics | 1 row per topic per form (60) | the rest, subsampled | 2.5 q = 4.2 pts | by formula | retrieval-only (preference-turn R@k under filler); adherence QA on dev only |
| PersonaBench, LaMP-2, HaluMem, LoCoMo-Plus | per loader | about 25% of items, whole persons/users/conversations | the rest | by formula | by formula | LaMP: kNN vote (free); others retrieval-only |

Slice rules: split by item (conversation, user, person), never by question, so a conversation does not appear in both;
stratify by type/ability so every category has at least 7 questions in dev; keep abstention questions in their own
stratum; hash the split into the run manifest.

### 4.2 Run types (cheap before expensive)

1. **Stage 1, retrieval-only, deterministic, no reader, no judge.** For every candidate on every dev slice that has gold
   ids: R@k, `R_all@k`/all-gold-in-context, sufficiency, tokens in context, fired-rate of each trigger, `n_quarantined`.
   Retrieval is identical run to run (1,540/1,540 identical in the repeat check, A10), so there is no judge noise and a
   paired exact test on per-item hits is enough; keep the sqrt band as a conservative bound until the repeat run is done.
   Applies to LoCoMo cat 1-4, BEAM, ConvoMem, PrefEval, PersonaBench, LaMP (proxy gold), MemoryAgentBench, LoCoMo-Plus.
2. **Stage 2, QA on the dev slices**, only for candidates that pass stage 1, and for every reader, prompt, retry, window-
   rendering or judge change (retrieval-only cannot see those). Slices: LoCoMo cat 1-4 (584), cat 5 (173),
   BEAM (100), plus MemoryAgentBench alias QA. Paired per-item verdicts, cluster bootstrap by conversation/user.
3. **Stage 3, held-out QA once per adoption batch**, pre-registered like `prereg/2026-10-10_heldout_reader_fixes.md`; the
   per-question output is not opened.
4. Judge: until A1 is done, a QA result counts only if it agrees in sign with the deterministic metric of stage 1 or with
   an alias/exact-match judge; otherwise it is reported as "judge-only".

### 4.3 Adoption rule

Let `D_b` be the paired net (gained minus lost) on dev slice `b`, `F_b = 0.328 * sqrt(n_b)` its floor, `B` the set of slices
on which the feature fires (inert slices must show byte-identical retrieved ids, which makes `D_b = 0` by construction).

**User decision 2026-10-10: the rule leans on the average** ("make it more towards average and improve all"). Rule 2 (the
aggregate and macro mean) is the primary test; rule 1 is a guard against hidden losses, not a veto on every small dip.

A change is adopted only if **all** hold:

1. **No slice loses beyond its noise band (guard):** `D_b >= -F_b` for every slice (retrieval-only and QA, including the
   cat-5 and BEAM abstention/knowledge-update strata). A slice with `-F_b <= D_b < 0` is reported and gets one repeat when
   the change is otherwise adopted; a slice below `-F_b` rejects the change (or scopes it to a shape profile, 4.4).
2. **Primary: positive on the aggregate beyond the floor:** the pooled sum `sum_b D_b >= F_agg`, with `F_agg = 0.328 * sqrt(sum n_b)`
   (857 dev items in the three QA slices: 9.6 q = 1.12 pts), **and** the macro mean of per-slice point deltas is positive with
   a benchmark-level bootstrap interval above zero (macro floor for the three QA slices, EST: about 1.5 pts). A gain that
   comes from one benchmark alone is a per-benchmark setting, not a default (see 4.4).
3. **Stage 3 reproduces the sign** on each held-out slice, with the same per-slice rule using `F_b` of the held-out size.
4. **Cost guard:** context tokens and p95 latency per question do not rise more than a pre-stated margin on a slice where
   quality does not rise (the list tier added +686 tokens on non-list questions for a gain of +7/-2 [B1 section 4]).
5. **Fired-rate report:** for every trigger, the fraction of questions it fires on, per slice, with the fired/unfired split
   of `D_b`. A trigger that fires on more than 25% of a slice's questions without a matching gain is demoted to opt-in.
6. **No LoCoMo-only metric moves:** the date check, errata adjustment and lenient list credit are reported with and without;
   they never count towards `D_b`.

### 4.4 When a feature helps one data shape only

Do not refuse it, scope it. Introduce **shape profiles** declared by the dataset adapter (`has_timestamps`,
`has_question_date`, `speaker_kind` = named-peers / user-assistant / single-user, `abstention_possible`, `turn_length`,
`haystack_size`) and select config defaults from the profile: window rule, date prefix/annotation, prompt clauses, retry,
list trigger. The adoption rule then applies per profile: a profile default is adopted when it satisfies 4.3 on all slices
of that profile; the global default stays at the last config that satisfies 4.3 everywhere.

### 4.5 Prerequisites (small, no engine change needed for the first three)

1. Cut and hash the splits for BEAM, ConvoMem, PrefEval, MemoryAgentBench (script reading the loaders).
2. Run one repeat of the reference config per slice to replace the estimated floors (A16 procedure).
3. Add the fired-rate and `n_quarantined` fields to each retrieval-only summary.
4. Hand-label 150 rows per QA slice and report judge kappa (A1 follow-up); until then rank candidates on the deterministic
   metric.

LongMemEval is excluded from the suite by user decision (no slice, no run); its question text is cited only as evidence in section 2.1.

Cross-references: new register rows I1-I26 in `GAP_REGISTER.md` section I; H6 (test isolation) is part of the same
import-isolation family as the `date_check` import in section 2.3.
