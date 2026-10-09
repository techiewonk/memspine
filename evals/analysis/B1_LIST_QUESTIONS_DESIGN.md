# B1 design: category-vs-instance list questions (2026-10-10)

Gap B1 in `GAP_REGISTER.md` / cause H in `RECALL_GAPS_FORENSIC.md`. CPU-only offline study; no LLM, no GPU, no code
change, no eval run. Everything marked **MEASURED-SIM** was computed on the logged per-question forensic file of the
fixed config (`runs/qa-full-qs-eq06-fix--memspine/report/per_question.jsonl`, all 1,540 questions; reranker off,
candidate pool 1, hits = fused top-10, window 2 before / 4 after) plus a CPU re-embedding of all 5,882 turns and 1,540
questions with `Qwen/Qwen3-Embedding-0.6B` (fp32; the logged legs come from the GPU bf16 run, so the new legs differ
from a live run by rounding only). Everything marked **ESTIMATE** could not be computed without an LLM.

## 1. Question set and baseline coverage

**Set L ("list questions", the evaluation label, 202 questions).** Multi-hop (category 1) questions that do not start
with when / how many / how long / a yes-no verb, have at least 2 resolvable gold turns, and whose gold answer is a list
(a comma or " and ") or has at least 3 gold turns. 202 of the 282 multi-hop questions. Eight gold ids in the dataset
are malformed (`D:11:26`, `D9:1 D4:4 D4:6`, ...) and are dropped from every count below (the forensic file counts them
as never in context). L is only an analysis label (it uses gold); no design below uses it as a trigger.

**Trigger used by the designs (question text only).** `is_list_q` = what/which/who + a plural set noun
(activities, events, hobbies, books, items, places, ...), or "what kind/type of", or "what do/does/has/have X
do/like/attend/play/...", minus when/how many/yes-no openers; plus exactly one speaker named in the question.

| trigger | fires | of L caught | fires on |
|---|---|---|---|
| `is_list_q` (regex above) | 343 (304 with exactly one named speaker) | 138 (68%); 121 with one named speaker | single-hop 177, multi-hop 150, open-domain 11, temporal 5 |
| existing `query_shape.is_aggregation or is_count` | 146 | 78 (39%) | multi-hop 104, single-hop 29, temporal 10, open-domain 3 |

Precision of `is_list_q` against L is 40%; most of the other fires are single-hop questions of the same shape ("What
pets does Melanie have?", "What games does Jolene recommend for Deborah?"). That false-fire population is the
side-effect risk in section 4.

**Baseline coverage today** (MEASURED from the log; "context" = hits plus window):

| set | n | gold turns in context | fully covered | all gold in some leg top-30 | all gold in fused top-10 | judge accuracy | context tokens |
|---|---|---|---|---|---|---|---|
| ALL | 1,540 | 1831/2348 (78.0%) | 1259 (81.8%) | 1255 | 1008 | 80.1% | 2,071 (logged) |
| multi-hop | 282 | 535/882 (60.7%) | 108 (38.3%) | 141 (50.0%) | 62 | 62.8% | 2,204 |
| **L (list)** | **202** | **420/698 (60.2%)** | **73 (36.1%)** | **98 (48.5%)** | **39** | **62.9%** | 2,202 (2,126 by my chars/4 count; 55.4 turns) |
| L, 2 gold turns | 73 | | 42 | | | | |
| L, 3-4 gold turns | 94 | | 25 | | | | |
| L, 5+ gold turns | 35 | | 6 | | | | |
| single-hop | 841 | 864/895 (96.5%) | 810 | | | 90.8% | 2,012 |

Accuracy on L is 76.7% when all gold is in context and 55.0% when not (n = 73 / 129), so full coverage is worth about
0.22 per question; use that to turn coverage gains into expected judged-correct gains.

Properties of L that shape the designs (MEASURED):

- 182 of 202 name exactly one of the two speakers; **96% (592 of 617) of those questions' gold turns are spoken by that
  person**. A speaker restriction therefore costs almost no recall. 18 name both speakers, 2 name neither.
- Gold sits in a mean of 2.9 sessions out of about 27.5 per conversation; the named speaker has about 297 turns per
  conversation.
- Within the named speaker's turns only (vector, cosine to the question), the 232 gold turns that are lost today rank:
  top 10: 53, top 30: 135, top 60: 181, top 100: 206, median 24.5. So most lost instances are reachable by a
  speaker-restricted ranking at depth 30, but not at depth 10, which is why a bigger hit budget matters as much as the
  new leg.

## 2. Simulation results

Method: base legs are the logged vector top-30, BM25 top-30 and any logged extra leg (temporal); a design adds legs and
re-fuses by RRF (k=60), takes the top-10 hits (or the tiered/changed budget stated), applies the window. Re-fusing the
logged legs with no change reproduces the logged hit set on 1,540 of 1,540 questions and the logged context on 1,540
of 1,540 (control row: +0 everywhere). Columns: change in gold turns in context on L (base 420/698), change in fully
covered questions on L (base 73 of 202), change in fully covered questions on all 1,540 (base 1259), mean context
tokens on L by chars/4 (base 2,126), and the side effect on fired questions that are not in L.

### 2.1 Existing config keys (MEASURED-SIM) and the single-leg designs

| design | fired q | L gold | L full | ALL full | L ctx tokens | non-L fired q | non-L full gain/loss | non-L tok delta |
|---|---|---|---|---|---|---|---|---|
| control (re-fuse logged legs) | 1371 | +0 | +0 | +0 | 2126 | 1189 | +0/-0 | +0 |
| existing `speaker_probe` (global "Name: core terms" probe), any named q | 1371 | +12 | +0 | -15 | 2071 | 1189 | +16/-31 | -38 |
| existing `speaker_probe`, list trigger | 304 | +4 | +0 | -4 | 2090 | 183 | +2/-6 | +18 |
| existing `subject_leg` (named speaker's turns by word overlap), any named q | 1371 | -39 | -19 | -37 | 2166 | 1189 | +20/-38 | +117 |
| existing `subject_leg`, list trigger | 304 | -35 | -15 | -19 | 2155 | 183 | +2/-6 | +177 |
| **a1** speaker-restricted vector leg, top-30, added as 3rd RRF leg, any named q | 1371 | +35 | +15 | +8 | 2139 | 1189 | +22/-29 | +32 |
| **a1**, list trigger | 304 | +22 | +9 | +5 | 2136 | 183 | +3/-7 | +66 |
| a1 on the 182 named L questions (trigger = oracle, upper bound of the leg) | 182 | +35 | +15 | +15 | 2139 | 0 | | |
| a1 with weight 2 (leg twice), any named q | 1371 | +33 | +15 | +3 | 2170 | 1189 | +23/-35 | +89 |
| a2 restricted vector leg replaces the global vector leg, any named q | 1371 | -1 | +0 | +6 | 2152 | 1189 | +15/-9 | +57 |
| a1 implemented as post-filter of the global vector top-100 (list trigger) | 304 | +22 | +9 | +5 | 2136 | 183 | +3/-7 | +66 |
| a1 as post-filter of the global top-60 / top-30 | 304 | +22 / +20 | +9 / +8 | +5 / +3 | | | | |
| **c1** restricted + session round-robin (one hit per session first), top-30, any named q | 1371 | +32 | +14 | +9 | 2296 | 1189 | +29/-34 | +229 |
| c1, list trigger | 304 | +23 | +9 | +5 | 2238 | 183 | +4/-8 | +300 |
| c2 a1 + c1 legs, list trigger | 304 | +31 | +13 | +10 | 2259 | 183 | +4/-7 | +335 |
| c1 with top-60 depth, list trigger | 304 | +25 | +11 | +5 | 2227 | 183 | +3/-9 | +237 |

Findings: (a) the existing keys do not help. `speaker_probe` is a global probe and neutral to negative; `subject_leg`
ranks the speaker's turns by word overlap with the question, which is exactly the vocabulary that instances do not
share, and it hurts (-19 fully covered). (b) Restricting the vote to the speaker's turns helps (a1: +15 on L when it
fires on every named question), but only by about 9-15 questions because the hit budget (10 hits) is the cap, not the
ranking. (c) Using the restricted ranking as a replacement (a2) loses the gain: it must be an extra vote, not a
substitute. (d) Diversity (c1) adds no coverage over a1 and costs 100-330 more context tokens, because one hit per
session scatters windows; this agrees with the earlier negative on `session_cap` (B14). (e) a1 can be built from the
existing vector leg by over-fetching and filtering on the speaker tag: a depth of 60 reproduces the true restricted
top-30 (identical result at depth 100), so no vector-store change is needed. MMR (`mmr_lambda`) only reorders the final
hits (schema comment), so at a fixed hit count it cannot change coverage; not simulated.

### 2.2 Budget variants and the existing aggregate path (MEASURED-SIM)

"Tier" = hits ranked 1-10 keep the full 2/4 window, hits ranked 11-30 enter as single turns (needs `candidate_pool: 3`;
this is RECALL_GAPS option H1). Without a leg change the fused candidates are the logged ones.

| design | fired q | L gold | L full | ALL full | L ctx tokens | non-L fired q | non-L full gain/loss | non-L tok delta |
|---|---|---|---|---|---|---|---|---|
| tier only, every question | 1540 | +75 | +32 | +75 | 2759 | 1338 | +43/-0 | +613 |
| tier only, list trigger | 304 | +47 | +20 | +23 | 2508 | 183 | +3/-0 | +624 |
| tier only, `is_aggregation` trigger | 146 | +28 | +14 | +21 | 2374 | 68 | +7/-0 | +623 |
| existing `aggregate_in_replay` top-20, full windows, `is_aggregation` trigger | 146 | +32 | +14 | +18 | 2818 | 68 | +4/-0 | +1768 |
| existing `aggregate_in_replay` top-15, full windows | 146 | +17 | +7 | +8 | 2474 | 68 | +1/-0 | +884 |
| shrink windows to buy hits: top-20, window -1/+1, no new leg, list trigger | 304 | +3 | +5 | -3 | 2046 | 183 | +4/-12 | +7 |
| a1 + top-20 hits, window -1/+1, list trigger | 304 | +30 | +11 | -1 | 2053 | 183 | +6/-18 | +15 |
| a1 + top-24 hits, window -0/+1, list trigger | 304 | +48 | +18 | +6 | 1931 | 183 | +7/-19 | -178 |
| c1 + top-24 hits, window -0/+1, list trigger | 304 | +17 | +5 | -5 | 1915 | 183 | +5/-15 | -187 |
| **a1 + tier**, list trigger (hits 1-10 full window, 11-30 single) | 304 | **+80** | **+30** | **+35** | 2513 | 183 | +7/-2 | +686 |
| a1 + tier with only ranks 11-20 as singles | 304 | +60 | +23 | +22 | 2323 | 183 | +5/-6 | +373 |
| a1 + tier, every named q | 1371 | +117 | +43 | +83 | 2702 | 1189 | +51/-11 | +652 |
| a1 + tier, `is_aggregation` trigger (named) | 134 | +39 | +16 | +27 | 2365 | 62 | +11/-0 | +649 |
| c1 + tier, list trigger | 304 | +63 | +23 | +29 | 2617 | 183 | +7/-1 | +914 |

Findings: the existing `aggregate_in_replay` path gives +14 on L for +1,768 tokens on its non-L fires; the tier gives the
same +14 for about a third of the tokens. Shrinking windows to afford more hits trades gold supplied by window
neighbours against new instances and loses on non-list questions (-12 to -19 fully covered); only a1 + 24 hits with a
0/+1 window is net positive and it is still worse than the tier on non-list questions. Gating the tier on how many
sessions the first hits span (>=5 to >=8) removes the gain faster than the cost (hits are spread over about 7 sessions
for list and single-hop questions alike); not useful.

### 2.3 Expansion probes (b) and instance-seeded second round (d)

(b) uses a hand list of about 12 generic category-to-vocabulary entries (`activities/hobbies -> painting pottery hiking
camping swimming ...`, `events -> signed up joined parade rally workshop ...`, books, items bought, places visited,
kids like, music, pets, food, people met, movies/games, jobs/goals), written from category nouns, not from gold. It
matches 235 of the 304 list-trigger questions (99 of the 121 L questions that fire). Probe = question + " Examples: " +
the words; leg = global or speaker-restricted vector top-30, and/or own BM25 (not tantivy) restricted to the speaker,
over the instance words only. Rows are on the 235-question lexicon trigger.

| design | fired q | L gold | L full | ALL full | L ctx tokens | non-L fired q | non-L full gain/loss | non-L tok delta |
|---|---|---|---|---|---|---|---|---|
| a1 (reference on this trigger) | 235 | +19 | +7 | +1 | 2137 | 136 | +0/-6 | +86 |
| b1 expansion probe, global vector leg | 235 | +16 | +3 | -2 | 2118 | 136 | +0/-5 | +58 |
| b2 expansion probe, speaker-restricted vector leg | 235 | +23 | +6 | +1 | 2131 | 136 | +0/-5 | +92 |
| b3 expansion words, speaker-restricted BM25 leg | 235 | +28 | +3 | -3 | 2145 | 136 | +0/-6 | +97 |
| **b4 = a1 + b2 + b3** | 235 | +43 | +15 | +8 | 2150 | 136 | +0/-7 | +160 |
| b5 one probe per instance word (12 words x top-5, restricted) + a1 | 235 | -25 | -15 | -37 | 2076 | 136 | +1/-23 | +49 |

(d) instance-seeded round 2: first pass = base legs + a1; seeds = the 5 best hits spoken by the named speaker; query =
question + " Example: " + the seed turn's text (image caption removed, 240 chars); one restricted (d2) or global (d1)
top-10 leg per seed, fused with a1. List trigger, 304 fired.

| design | fired q | L gold | L full | ALL full | L ctx tokens | non-L fired q | non-L full gain/loss | non-L tok delta |
|---|---|---|---|---|---|---|---|---|
| d1 seeds, global legs, + a1 | 304 | +20 | +8 | -1 | 2081 | 183 | +2/-11 | -9 |
| d2 seeds, speaker-restricted legs, + a1 | 304 | +25 | +11 | +2 | 2126 | 183 | +3/-12 | +99 |
| d2 + tier | 304 | +78 | +30 | +33 | 2510 | 183 | +8/-5 | +727 |

Findings: the hand-list expansion adds a little when combined (b4 vs a1: +24 gold turns, +8 fully covered on L, 99
questions) but alone it is not better than a1; per-instance-word probing (b5) is clearly harmful (twelve votes from
generic words swamp the question). d2 gives +2 over a1 (+11 vs +9) for 5 extra embeddings and 5 searches per question and
a loss profile on non-list questions that is worse (-12 vs -7) because seeds drift to what was already found; with the
tier it equals a1 + tier (+30). Not worth the cost.

### 2.4 Oracle and LLM-based designs (e) (oracle MEASURED-SIM, real LLM ESTIMATE)

Oracle decomposer: for each answer item in the gold answer (split on comma, "and", ";"), the probe "Name: item"; leg =
restricted (e1) or global (e2) vector top-5 per item; fused with base + a1. L questions that name one speaker (182).
This is what a perfect decomposer that already knows the instances would do; it is an upper bound, not a prediction.

| design | fired q | L gold | L full | ALL full | L ctx tokens |
|---|---|---|---|---|---|
| a1 alone on the same 182 questions (reference) | 182 | +35 | +15 | +15 | 2139 |
| **e1 ORACLE** item probes, restricted top-5 each + a1 | 182 | +83 | +32 | +32 | 2116 |
| e2 ORACLE item probes, global top-5 each + a1 | 182 | +82 | +32 | +32 | 2100 |
| **e3 ORACLE** e1 + tier | 182 | +164 | +66 | +66 | 2695 |

ESTIMATE (cannot be computed here): `planner: llm` v2/v3 decomposition (H3) has to guess the instances from the
category word, so it recovers a fraction of e1/e3. Taking 30-50% of the oracle gives about +10 to +16 fully covered
questions at the fixed 10 hits and +20 to +33 with the tier, consistent with the +18 to +36 multi-hop estimate already in
`RECALL_GAPS_FORENSIC.md`. Mined list cards (H4) are not estimable from retrieval logs; they could in principle approach
or pass the oracle (they gather instances regardless of wording) but depend on miner recall, need an LLM at ingest and
have a documented temporal-accuracy cost. Both need a live run. The oracle also shows that even perfect probes at 10
hits stop at +32; the hit budget (tier) is worth as much as the probe quality.

## 3. Robustness of the best designs (MEASURED-SIM)

| design (list trigger, 304 fired) | L fully covered delta | bootstrap 95% interval | dev conv-26/30 (28 L q) | other 8 conversations (174 L q) | expected judged-correct gain on L (delta x 0.22) |
|---|---|---|---|---|---|
| tier only | +20 | 13 to 29 | +3 (+3/-0) | +17 (+17/-0) | about +4 |
| **a1 + tier** | **+30** (103 of 202) | 20 to 41 | +5 (+6/-1) | +25 (+26/-1) | about +6.5 |
| a1 + b2 + b3 + tier | +37 (110 of 202) | 26 to 49 | +5 (+6/-1) | +32 (+33/-1) | about +8 |

By gold count, a1 + tier takes fully covered questions from 42 to 56 of 73 (2 gold turns), 25 to 37 of 94 (3-4), 6 to 10
of 35 (5+). Expected effect on the whole benchmark: +4 to +8 judged-correct questions of 1,540 (+0.3 to +0.5 point),
before any non-list effect. Three of the dev questions named in `DEV_GAP_REASONING`: "What activities does Melanie
partake in?" (4 gold) 1 -> 1 gold turn in context with a1 + tier, 2 with the lexicon legs; "What events has Caroline
participated in to help children?" (2 gold) 0 -> 1; "What activities has Melanie done with her family?" (6 gold) 0 -> 2
(a1 + tier), 4 (with lexicon legs). One regression is visible ("What transgender-specific events has Caroline attended?",
2 -> 1 gold turns).

Caveats: the trigger regex and the lexicon were written after reading examples from all ten conversations, so the
dev/held-out split above is not blind; treat the held-out numbers as indicative. The reference run has the reranker
off; with `rerank: qwen3` and pool 2-3 part of the tier gain is already taken (H2 oracle: +81 questions overall), so the
two should be measured together, not added. Token counts are chars/4 of raw turn text; the logged context tokens are
about 3.5% higher for the same set.

## 4. Side effects on non-list questions

Applied to all 1,540 questions, the list trigger fires on 304 (183 outside L: 160 single-hop, 10 open-domain, 8
multi-hop, 5 temporal). a1 + tier on those 183 (MEASURED-SIM):

- fully covered: +7 gained, -2 lost (single-hop +4/-2, temporal +1/-0, open-domain +0/-0, multi-hop +2/-0); gold turns
  +10/-4. The losses are fusion displacement by the new leg (0-70, 5-28, 5-109, 7-59 in the forensic ids).
- context grows by +686 tokens on average on those 183 (1,949 -> 2,635, +35%) because the tier adds up to 20 single
  turns. Their baseline accuracy is 86.9%, so the added tokens are a pure cost with a small expected gain: reader
  dilution is the real risk and cannot be measured offline.
- Cheaper variant: tier with only ranks 11-20 as singles keeps +23 of the +30 for +373 tokens (a1 + tier 10+10 row).
- Applying a1 + tier to every named question gains more on L (+43) and on ALL (+83) but costs +652 tokens on 1,189
  non-L questions for +51/-11; this is H1 over the whole benchmark and should be a separate decision (it needs the
  reader to be robust to 3k-token contexts).
- Plain a1 without the tier: on all 1,371 named questions it is +22/-29 on non-L fully covered, i.e. roughly neutral
  to slightly harmful alone; the leg is only worth shipping together with the extra hit budget and behind the trigger.
- Do not use: `subject_leg` (-37 fully covered overall), global `speaker_probe` (-15), per-word probes (-37), sessions
  spread gate (no signal), windows shrunk for hits (-12 to -19 on non-list).

## 5. Recommendation

**Implement one design, with one optional add-on:**

1. **List mode = speaker-restricted vector vote (a1) + rank-tiered pool (H1), behind a question-shape trigger.**
   MEASURED-SIM on the fixed run: +30 of 202 list questions fully covered (73 -> 103), +80 of 278 missing gold turns,
   +35 fully covered over all 1,540, bootstrap interval 20 to 41, +6.5 expected judged-correct questions, +686
   context tokens only on the 304 fired questions (about +7% over the whole benchmark), no extra LLM call, no extra
   embedding, negligible latency (an in-memory filter of a wider vector fetch; not timed live).
2. **Add-on (second step, only after 1 is confirmed live): the lexicon legs b2 + b3** (one extra embedding, one
   speaker-restricted lexical leg): +7 more fully covered (110 of 202), +13 more gold turns. Because the lexicon is
   hand-written, ship it as data in config, off by default, and evaluate on the held-out conversations only.
3. Do not build: diversity round-robin (c), instance-seeded second round (d), word-by-word probes, new use of
   `subject_leg` / `speaker_probe`. Keep H3/H4 (LLM) as the route to the oracle's remaining headroom (+32 at 10 hits,
   +66 with the tier), measured by a live run.

### Mapping to the engine

| piece | exists | new |
|---|---|---|
| speaker tags on turns | `memories.episodic.policies.subject_tagging: true` writes `speaker:<name>` (engine.py ~1402); the eval config of the fixed run does not set it, so the screen must | none |
| name detection | `temporal_query.speaker_leg` already matches a speaker name as a whole word against the `speaker:` tags | reuse the same lookup |
| restricted vector vote | `read.subject_leg` is a lexical word-overlap leg (measured harmful) | **new** `read.subject_vector_leg: bool` (name may differ) |
| bigger candidate pool | `read.candidate_pool` (up to 10; 3 = 30 candidates), `read.rerank_keep`, `assembly.relative_floor: 0` | none |
| window per side | `read.replay_window_before/after` (2/4) | none |
| tier (full window for ranks 1-10, single turns for 11-30) | not present (RECALL_GAPS H1 says "new code, small") | **new** `read.window_full_hits: int` (None = all hits get the window) |
| trigger | `query_shape.is_aggregation` / `is_count` (39% of list set), `read.aggregate_in_replay`, `aggregate_top_k`, `list_cards_only_aggregate` (flat, 3x the tokens, measured above) | **new** `query_shape.is_set_question(query)` (the regex of section 1, 68% of list set) and a key `read.list_mode: off \| aggregation \| set_question` that gates the two new pieces plus `candidate_pool` for that read only |
| expansion lexicon (add-on) | none | `read.list_lexicon: dict[str, list[str]]` and a probe leg; reuse `_probe_legs` |
| rejected | `session_cap`, `mmr_lambda`, `cluster_expand`, `prf_expansion`, `second_round` (names/dates only), `planner: llm` (needs LLM, H3) | |

### Sketch

1. `core/temporal_query.py`: add `speaker_vector_leg(query, records, vector_hits, top_k=30) -> list[LegHit]` next to
   `speaker_leg`: build the `speaker:<name>` map exactly as `speaker_leg` does; if the query names exactly one
   speaker, return the first `top_k` `vector_hits` whose record carries that tag, in vector order; if it names zero
   or two speakers, return `[]`. Pure function, easy to test.
2. `engine._search` (the `while True` loop, after `vector_hits = await self._vector_leg(...)`): when
   `read.list_mode` is on and the trigger holds, `fetch_k_vec = max(fetch_k, 100)` for the vector call only (the
   restricted result is the same at depth 60-100 in the simulation), then pass `vector_hits` and the live records to
   `speaker_vector_leg` and append its result as `NamedLeg("subject_vector", ...)` to `extra_legs` before the RRF
   call (the same place the metadata legs are appended). The records list is already loaded in `_metadata_legs`; add
   the call there (`read.subject_vector_leg` alongside `read.subject_leg`) and let it receive `vector_hits` as an
   argument. Keep the existing `hide`/`widen` behaviour untouched.
3. Assembly (`_assemble_core` / `_expand_neighbours` in `engine.py` ~4455 / ~6490): when `list_mode` fires, set
   `want = 30` (`candidate_pool: 3`) and call `_expand_neighbours` only for `chosen[:window_full_hits]`; the remaining
   chosen turns are rendered as single turns, still passing the same gates and the token budget (relative floor off, as
   in the H1/H2 recipe). Render order stays chronological.
4. Config: `read.list_mode`, `read.window_full_hits` (default 10), `read.subject_vector_leg`; `describe()` must record
   them (the B12 lesson); everything off = byte-identical.
5. Tests: (a) `speaker_vector_leg`: one name -> only that speaker's hits in vector order; two names / none -> empty;
   case and possessive ("Melanie's kids"); records without tags -> empty; (b) `is_set_question`: positives from the
   examples ("What activities does Melanie partake in?", "What do Melanie's kids like?", "What events has Caroline
   participated in to help children?", "What kind of fiction stories does Tim write?"), negatives ("When did ...",
   "How many ...", "Did ...", "What book did Melanie read from Caroline's suggestion?" should not fire on the singular
   past-tense form); (c) tier assembly: 30 candidates, windows only on ranks 1-10, singles beyond, budget cap respected,
   `window_full_hits: None` identical to today; (d) off = byte-identical context on a fixture; (e) integration on a toy
   two-speaker conversation where the instance turns share no words with the question.
6. Evaluation: paired screen on conv-26 and conv-30 (as for B9), then the other 8, comparing `list_mode: set_question`
   (a1 + tier, tier 10+30) against the fixed config and against `candidate_pool: 3` + tier alone; report fully covered on
   L, judged accuracy by category, context tokens on fired vs not fired, and run with and without the reranker (pool
   2-3, keep 10). Decision rule: keep if L accuracy rises with no drop on single-hop beyond noise; if single-hop falls,
   switch the trigger to `aggregation` or the tier to 10+10.

## 6. Reproducibility

Scripts are in the session scratchpad (`b1_core.py`, `b1_harness.py`, `b1_t1.py` baseline, `b1_sim1..8.py` designs,
`b1_lex.py` hand list, `lq.py` classifier). They read `data/locomo10.json` and the per-question forensic file, embed
turns on CPU, and change nothing in the repository. Note: the scratchpad `docs.npy` and `qs.npy` were overwritten with
equivalent CPU embeddings of the same turns and questions (same model, same order).
