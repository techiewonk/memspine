# Owner observations: code-grounded gap analysis and enhancement proposals (OBS-GAPS)

Generated 2026-10-14 (CPU only, no model call, no GPU). **Text-free**: this file carries ids, counts, class names, settings and invented names (Ilse, Bram, Noor, Tomasz) only. No LoCoMo turn, question, answer or speaker name appears here. Per-question evidence that carries text stays under the gitignored `evals/runs/_analysis/obs_gaps/`.

Sources: `src/memspine` (code traced, not run), `docs/v2/LIBRARIES.md`, `docs/v2/PLAN.md`, `docs/v2/data/screen_internals_{write,read,sleep}.json`, `docs/v2/SCREEN_ATLAS.html`, `docs/v2/LOCOMO_PROBLEM_MAP.md`, `evals/analysis/locomo_solution_map.json`, `evals/analysis/GAP_REGISTER.md`. Measurements: `evals/obs_gaps_measure.py` over `data/locomo10.json` and the `clean-jina-full` problem table (1,540 questions, 287 wrong, 81.4% correct); results in `docs/v2/data/obs_measurements.json`. Structured version for the Atlas: `docs/v2/data/observations.json`.

## 0. Reading guide

**Status vocabulary.** *ref* = on in the reference arm (`scr-pool-protect-jina`). *built-off* = built, flag-off, a screen is queued. *built-off, no screen* = built, flag-off, no screen id. *partial* = a piece exists but not the capability. *missing* = nothing in `src/`.

**What the reference run actually does** (this is the baseline every observation is measured against). Ingest is verbatim: no write-time model call, one record per turn, stored text `Speaker: text` (the harness appends `[image: caption]` to a turn with a caption: `evals/memspine_evals/datasets/locomo.py:170`). The embedder and BM25 both index `record.content` only (`ingest/write.py:626`); there is no separate index text. Read = vector + BM25 + `perspective` leg, `pool_protect_per_leg=3`, Jina rerank (keep 10), replay window 2 before / 4 after, `list_mode`, `resolve_relative_dates` + `relative_dates_anchored` (read-time `[= date]` annotation), `[date]` line prefix. Reader prompt `grounded` (sha `71c12de6...`; "best-supported answer, even if indirect; not mentioned only when nothing bears on the question") plus a neutral refusal retry. Everything below the line "built-off" is unmeasured in v2: **no write-side v2 screen has run yet** (SCREENS.md: "Nothing here has been run"). So most proposals are "run the screen that already exists, then close the narrow gap", not "build a new subsystem".

**Wrong-answer counts.** "Targeted" = how many of the 287 wrong answers in `clean-jina-full` the observation could plausibly change. Each is a rule over the problem-table labels (class, stage, category) and, where noted, a heuristic over the data. They are upper bounds on opportunity, not forecasts of gain; a capture-rate assumption is a hypothesis, not a measurement (same rule as `LOCOMO_PROBLEM_MAP.md`).

**Measured vs heuristic.** *measured* = read from logged ranks or classifier labels. *heuristic* = a regex or token rule written here (rule stated). Heuristics over free text over-count in one direction or the other; the rule is given so it can be audited.

## 1. Summary table

| # | Observation | Generic capability | Status today | Gap | Proposal | Wrong targeted (of 287) | Effort | Priority |
|---|---|---|---|---|---|---|---|---|
| 1 | Name variants, nicknames, pronouns to the canonical entity | Participant-scoped alias binding at write | partial: W2-09 resolver + nickname table (`write.resolver_v2`, built-off, S62); `stamping_v2` has addressee | Resolver runs on extracted subject/object only; table is closed (no prefix/short-form rule); bare given name vs full name is only `pending_merge`; vocatives and the read-side query never use aliases | `write.vocative_alias_v2`: bind a greeting/vocative token to a participant by exact, table or unique-prefix match; learn the alias; no model call | <= 9 (35 gold turns carry a short form; 54 incidental) | S | P3 |
| 2 | Decontextualise turns to standalone dated statements | Turn restatement (standalone + absolute date + resolved speaker) | built-off: `write.window_extract=facts` + `prompt_v2` + `temporal_v2` (S03, S43, S44, S62) | Restatement only competes as a separate fact record; the vector/BM25 index still sees the raw turn; no cheap rule-based index view; unmeasured | `write.index_view: context` (rule-based embed/BM25 text with speaker+date+session topic, 0 calls) then merge with the restate stage (item 3) | 14 to 33 (temporal evidence missing + date arithmetic) | S then M | P2 |
| 3 | Context carry-over inside a session; rule for when to rephrase | Gated turn completion with antecedent link | partial: W2-06b CONTEXT lines (previous window, never cited); read-time replay window | No incompleteness decision; extraction rewrites every window or none; CONTEXT is not an overlap; no antecedent edge | `write.restate_gate`: rule score of incompleteness signals; one call only for flagged windows; output cites antecedent lines, grounded, stored as derived child with a `context` lineage edge | 15 (21 upper) | M | P2 |
| 4 | Per-person profile attributes with change history | Attribute-history profile with typed registry | partial, three unlinked pieces: v1 `rule_miner` slots + `profile_slots_header`; W2-10 ladder + O4-04 `value_history`; O4-07 profile card (24 entries, 5 sections) | No default attribute registry (`integration_cardinality` defaults to `{}`); history is a projection never rendered to the reader; sensitive attributes only gated by `profile_sensitive` | `offline.person_attributes_v2`: default registry (cardinality, tier, consent purpose) shared by miner, ladder and card; `read.profile_history` renders "X (from date) -> Y (from date)" | 33 (60 over all categories) | M | P2 |
| 5 | Layer 3: image relevance and world knowledge | Asset-grounded evidence + opt-in enrichment | partial: W2-11 asset describer (off), `read.asset_evidence`, `read.external_v2` (S23, read-time, generic questions only) | Caption lines are never typed as image evidence at write; no relevance note; no write-time enrichment layer; external is not wired to captions | `write.caption_facts` (rule: caption line -> `asset_note` record, parent = turn) and opt-in `write.enrich_external` through the labelled-block rules of `read/external.py` | 16 (6 classifier-labelled) | M (S for captions only) | P3 |
| 6 | Inference from memory, short committed answer | Shape-gated inference route + evidence-seeking probes | partial: reader variants `dated_infer`, `grounded_generic_infer` (harness, no screen); `routed` prompt; `plan@v2` probes (built-off) | Reference prompt cannot commit when the question is an inference ask; the yes/no clause once regressed other shapes; no probes for indirect evidence | `read.inference_mode`: contract `request=inference` selects a commit-with-reason clause (rules, 0 calls) and, optionally, 1 planner call for evidence-seeking probes | 54 (26 reader + 28 retrieval); 14 refusal-style | S (prompt) + S (probes) | **P1** |
| 7 | Session and window summaries, topic-based, overlapping | Multi-resolution summaries | built-off: `session_close_v2` (S33), episode section + `episode_leg_v2` (S31, S61), `reflect_profile` summaries (S01) | No window overlap (CONTEXT is read-only); no topic label on every summary; summaries are unmeasured | `write.window_overlap_turns` (stride < size) + a `topic` field on episode/summary records; screen what exists first | 59 (gold across >= 3 sessions) | S | P2 |
| 8 | Link new turns to older facts; synonym/paraphrase query expansion | Backreference linking + multi-probe retrieval | partial: `plan@v2/v3` evidence-seeking subqueries, `compose_rewrites`, `prf_expansion`, `statement_probe` (all built-off); true HyDE leg not built | No rule paraphrase/synonym probe; no backreference edge; none has a v2 screen | `read.paraphrase_probes` (planner v2 reuse, <= 2 probes) and `write.backref_link` (rule detect, antecedent by retrieval) | 10 labelled + 17 backreference gold | S to M | P3 |
| 9 | Plural and pronoun enrichment ("they" = the kids) | Referent annotation on facts | partial: `window_extract` rule (3) "never write he/she/they"; `persons` field | Plural group referents are not a field; unmeasured; "we" is ambiguous | `referents` list on the restate item (names or role noun + count), grounded to cited lines; part of item 3 | <= 18 (2 plural-gold) | S (inside 3) | P3 |
| 10 | Multi-entity / multi-hop as map-reduce | Query decomposition with combine operators | partial: `multi_intent_split` (discourse markers), `plan@v3` persons + `person_time_leg_k`, `agentic` query loop, `obligations` join/compare; slot loop and fact chain **retired** | No per-entity fan-out with quotas; no combine operator; `agentic` unscreened | `read.decompose_v2`: rules detect decomposable shape, per-entity sub-reads with entity quotas (0 calls); combine by an evidence table the reader fills (1 call) | 43 (55 incl. open-domain) | M | **P1** |
| 11 | Dates on facts and context | Event-time on every fact and line | partial: `relative_dates_anchored` (ref), W2-14 anchor ladder (`temporal_v2`, S62), `timeview_v2` (S24), `[date]` prefix | Event time exists only as a read-time annotation on relative phrases; non-relative events get the said-date; ordering is by said-date | Fold into item 2: restate item carries `happened` + precision; render `[said ... happened ...]` | 33 (54 temporal wrong) | S (inside 2) | P2 |

**Priority key.** P1 = largest targeted count with a cheap, flag-gated first step. P2 = built-off already, screen first and add one narrow piece. P3 = small count on this benchmark or high privacy/egress cost; keep as a documented, opt-in option.

## 2. The measurements (CPU only)

All over 5,882 turns in 272 sessions of the ten conversations (mean 21.6 turns/session, 22.7 words/turn) and the 1,540 questions. "wrong" = of the 287. Rules in `evals/obs_gaps_measure.py`.

| What | Value | Method |
|---|---|---|
| Turns with an image caption | 1,226 of 5,882 (20.8%); 910 also have a URL | measured (dataset field) |
| Questions whose evidence includes a caption turn | 700 (169 wrong; 24.1% wrong against 18.6% overall) | measured (evidence ids x dataset field) |
| ... caption decisive (question stems overlap the caption but none of the spoken text) | 55 (16 wrong) | heuristic: stem-overlap(question, caption) >= 1 and stem-overlap(question, spoken) = 0 |
| Classifier `image_caption_misuse` | 6 wrong | measured (classifier label) |
| Turns with a short-form vocative of a speaker's name | 74 (1.3%) in 42 of 272 sessions | heuristic: greeting word + capitalised token that is a >= 3-letter strict prefix of a speaker name |
| ... questions whose gold turn sits in such a session / itself holds one | 289 (54 wrong) / 35 (9 wrong) | heuristic (same rule) |
| Turns with a relative-time phrase | 555 (9.4%) | heuristic: yesterday, last/next week/month/year/weekday, N units ago, recently, ... |
| Questions with a relative-time gold turn | 637 (149 wrong); temporal-category wrong with one: 40 of 54 | heuristic |
| Turns that cannot be read alone ("incomplete") | 726 (12.3%); strict subset 642 | heuristic: (third-person pronoun, deictic or back-reference with no antecedent in the turn, <= 20 words) or short follow-up question or <= 6 words or reaction opener <= 8 words |
| Windows of 6 turns / of 4 turns stride 3 containing one | 452 of 1,095 (41%) / 623 of 1,958 (32%) | heuristic (strict rule) |
| Questions with an incomplete gold turn | 98 (21 wrong); with a retrieval-stage failure (b, cf, cr): 15 | heuristic x measured stage |
| Questions where the previous turn holds the question terms but the gold turn does not | 150 (13 wrong) | heuristic: >= 2 stem overlap with previous turn, <= 1 with gold |
| Gold turns that are back-references ("you recommended", "last time") | 54 questions (17 wrong) | heuristic regex |
| Gold turns with plural pronouns and no named antecedent | 27 questions (2 wrong); 103 turns | heuristic |
| Turns using "we/us/our" | 1,010 (17.2%) | heuristic (referent ambiguous by nature) |
| Questions naming both speakers | 149 (48 wrong); with a set-operation word (both, in common, same, share, together) 82 (22 wrong); either 195 (55 wrong) | heuristic regex over speaker names and words; over-counts questions about one shared event |
| ... of the 55 multi-entity wrong, single-hop / multi-hop / open-domain / temporal | 25 / 18 / 9 / 3 | measured category |
| "How many" questions | 37 (21 wrong, 57%) | heuristic regex |
| Attribute-lookup wording (job, study, pet, family, home, hobby, status, ...) | 331 (60 wrong) | heuristic regex |
| Open-domain category | 96 (55 wrong, 57%) | measured |
| ... wrong answers that are a refusal/"not mentioned" form | 28 over all categories; 14 inside open-domain wrong | heuristic regex on the answer text |
| Inference-stem questions (would, likely, might, prefer, ...) | 71 (28 wrong, 39%) | heuristic regex |
| Gold evidence in >= 3 sessions / >= 3 turns | 129 (59 wrong) / 187 (79 wrong) | measured (evidence ids) |
| Zero content-word overlap between question and every gold turn | 271 (81 wrong, 30%) | heuristic (stem overlap, stopwords and speaker names removed) |
| Gold turn found by vector (top 10) but not BM25 (top 10) | 484 (100 wrong, 20.7%, about the base rate) | measured (logged ranks) |
| Gold turn found by BM25 but not vector | 133 (35 wrong, 26%) | measured |
| Gold turn in neither leg's top 60 | 200 (96 wrong, 48%) | measured |

Two findings change the plan. (a) **Lexical mismatch is not what is hurting**: where the vector leg finds the gold and BM25 does not, the wrong rate is the base rate (20.7% vs 18.6%). The costly group is "neither leg finds it" (48% wrong), and 25 of its wrong answers are `open_domain_inference_retrieval`: the gold evidence is indirect (an interest or habit that implies the answer), so paraphrase expansion alone would not reach it; evidence-seeking probes would. (b) **Incomplete gold turns are rare** (98 questions, 15 retrieval failures): informative turns are rarely the elliptical ones, so carry-over matters more for the quality of the memory than for this benchmark's score.

## 3. Observation 1: name variants and entity linking

**Generic capability.** Participant-scoped alias binding at write: a vocative or short form is linked to the canonical participant, so the engine does not grow a second entity "Bram" next to "Bramwell".

**What MemSpine already has.**
- `write.resolver_v2` (W2-09, off; bundled in S62 `scr-b8-write-v2`): `derive/resolve.py` (`resolve_pair`, `_person_alias`, `_given_match`, `OnlineResolver.resolve`) and `derive/resolve_stage.py:EntityResolveStage` (envelope stage order 300). Exact name, alias, nickname table (`_NICKS`, e.g. Tomasz/Tomek/Tom), spelling rules, then name-embedding cosine (`resolve_high` 0.9, `resolve_grey` 0.7). Homonyms stay apart. A bare given name against a full name is only `pending_merge` (`partial_name`, `unqualified_nickname`), "never an auto-merge" (`derive/resolve.py:713`, `:724`).
- `write.prepass` (W2-13): `derive/prepass.py:rule_hints` finds store-known aliases in turns as hints; `write.learned_persist` keeps learned aliases across restarts.
- `write.stamping_v2` (W2-02): `ingest/stamp.py:stamp` writes speaker, addressee, participants; `normalise_principal(name, aliases)` takes an alias map.
- Perspective (in ref): `models/encoders/query/perspective.py` takes the speaker from the `Name:` text prefix; no alias handling.
- Kin/possessive binding (I54, implemented): `core/rule_miner.py`.

**Gap.**
1. The resolver only sees `subject`/`object` of an extracted assertion. In the reference (no extraction) nothing is resolved. A greeting "Hey Bram!" is never turned into "the addressee is Bramwell".
2. The nickname table is a closed lexicon of formal/nick pairs. A plain short form (Bram for Bramwell) has no rule; a unique prefix of a known participant is not tried.
3. In a chat with a closed participant set a unique match is safe to bind, yet the resolver treats a bare given name as only a hint.
4. The read side never maps a query name to aliases (entity leg takes resolved ids; the perspective leg matches exact names).

**Proposed design** (`write.vocative_alias_v2`, default off). In the stamping step (`ingest/stamp.py`), after speaker/addressee are known: detect a vocative (greeting or thanks word followed by a capitalised token, or a token followed by a comma at turn start). Bind the token to a participant when (a) exact case-insensitive match, (b) both in the `_NICKS` table to the same formal name, or (c) it is a strict prefix of >= 3 letters of exactly one participant name. Set the typed `addressee` column to the canonical name, add the alias to the learned-alias registry (`learned.alias` marker) so the resolver and the prepass use it for later turns, and tag the record `alias:<token>`. Ambiguous or no match: bind nothing. Placement: write path, stamping stage (before the window segmenter). Model calls: 0. Read side (optional, `read.alias_expand`): expand a query name to its learned aliases for the BM25 leg only.
Hallucination control: only participants already in the call can be targets; the raw text is never edited; a wrong bind affects a tag, not content.

**LoCoMo evidence.** 74 turns / 42 sessions carry a short-form vocative (heuristic). Gold sits in such a session for 289 questions (54 wrong), but the gold turn itself holds the short form for only 35 (9 wrong: `wrong_detail` 4, `date_duration_arithmetic` 2, 3 others). In a two-person chat the speaker prefix already names the interlocutor, so retrieval is rarely the problem. Plausible targeted: <= 9.

**Generalisation.** Higher value outside LoCoMo: multi-party and user/assistant chats (OP-Bench personas, ConvoMem) where third parties are named by different forms. PrefEval has one user and no aliasing. MAB-CR is entity-keyed facts (aliases of one subject matter for supersede). Low overfitting risk: it is a rule on participants, no dataset vocabulary.

**Feasibility, cost, risk.** S. No model call; CPU microseconds per turn. Privacy: none added (it only reads names already stored). Risk: a wrong prefix bind in a group chat with Bram and Bramwell both present; mitigated by "exactly one participant" and exact-first order.

**Test plan.** Model-free unit tests in `tests/unit/lib/` (next to `test_lib_04_resolve.py`): (invented) participants Bramwell and Ilse; "Hey Bram!" binds addressee Bramwell; "Hey Bra!" (2 letters) binds nothing; participants Bramwell and Bram -> no prefix bind; table pair (Tomasz, Tomek) binds. Slice: `fail_retrieval` for the 9 targeted; the real check is a precision test on the 74 detected turns against a hand list (offline, local). Proposed flag `write.vocative_alias_v2`; screen **S68** (rules, no GPU needed beyond ingest).

## 4. Observation 2: decontextualising turns into standalone, dated statements

**Generic capability.** Turn restatement: a standalone statement with the speaker resolved and relative times made absolute, linked to its source turn.

**What MemSpine already has.**
- `write.window_extract=facts` (+ `window_*`): `prompts/defaults/window_extract.yaml` (v1) and `v2/window_extract.yaml`. Rules in the prompt: (3) "name people: never write he, she, they or the user ... resolve every pronoun", (5) relative time -> original expression + absolute date from the line's own date, items carry `entity`, `attribute`, `value` (self-contained), `date`, `persons`, `location`, `topic`, `turns`, `span`. Screens: S03 (`scr-window-facts-jina`), S43.
- v2 write stack, all off: `envelope_v2` (W2-06a ItemV1 -> `AssertionEnvelope`, `derive/compile.py`), `prompt_v2` (W2-06b NEW / CONTEXT / KNOWN), `temporal_v2` (W2-14 anchor ladder, `derive/temporal.py` + `derive/temporal_stage.py`: explicit date, then relative phrase against the cited turn's `source_at`, else unknown; never "now"; precision never finer than the evidence), `grounding_v2` (W2-08, `derive/ground.py`, `grounding_stage.py`), `coverage_v2` (W2-15, `derive/coverage.py`). All in S62 `scr-b8-write-v2`; S44, S45, S48 isolate parts.
- v1 miner: `consolidation.mine_facts` with `extract@session4` (rule 3 pronouns, rule 4 absolute dates; `mine_multiview` persons/location/topic), S04 `scr-gc2-facts-jina`.
- Read side, in ref: `read.resolve_relative_dates` + `relative_dates_anchored` annotate `last Friday [= Fri 2023-..]` at render; `render: dated` prefixes `[date]`.
- Retrieval keeps raw authority: `read.raw_turn_floor`, `read.facts_to_sources` (a fact's slot goes to its source turns).

**Gap.** The restated fact is a separate record that competes in the pool; the index of the turn itself never improves. No record has an index view different from its display text (no `embed_text`; `ingest/write.py:626` embeds `content`). So even with extraction on, a pronoun-heavy turn is still matched by its raw words, and a date typed in a question ("in May") never matches the turn's words. Also unmeasured: not one v2 write screen has run (the `C0-1` gate asks whether verbatim storage beats extraction; the extraction arms G-C2, G-C3, the window arm and the W2 stack are all queued, none has a verdict).

**Proposed design.** Two flag-gated steps, cheapest first.
1. `write.index_view: context` (0 model calls). Keep `content` untouched. Build a deterministic index text `"{speaker}, {absolute date of the session}, {session topic if known}: {text}"` plus the resolved-relative-date annotation the read side already computes, and embed/BM25 that. Plug-in: write path, immediately before embedding (`ingest/write.py:626`) and the lexical indexer; the stored `content`, the reader's context and all lineage are unchanged. Hallucination: none (only data already on the record).
2. The model-based restatement is the merged "turn restatement stage" of item 3 (gated, grounded). It writes a `restated` derived record (parent = the turn, `derive/write.py`), whose retrieval slot is handed to the source turn (`facts_to_sources`) so the reader still reads the raw line, now with `[said <date> | happened <date>]` when known.

**LoCoMo evidence.** 555 turns (9.4%) have a relative-time phrase; 637 questions have such a gold turn (149 wrong); 40 of the 54 wrong temporal answers do. Read-time anchoring is already in the reference, so what remains is `temporal_evidence_missing` 14 (gold not retrieved or cut), `date_duration_arithmetic` 10, `date_wrong_event` 8, `date_offset_small` 1: 33. Plausible targeted: 14 to 33.

**Generalisation.** The index view helps any benchmark with timestamps and short turns (BEAM, ConvoMem with `valid_from=now` gets nothing and that is fine: the step is skipped when no source time exists). Restatement helps PrefEval-style implicit preference turns and OP-Bench persona turns, whose statements are first-person. Overfitting risk: low for the index view; moderate for restatement if tuned on LoCoMo's two-person third-person style.

**Feasibility, cost, risk.** Index view S, 0 calls, +1 short string per embed. Restatement M, model calls per flagged window (item 3). Risk: embedding the date can pull toward same-date turns (diversity loss); screen with the date field on and off.

**Test plan.** Model-free: index text for a turn with a relative phrase equals the expected string; unknown date -> no date token; `content` byte-identical. Slices: `fail_temporal` (date classes plus matched controls, per `build_targeted_slices.py`), `fail_retrieval`. Proposed flag `write.index_view`; screen **S69**; the restate stage is **S70** (item 3).

## 5. Observation 3: context carry-over within a session (and the "when to rephrase" rule)

**Generic capability.** Gated turn completion: rewrite or link a turn only when it cannot be understood alone, citing the antecedent it depends on.

**What MemSpine already has.**
- `write.prompt_v2` CONTEXT lines (`derive/prompt_context.py:PromptContextBuilder`; `write.prompt_context_turns=3`): the last turns of the previous window are shown "to resolve pronouns; never cited". This is the only carry-over, and it is a window-edge feature, not overlap: segmentation uses "no overlap and no gap in NEW lines" (`ingest/segment.py:24`).
- Read-time carry-over: replay window 2 before / 4 after hits (ref); `read.neighbours_v2` (S19) budget-aware neighbours.
- The extraction prompt asks the model to resolve "that"/"it" from surrounding lines (rule 3).

**Gap.** There is no decision about *when*. Extraction is on for every window or off for all; there is no signal that a turn is elliptical, no antecedent edge (`refers_to`), and no check that a rewrite added nothing the lines do not support beyond the generic grounding stage.

**Proposed design: the "when to rephrase" decision rule** (`derive/standalone.py`, pure function, no model; flag `write.restate_gate`). For each turn compute the weighted sum of incompleteness signals; rephrase if the score >= 0.5, and never for a content-free turn.

| Signal | Test | Weight |
|---|---|---|
| Unresolved pronoun | third-person pronoun (he, she, they, it, them) and no capitalised entity, number or known-entity name in the turn | 0.4 |
| Deictic place or time | "there", "then", "that day/time/place", "back then", "over there" | 0.4 |
| Back-reference | "you recommended/mentioned/suggested", "last time", "as I said", "the one you ..." | 0.4 |
| Follow-up question | turn is a question, <= 14 words, wh-word or "any ..." opener, and has no noun phrase the previous turn lacks | 0.5 |
| Ellipsis | <= 6 words, or a reaction opener (wow, nice, sounds ...) with <= 14 words and a predicate that refers to "it/that/this" | 0.3 |
| Missing subject | imperative or gerund opening with first-person context only | 0.2 |
| Missing time | relative time phrase and no stamped `source_at` | resolved by rule (W2-14), no rephrase |

Overrides: a turn that holds a named entity + a verb + >= 6 words is complete (score 0); a pure reaction ("Congrats!") has nothing to carry and is *linked* to the previous turn, not rephrased; system/tool/echo turns are skipped (W2-03 tags); the previous K=3 turns of the same session are the only antecedent candidates.

Action when flagged: one `window_extract` call for the *window containing the flagged turn* (existing machinery), with the flagged lines marked, asking for `standalone` text and `refers_to` (line numbers of the antecedent lines). Grounding (W2-08 rules): every entity, number and date in `standalone` must occur in the cited lines or the KNOWN list, else the rewrite is dropped and the raw turn stays; `refers_to` lines must be in the window. Store as a derived child (`derive/write.py`, trust capped at its parents) with a lineage edge of role `context` to each antecedent; at read, `facts_to_sources` lifts the raw turn and the antecedent travels with it as a neighbour. Model calls: 0 per unflagged window; 1 per flagged window (41% of 6-turn windows on this data by the strict rule: 452 of 1,095, i.e. about 450 calls for the dataset against 1,095).

**LoCoMo evidence.** Incomplete turns 12.3%. Questions with an incomplete gold turn: 98, wrong 21; with a retrieval-stage failure: 15 (`list_aggregation_generic` 5, `implicit_or_paraphrased_gold` 2, `open_domain_inference_retrieval` 2, `temporal_evidence_missing` 2, `wrong_detail` 2, others 2). Another 150 questions have the question's terms in the *previous* turn rather than the gold turn (13 wrong). Plausible targeted: 15, at most 21.

**Generalisation.** Strong for user/assistant chat (BEAM, OP-Bench), where short replies ("yes, do that") are the norm and carry the preference. The signals are language rules written for English; `read.language_guard`-style gating is needed for other languages (the rule fails closed to "no rephrase"). Overfitting risk is low because the rule is syntactic.

**Feasibility, cost, risk.** M. Cost as above; latency on the ingest path only. Hallucination risk is the highest among the proposals, so the grounding stage is mandatory (the flag refuses to start without `write.grounding_v2`). Privacy: restated text inherits the strictest tier of its parents (`derive/write.py` rule).

**Test plan.** Model-free unit tests with invented turns: (Bram: "Wow, what happened there? Any good stories?" after Ilse: "We visited the old harbour in Tallinn last month") -> flagged, antecedent = the Ilse line; ("Congrats!") -> link only; (Noor: "I adopted a dog named Pip in March") -> complete, score 0. A grounding test: a fake rewrite adding "Riga" is dropped. Slices: new `fail_context` (gold-incomplete wrong, 21 questions, built by `build_targeted_slices.py` from the problem table) plus `fail_retrieval`. Proposed flag `write.restate_gate`; screen **S70**.

## 6. Observation 4: personal profile extraction with history

**Generic capability.** An attribute-history profile: per person, typed attributes with a cardinality, a sensitivity tier and a dated chain of values.

**What MemSpine already has.** Three pieces that are not joined.
1. v1 mining: `core/rule_miner.py:mine_rules` (English regex, no model) emits `state` slots (home, origin, job, employer, relationship status, age, education, diet, `favourite_<thing>`, partner, mother, father) and `event` slots (likes, pets, family, activities, plans); `consolidation.miner: llm` with `extract@session4` does it with a model. Rendered by `read.profile_slots_header` / `profile_subject_card` (current state per subject; `profile_sensitive` default off hides `sensitive:*` slots).
2. v2 integration: `write.integration_v2` (W2-10, `derive/integrate.py`, `integration_stage.py`; supersede retires the old value as history, coexist for cardinality many, contest) with `write.integration_cardinality` (**default `{}`**, so every attribute is undeclared: coexist or deferred to K_C), `offline.reconcile_v2` (O4-03) and `offline.histories_v2` (O4-04, `derive/histories.py:value_history`, `derive/pipeline/stages/histories.py:build_value_history`). S62, S64.
3. O4-07 profile card (`offline.profile_card_v2`, `derive/profile.py`): five sections (identity, preferences, relationships, routines, constraints), guarded ops proposed at session close (`offline.session_close_v2`, `derive/session_close.py`), bounded by `kappa_P` (24 entries), entries carry first/last confirmed and a status; S46, S64.

**Gap.**
- No default attribute registry. The owner's CV-like list (gender, location and location history, education, job, hobbies, activities, family, pets, preferences) exists only as free-text attribute names in prompts and regexes.
- `value_history` is "a projection; nothing stored" and only `patterns`/`standing` consume it. The reader is never shown "lived in A until date, then B".
- The card has 5 coarse sections and 24 slots; location history, a job chain and a pet list do not fit.
- Sensitivity: the sensitivity lexicon grades `sexuality_gender` and `health` as high (`core/sensitivity.py`), consent tiers exist (`trust/consent.py`, default-deny for sensitive and above), but a profile extractor that writes "gender" is not wired to ask for the grant first.

**Proposed design** (`offline.person_attributes_v2`, default off). (1) A declarative registry `config/templates/person_attributes.yaml`: attribute -> cardinality (`one` for current location, job, education level, relationship status; `many` for hobbies, pets, family members), kind (state/event), sensitivity tier (public / personal / sensitive; gender, health, orientation, religion = sensitive, default-deny), and consent purpose (`personalisation:profile`). It fills `write.integration_cardinality` and the miner's slot list, so rules, model miner, ladder and card share one vocabulary. (2) The sleep `histories` stage persists the chain as a derived "attribute timeline" record per (person, attribute) with parents = the dated fact records, so a read can fetch it. (3) `read.profile_history` renders, for the persons the question names, one line per changed attribute: `lives_in: A (until 2023-04) -> B (from 2023-05, said 2023-05-08)`, only for attributes the question's contract targets (`query_contract`), within `profile_budget_share`. (4) Extraction rules: only user-stated or user-confirmed evidence creates an `active` value (the W5 rule S5-2 in `derive/profile.py`); assistant-proposed values are `tentative`. Sensitive-tier attributes are extracted only when the consent registry grants `personalisation:profile`; otherwise they are neither written nor rendered. Model calls: none beyond session close call 2 (already specified, 1 per session) and the miner (1 per session); the timeline build is CPU.
Hallucination control: every value cites a span (grounding), a stated value is never overwritten by an inferred one, a conflicting equal-trust value becomes `contested` and shows both.

**LoCoMo evidence.** Attribute-lookup wording: 331 questions, 60 wrong (heuristic). Of the 33 wrong in single/multi-hop: `list_aggregation_generic` 7, `wrong_detail` 7, `list_incomplete` 5, `temporal_evidence_missing` 5, `date_duration_arithmetic` 3, others. Open-domain attribute questions (16 wrong) are item 6. Questions worded as a state change ("still", "now", "used to", "no longer", ...) are only 30 (3 wrong): this benchmark barely tests history, so the history half cannot be validated here (MAB-CR can).

**Generalisation.** This is the most transferable item: PrefEval (preference carried through filler), ConvoMem (changing-fact and user-fact types), MAB-CR (newest fact overrides), OP-Bench sycophancy/diversity (stable profile vs request). Risk of overfitting to LoCoMo: low; the registry is domain-neutral and operator-editable. Risk of harm is privacy, not accuracy.

**Feasibility, cost, risk.** M. Effort is mostly joining and rendering existing libraries. Latency: read +1 small block. Privacy: the main risk. Default-deny for sensitive tiers, `remote_llm_max_tier` stops sensitive text from reaching a remote extractor (`config/schema/privacy.py:127`), erasure planner (LIB-09) already re-derives the timeline when a source is forgotten.

**Test plan.** Model-free: with the registry, (Noor, lives_in, Lisbon, 2022) then (Noor, lives_in, Porto, 2024) yields a two-span chain, hobbies coexist, a `gender` write without a grant is refused and counted (`write.profile.denied`); render golden test. Slice: new `fail_attr` (33 questions) and the MAB-CR dev set for the history half. Proposed flag `offline.person_attributes_v2` + `read.profile_history`; screen **S71** (rides on the S64 bundle).

## 7. Observation 5: layer 3, images and world knowledge

**Generic capability.** Asset-grounded evidence, with opt-in, labelled public enrichment on top of the raw turn and the extracted fact.

**What MemSpine already has.**
- The harness folds `[image: caption]` into the turn text, so the caption is embedded and in BM25 (ref). Reader prompts `grounded_v2/v3` add "text in [image: ...] describes a photo and counts as evidence" (harness, no screen).
- W2-11 asset describer (`write.asset_describer: off|ingest|on_read`; `ingest/asset_describer.py:LLMDescriber, MetadataDescriber`; role `describe_asset`, prompt `describe_asset.yaml`): stores a reference and a trust-capped description as a derived child. E04: `ingest.assets`, `read.asset_evidence: cached|fetch`.
- `read.external_evidence` + `read.external_v2` (R3-13, S23): a separate, labelled `public` block, opt-in, never deposited (`read/external.py:plan_external`, `read/pipeline/legs.py:_external_overlay`). It is a *read-time* feature, planned only for generic or explicitly public questions; S23 is inert as configured and "skipped in B7 (no cache/provider can answer unseen questions, web means egress)".

**Gap.** (1) No write-time typing of a caption: it is just words in a turn. (2) No "relevance" note (what the photo adds to what was said). (3) Identifying a thing from a caption (the book, the landmark) needs a lookup; `external_v2` would never plan one for a personal question and takes no caption as input. (4) No separate layer record for the enrichment with its own trust and source family.

**Proposed design.** `write.caption_facts` (rule, 0 calls): when a turn text matches the caption wrapper, create an `asset_note` derived record `"<speaker> shared a photo: <caption>"` with the turn as parent, tagged `asset:caption`, so retrieval and lineage treat it as image evidence (the reader prompt then needs no special-casing). Optional `write.enrich_external` (opt-in, off): for a caption whose noun phrase is a proper noun or title (rule), build a privacy-filtered generic query (the `read/external.py` filter removes private terms), look it up through the provider port, and store the snippet as a layer-3 record, family = registrable domain, trust capped at the external cap, label `public`, parent = the caption note, rendered only in a separate block. Calls: 0 for captions; 1 lookup per enrichable caption, capped by `external_max_calls`. Never deposited into personal memory as a user fact.

**LoCoMo evidence.** 20.8% of turns have a caption; 700 questions have a caption gold turn and 24.1% of those are wrong against 18.6% overall (a modest elevation). Caption-decisive questions: 55 (16 wrong); classifier `image_caption_misuse`: 6. Plausible targeted: 16. Whether enrichment helps is not testable from the data without a lookup provider; the identifiable-title cases are few.

**Generalisation.** The caption note generalises to any multimodal log (alt text, OCR) and to OP-Bench only if its probes include images (they do not). External enrichment is the least general: it needs egress, a provider and a licence decision (owner decision already deferred web mode). Overfitting risk: the caption wrapper is the harness's own format; production use would take the asset registry instead.

**Feasibility, cost, risk.** Captions: S. Enrichment: M plus an egress/licence decision, which is the real cost. Hallucination: enrichment text is never merged into the fact; a wrong identification stays in the labelled block.

**Test plan.** Model-free: caption wrapper -> one `asset_note`, parent = turn, content has no text the turn lacks; non-caption turn -> none; external planner refuses a personal-only contract. Slice: `fail_reader` (image_caption_misuse is in `d:det`) and the 55 caption-decisive questions as a new `fail_caption`. Proposed flag `write.caption_facts` (+ `write.enrich_external`); screen **S72**.

## 8. Observation 6: inference from memory, not only retrieval

**Generic capability.** A shape-gated inference route: for "would / is it likely / prefer" asks, commit to a short answer inferred from the retrieved evidence and give the reason; plus evidence-seeking probes so the indirect evidence is retrieved.

**What MemSpine already has.**
- Reference reader prompt `grounded` already says "best-supported answer, even if indirect"; the harness adds a neutral refusal retry (`evals/memspine_evals/refusal.py`). Still 14 open-domain wrong answers are refusal forms and 26 are `inference_open_domain` (reader, gold in context).
- Harness prompt variants, none screened: `dated_infer` (`INFER_RULE`), `grounded_generic_infer` (`INFER_CLAUSE`: "do not refuse: give the most likely answer and a one-line reason ... using ordinary knowledge to bridge a gap"), `dated_world` (world-knowledge clause), `routed` (`QA_ROUTER_VERSION shape-v1`, inference variant drops the refusal and says "likely"), `grounded_v2` (yes/no rule, **regressed**: 81.5 vs 86.7 on dev, 5 gained / 17 lost, because "No," leaked into when/what answers; `readers.py:236`).
- Engine side: `read.query_contract` (A03, `read/contract.py:build_contract`) already outputs `request: recall | inference | recommendation` and answer type; `read.query_contract_use` routes it to header/rerank (S06, S11).
- Probes for indirect evidence: `read.planner=llm` + `planner_version v2/v3` (`prompts/defaults/plan@v3.yaml`, "for 'Is Ben sporty?': 'Ben fencing', 'Ben rowing'"), fused as RRF legs (`read/pipeline/router.py:1318`). `read.deep_v2` (S55, one sufficiency check + one rewrite). Built-off, unscreened.
- Not built: true HyDE leg (SOL-GAP-HYDE); a larger reader (SOL-GAP-BIGREADER).

**Gap.** Two separate defects. (a) Reader: the inference clause is not gated by question shape, so the only variants that commit also damage other shapes; nothing makes the answer *short and committed* only for inference asks. (b) Retrieval: 28 `open_domain_inference_retrieval` answers have no gold turn in any leg; the evidence is indirect (an activity implying a preference) and the question's words do not reach it.

**Proposed design.** `read.inference_mode` (default off).
1. Reader prompt (rules, 0 calls): when the contract says `request=inference` (and answer type yes/no or choice or description), append a clause: "Answer with the most plausible choice in the first words, then one short reason citing a memory line; say you cannot tell only if no memory bears on the person at all." Other shapes keep `grounded` byte-identical (this is the fix for the `grounded_v2` regression). Plug-in: compose/reader prompt, selected by `read.query_contract`; a prompt-layout-lint-clean variant under `src/memspine/prompts/defaults/` (`chat@inference.yaml` already exists for the engine's own chat role).
2. Evidence-seeking probes (1 planner call per inference read, only these): reuse `planner_version v2` restricted to inference-shaped questions: 2 subqueries naming the activities or traits that would *imply* each option ("Noor camping", "Noor hiking" for "national park or theme park?"). Fused as extra RRF legs; `rerank_keep` unchanged. Fallback with no model: a rule probe `{person} {option noun}` per option from the question's "A or B" structure.
Hallucination control: the reason must cite a memory line (a checker in `compose/answer_check.py` can drop an answer whose cited line does not exist); the committed answer is marked `inferred` in the render so downstream consumers can tell it from a stated fact; cat-5 style premise questions are guarded by the existing `gate_v2` and the contract (a person absent from the store still gets "not mentioned").

**LoCoMo evidence.** `inference_open_domain` 26 + `open_domain_inference_retrieval` 28 = 54 of 55 open-domain wrong; plus `refusal_premise_denial` 14 (10 in temporal) where a refusal replaced an answer. Inference-stem wording: 71 questions, 28 wrong (39%). Plausible targeted: 54, with 14 more partly.

**Generalisation.** High: PrefEval is the same task (apply a stored preference to a new request); ConvoMem implicit-connection and preference types; OP-Bench sycophancy and diversity. The risk is the opposite one: over-committing on OP-Bench irrelevance probes, where a refusal or no-use of memory is correct. The contract gate and the cat-5/irrelevance screens (S20, S63 style) are the guard; the adoption rule (average-led, non-inferiority on the others) applies.

**Feasibility, cost, risk.** S for the prompt, S for the probes. Latency: probes add one planner call (about 1 s on the 9B) on inference reads only (about 5% of questions). Risk: moderate (over-commitment); mitigated by the gate, the citation check and the inferred label.

**Test plan.** Model-free: contract rules classify "Would Noor prefer a national park or a theme park?" as `inference`; the prompt builder returns the clause for it and the unchanged `grounded` text for a "when did" question (golden hash test); the rule probe produces two probes; the checker drops an answer citing line 99 of 5. Slices: `fail_reader` (26+14), `fail_retrieval` (28), cat-5 `sample_slice_loc_cat5` as the over-commit guard, OP-Bench dev 60 as the sycophancy guard. Proposed flag `read.inference_mode`; screens **S73** (prompt only) and **S74** (probes).

## 9. Observation 7: session and window summaries

**Generic capability.** Multi-resolution, topic-labelled summaries that are retrievable and lift to their member turns.

**What MemSpine already has.**
- Window `episode` section (title, summary <= 50 words, narrative, participants, topics) in `window_extract` (`write.window_extract=all`); one per window; S61.
- `offline.session_close_v2` (O4-02, call 2: summary + episodes + profile ops, deterministic stitching of adjacent same-participant, topic-overlapping episodes; `derive/session_close.py:plan_session_close`) — **zero calls in arm S33**: it fires only from the window machinery, so it needs `window_extract` on (finding recorded in `screen_internals_sleep.json`).
- `read.episode_leg_v2` (R3-05c, `read/legs/episode.py:EpisodeLeg`, `read/pipeline/lift.py:323`) lifts episode hits to their member turns; S31, S61.
- `reflect_profile` + `profile_header_packing` (S01 `scr-gc9-summary-jina`): per-session summaries and observations in a header.
- Summaries per entity and community (`offline.communities_v2`, S40), multi-resolution plan (`offline.decay_v2`, S47). Extractive `read.session_digest` (N31).

**Gap.** (1) The "window of 4, overlap 1" scheme cannot be expressed: segmentation has no stride (`ingest/segment.py:24`); CONTEXT is a read-only overlap for coreference, not an independent summary window. (2) Topic labelling is on episodes (`topics`) but not required on every summary; (3) nothing measured.

**Proposed design.** `write.window_overlap_turns: int` (default 0): windows of `window_max_turns` advance by `size - overlap`; the overlapping turns are *cited-eligible* in both windows but a fact's evidence must not be double counted (dedupe by `write_id`, W2-01). Add `topic` (lower-case plural class, same convention as the facts `topic`) to the episode and session summary schemas and use it as a retrieval tag (`view_tag_leg`). Plug-in: segmenter (`LIB-14`), the episode section, the sleep summary job. Calls: windows of 4 with stride 3 is 1,958 windows on the dataset against 1,095 for 6/6 (x1.8); session-level: 272 calls. Recommendation: do not raise the overlap before S31/S33/S61 give a verdict; summaries are a retrieval *entry point*, so the first question is whether `episode_leg_v2` finds anything the turn legs miss.

**LoCoMo evidence.** Gold in >= 3 sessions: 129 questions, 59 wrong (`list_aggregation_generic` 26, `open_domain_inference_retrieval` 12, `list_incomplete` 6, `wrong_detail` 5, `multi_evidence_recall` 2, `count_wrong` 2, others 6); gold in >= 3 turns: 187 questions, 79 wrong. These are the questions that need a cross-session view; some are fixable by summaries, many need set completion (item 10). Plausible targeted: 59.

**Generalisation.** Summaries help long, topic-drifting logs (BEAM long contexts, ConvoMem) and hurt precision questions if they displace raw turns; hence "lifted to turns, never read instead of them" (the design already in place). Overfitting risk: low.

**Feasibility, cost, risk.** S for the stride and topic field; the rest is existing. Risk: summary drift (invented detail); the episode section is already grounded to cited lines and the narrative is "close paraphrase, no interpretation".

**Test plan.** Model-free: segmenter with size 4 / overlap 1 gives windows [1-4], [4-7], [7-10]; each turn is deposited once; a fact citing turn 4 gets one write id. Slices: `fail_retrieval`, `fail_list`. Proposed flag `write.window_overlap_turns`; screen **S75** (after S31/S33 results).

## 10. Observation 8: linking new turns to older facts; synonym and paraphrase expansion

**Generic capability.** Backreference linking at write and multi-probe (paraphrase) retrieval at read.

**What MemSpine already has.**
- Read-side expansion, all off: `read.planner=llm` v2/v3 subqueries (evidence-seeking, 1 call), `read.compose_rewrites` (up to two answer-free rewrites, `query_rewrite@compose`), `read.statement_probe` (N63: the question as a statement, by rules), `read.prf_expansion` (N03, pseudo-relevance feedback), `read.multi_intent_split` (G34), `read.bridge_hop` (R2-1), `read.second_round` (N04), `read.cluster_expand` (N06). `query_rewrite.yaml` is a HyDE passage prompt (E8) with no leg using it. No v2 screen id for any of these (solution map: SOL-PLANNER "no v2 screen queued").
- The embedder is semantic (Qwen3-Embedding-0.6B with the query instruction): "suggestion" vs "recommended" is a vector match, which is why the vector leg finds what BM25 misses.
- Write side: the lineage table (`write.lineage_table`, F-16a) and `context` evidence role exist; `fact_projection` links a fact to its source turn. The reference reader prompts `grounded_v2/v3` already tell the reader to "find the earlier line it refers to".

**Gap.** No rule paraphrase/synonym probe; no `HyDE` leg; no write-time backreference detection or edge ("the book you recommended" -> the earlier recommendation record); and no measurement says that lexical mismatch is the actual failure (see the measurements).

**Proposed design.** (a) `read.paraphrase_probes` (default off): up to 2 probes per read = the planner-v2 subqueries when the planner is bound, else a rule set (the `statement_probe` form, plus verb-synonym swaps from a small closed list: recommend/suggest/advise, buy/purchase, start/begin, visit/go to), fused as RRF legs at weight 0.5 so they cannot outvote the question. 0 calls by rules, 1 with the planner. (b) `write.backref_link` (default off): rule-detect a backreference phrase (the regex in the measurements), take the previous 30 turns of the same pair, retrieve the best two by the embedding of the *noun phrase after the phrase*, and write a lineage edge `refers_to` from the turn to the antecedent if the score clears a calibrated floor. 0 model calls, 1 vector query per backreference turn (54 gold turns, a few hundred in total). Read: the antecedent record rides with the hit (neighbour-style) under the same budget.
Hallucination: an edge is a pointer, never new text; below the floor nothing is linked.

**LoCoMo evidence.** Classes: `vocabulary_mismatch_recall` 4, `implicit_or_paraphrased_gold` 4, `lexical_outranked_recall` 2, `fusion_loss_lexical_only` 12 (a fusion issue, S14/S51), plus 17 wrong answers whose gold turn is a backreference. BM25-miss/vector-hit questions fail at the base rate (100 of 484), and 25 of the 96 "neither leg" failures are indirect-evidence cases (item 6). Plausible targeted: 10 labelled (+17 backreference).

**Generalisation.** Paraphrase probes help PrefEval's implicit form (the request never uses the preference's words) and ConvoMem; risk is probe noise on OP-Bench irrelevance probes (more recall = more injection of unrelated memories), guarded by the relevance gate (`gate_v2`, S20/S63). Not LoCoMo-tuned (closed verb list is domain-neutral but English-only; gate with `language_guard`).

**Feasibility, cost, risk.** S (probes by rules) to M (backref linking). Low risk.

**Test plan.** Model-free: "What did Ilse suggest?" yields a probe containing "recommended" and "advised"; probe weight 0.5 in the fusion output; the backref detector fires on an invented turn ("I finally read the book you suggested") and has a negative list ("last time" inside a quoted greeting, a question asking for a recommendation). Slice: `fail_retrieval` + `fail_fusion`. Proposed flag `read.paraphrase_probes` (+ `write.backref_link`); screen **S76**.

## 11. Observation 9: plural and pronoun enrichment

**Generic capability.** Referent annotation on facts: who a "they/we/she" is, with a count for groups.

**What MemSpine already has.** `window_extract` rule (3) forbids unresolved pronouns in item text and the schema has `persons` ("every named person involved, the entity first"); `prompt_v2` gives the model CONTEXT and KNOWN entities; the rule miner (I54) binds "my X / your X / X's Y" to the right person; the W2-13 prepass hints known aliases. Grounding checks that names and numbers in an item occur in its span.

**Gap.** A group referent ("the kids", "my coworkers") has no field: `persons` lists named people, so "they" resolves to a role noun with an unstated count and no member list. "We" resolves to speaker + unknown.

**Proposed design.** Part of the restate stage (item 3): add `referents: [{surface, resolves_to, kind: person|group|thing, count|null, source_turns}]` to the item, required only when the cited line contains a pronoun the gate flagged. Grounding: `resolves_to` must be a name or role noun in the cited or CONTEXT lines; a count must be a number or number word there; else the referent is dropped (the fact stays). Render for small readers: when a line is shown, append `[they = the kids (2)]` as an annotation next to the line, like the `[= date]` annotation, without changing stored text. Calls: none beyond the item 3 call.

**LoCoMo evidence.** 103 turns use a plural pronoun with no named antecedent in the turn; 27 questions have such a gold turn, only 2 wrong. 346 turns have an unresolved third-person pronoun; 18 questions with such a gold turn are wrong (`wrong_detail` 3, `fusion_loss_lexical_only` 2, `inference_open_domain` 2, `implicit_or_paraphrased_gold` 2, `list_aggregation_generic` 2, `temporal_evidence_missing` 2, others 5). Plausible targeted: 2 to 5 (<= 18).

**Generalisation.** More value with small readers and multi-party chats (family, team) than with the 9B on two-person logs. Low overfitting risk, low gain here.

**Feasibility, cost, risk.** S once item 3 exists. Risk: a wrong referent is a visible error; the grounding rule and "drop on doubt" bound it.

**Test plan.** Model-free: a fake item "they were scared" with CONTEXT "Ilse's two children" and referents [{they -> Ilse's children, count 2}] passes; a referent naming "Bram" absent from lines fails; render golden test of the annotation. Slice: `fail_context` (shared with S70). Proposed flag `write.restate_referents`; screen **S70b** (variant inside S70).

## 12. Observation 10: multi-entity and multi-hop questions as map-reduce

**Generic capability.** Query decomposition with per-entity retrieval and combine operators.

**What MemSpine already has.**
- `read.multi_intent_split` (G34, `models/encoders/query/shape.py:split_intents`): splits on discourse markers ("..., and where did he move?"); not on shared-predicate coordination ("both X and Y").
- `read.planner=llm` + `planner_version v3`: `persons` and `time_expr` feed `person_time_leg_k` (a structured leg of records about those persons), `aggregate` mode routes to compose with subqueries.
- `read.agentic` (I67, `core/agentic.py`; trigger `multi_hop` = several names, a relation word such as both/same/before/after, possessive chain): up to 2 `sufficiency` actions `search` / `search_person_time`. **Retired:** `read.agentic_mode=slot` (net -3 at about 8x wall time) and `read.fact_chain` (net -1); `flags.py:126`. Unscreened in v2.
- `read.deep_v2` + `read.obligations` (R3-12/R3-16, `read/obligations.py`): kinds single, join, duration, list, count, comparison, order, latest with typed holes and a follow-up retrieval suggestion per hole; S55, S56, S63 (the evidence bundle is made from keyed semantic records, so on the raw-turn reference it stays open).
- Harness: `--count-verify`, `--evidence-table`, `--verify-slots` (`evals/memspine_evals/count_verify.py`, `evidence_table.py`, `slot_verify.py`), no screen; B1 list-question design adopted `list_mode`, pool 30 and rank-tiered windows (+12/-5, net +7 on dev).

**Gap.** There is no decomposition into sub-questions, one per entity, each retrieved and answered separately, and no combine step. One pooled search for "which city have both Bram and Ilse visited" returns a mix, and the reranker sees a query that names two people, so the pool is dominated by whichever is better covered. The `agentic` loop searches again but does not guarantee a per-entity quota or a combine rule.

**Proposed design** (`read.decompose_v2`, default off). 
1. **Detection (rules, 0 calls).** A question is decomposable when `build_contract` finds >= 2 targets (capitalised names matching the store's participants or the contract's `subjects`) and one of: set operator (both, in common, same, share, together, either, neither), comparison (more than, older, first, earlier than, compared), count over entities (how many ... did each), or temporal order between two entities' events (before/after/first). Else it is a normal read.
2. **Map.** One sub-read per target entity: the original question with the other entity removed and the shared predicate kept ("Which city has Bram visited?"). Each sub-read is a standard `Engine.read` with the person filter (`person_time_leg_k` / perspective subject weight), top-k/n of the budget (entity quota), so one entity cannot crowd the other out. Latency in parallel (`read.parallel_legs`).
3. **Reduce.** Code, not the model, builds an evidence table: rows = (entity, candidate value, source line, date), values extracted from the sub-read's top hits by the existing slot/list machinery. Combine operators implemented in code on normalised values: *intersection* (both/in common), *union* (either/all), *difference* (neither/only), *compare* (ordered by date or magnitude), *count* (distinct merged values, the `count_verify` rule), *before/after* (by event date, W2-14/`[= date]`). The reader gets the table plus the lines and is asked to answer from the table's rows (1 call, as now).
4. **Fallback.** If an operand has no row, the answer states which operand lacks evidence (an `obligations` hole), never a guess.
Model calls: 0 extra for detection and map (retrieval only); 1 reader call as today; the optional LLM variant (planner v3 `persons`) adds 1 call. Placement: read spine, after the request/contract stage and before the pool is cut (`read/pipeline/router.py`), with the combine in compose.
Hallucination control: the table's rows quote their source lines; the combine is deterministic; the reader may not add a row.

**LoCoMo evidence.** 195 questions name both speakers or use a set-operation word (heuristic; 149 + 82 minus overlap), 55 wrong; by category single-hop 25, multi-hop 18, open-domain 9, temporal 3. Classes of the 55: `wrong_detail` 11, `list_aggregation_generic` 11, `open_domain_inference_retrieval` 7, `fusion_loss_lexical_only` 4, `gold_error` 4, others 18. Count questions: 37, 21 wrong (57%), `count_wrong` 9 + `list_aggregation_generic` 4. Multi-hop wrong total 94. Note the heuristic includes questions about one shared event ("what did Bram and Ilse do together"), where a single read is right; the detection rule must exclude them (a shared-event verb with no set operator: no decomposition). Plausible targeted: 43 (single/multi-hop), at most 55.

**Generalisation.** Multi-entity questions are rare in BEAM/ConvoMem single-user logs, common in multi-party logs and in MAB-CR (multi-hop fact consolidation); the rule is neutral to dataset words (names come from the participant set). Overfitting risk: moderate for the operator vocabulary (English); gate with `language_guard`. The retired slot loop is a warning: a decomposition that adds latency without a gain gets removed, so the first step is the 0-call version.

**Feasibility, cost, risk.** M. Reads that decompose cost n sub-reads (each ~0.2 to 0.5 s without the reranker; with Jina the pool rerank dominates, so cap n at 3 and share one rerank pass). Risk: entity extraction errors from the question; mitigated by requiring both entities to be participants or known entities.

**Test plan.** Model-free: detector on invented questions ("Which city have both Bram and Ilse visited?" -> intersection of {Bram}, {Ilse}; "What did Bram and Ilse do together?" -> not decomposed); combine operators on small tables (intersection, union, difference, count merges the same event on the same date, before/after on dates); entity-quota test (no sub-read takes more than its share). Slices: `fail_list` + a new `fail_multi` (43 questions) built from the problem table. Proposed flag `read.decompose_v2`; screen **S77**.

## 13. Observation 11: dates on facts and context

**Generic capability.** Event-time and said-time on every fact and every context line, with precision.

**What MemSpine already has.** In ref: `[date]` per line (said-time), `resolve_relative_dates` + `relative_dates_anchored` + `resolve_durations` (read-time `[= date]` for relative phrases, deterministic rules in `models/encoders/query/temporal_resolve.py:annotate`), `order_by_time_for_ordering`, `temporal_leg_mentions`. Reader prompts `dated3`/`grounded` explain said vs happened. Off: W2-14 anchor ladder (`temporal_v2`, S62 on the `fail_temporal` slice: precision and basis recorded, relations "before/after" without dates), `read.timeview_v2` (R3-14, S24 as-of reads), `temporal_relative`, `temporal_infer_year`, `temporal_leg_event_dates` (S24, S62, S63), `read.span_line` (N10 duration line), harness `--duration-solve`, `--milestones`, `--date-repair` (I76/E05/I57, no screen), `gap_markers`.

**Gap.** For a non-relative event ("I went in June", "since 2019") the only date a line carries is the said-date; the happened-date is computed from relative phrases at read time and is lost once the line leaves context (nothing stored). Event time is not on the record unless W2-14 runs on an extracted item, and then it lives on the fact, not on the turn the reader sees. Ordering by event time across facts is therefore best effort.

**Proposed design.** Folded into item 2/3: the restate item carries `happened` (ISO, precision, basis `explicit|relative|inferred|unknown`) from the existing anchor ladder (no new rules) and the renderer shows `[said 2023-05-08 | happened 2023-05-07]` for restated lines and the raw `[2023-05-08] ... [= Sun 2023-05-07]` for raw ones. For the answer generator: a compact "timeline" block for questions whose contract has a time view of `order` or `duration` (sorted events with dates; `span_line` generalised to n events). 0 extra calls.

**LoCoMo evidence.** Temporal category: 321 questions, 54 wrong (16.8%, below average). Classes: reader date/duration arithmetic `d:dar` 19, `temporal_evidence_missing` 14, `refusal_premise_denial` 10, `gold_error` 9, other 2. The two reader-side groups already have harness fixes (`--duration-solve`, `--date-repair`) with no screen. Plausible targeted: 33.

**Generalisation.** Dates matter wherever there are timestamps (BEAM temporal, MAB-CR ordering, LoCoMo-Plus); ConvoMem and PrefEval have none (skip). Overfitting risk: the "week before" convention (G13) is LoCoMo's own; keep it behind `relative_dates_anchored`.

**Feasibility, cost, risk.** S inside item 2. Risk: a wrong happened-date shown with authority; precision label and `inferred` basis shown in the render.

**Test plan.** Model-free: existing LIB-01 tests plus golden render of the two-clock line; ordering test on a three-event table. Slice: `fail_temporal`. Proposed flag `write.restate_v2` renders `happened`; screen **S70** (shared); the timeline block `read.timeline_block` is a variant **S78**.

## 14. Cross-cutting points

**Generalisation and overfitting.** Features that read words (regexes for pronouns, deictics, backreferences, set operators, relative time) are English-only. Each new rule must (a) be behind `read.language_guard`/`write` equivalents and fail closed, (b) carry its fired-rate in the tap, (c) be screened on a dataset other than LoCoMo before adoption (PrefEval, ConvoMem, OP-Bench, MAB-CR; the existing adoption rule in `evals/analysis/GENERALISATION_AUDIT.md` section 4.3). None of the proposals uses a LoCoMo name, noun or phrase; the examples above are invented.

**Privacy and consent.** Profile attributes (item 4), restated text (items 2, 3, 9) and enrichment (item 5) all create *derived* personal data. Rules shared by all: derived records inherit the strictest tier and purpose of their parents (`trust/privacy.py`); sensitive and special-category attributes are default-deny and need a consent grant (`trust/consent.py`); `remote_llm_max_tier` keeps sensitive text off remote models; external lookups carry only generic terms and never deposit; erasure re-derives (LIB-09).

**Model-call budget** (full 10-conversation dataset, 5,882 turns, 272 sessions, from the window arithmetic above):

| Step | Unit | Calls on the dataset |
|---|---|---|
| vocative alias, index view, caption note, paraphrase rules, decompose detection | per turn / read | 0 |
| restate gate (6-turn windows, strict rule) | flagged window | about 450 (of 1,095) |
| restate ungated (6/6) / overlapped (4, stride 3) | window | 1,095 / 1,958 |
| session summary and profile ops (call 2) | session | 272 |
| inference probes | inference-shaped read | about 70 reads (heuristic 71 questions) |
| external enrichment | enrichable caption | <= `external_max_calls` (default 20) |

## 15. Ranked roadmap

Ranking = targeted wrong answers x probability that the first step is cheap and safe, discounted by egress/privacy cost.

1. **Observation 6, `read.inference_mode` (S73, S74). Build first.** 54 targeted, the first step is a shape-gated prompt clause with no model call and a golden test; it fixes the exact defect that sank `grounded_v2` (a clause applied to every shape). Probes reuse `plan@v2`. Generalises to PrefEval and ConvoMem. Guard with the cat-5 and OP-Bench irrelevance slices.
2. **Observation 10, `read.decompose_v2` (S77).** 43 to 55 targeted, rules-only first (entity quotas, combine operators in code); the `count_verify` and `evidence_table` harness flags are the combine ancestors. Do not repeat the retired slot loop: no extra model call until the 0-call version has a verdict.
3. **Merge observations 2, 3, 9, 11 (and the rule half of 1) into one "turn restatement and enrichment" stage.** One gated `window_extract` item schema with `standalone`, `refers_to`, `referents`, `happened`; rule-based incompleteness gate (the table in section 5); grounded by the existing W2-08 stage; index view as its 0-call precursor. First run the queued screens S03, S43, S44, S62 (`fail_temporal`) as written, because nothing in the write v2 stack has a verdict yet; then build `write.index_view` (S69) and the gate (S70). Do not build the restatement stage before S62 reports.
4. **Observation 4, `offline.person_attributes_v2` (S71).** 33 targeted on LoCoMo, but the most transferable; it is joining three existing pieces (registry, history rendering, consent) rather than new extraction. Validate the history half on MAB-CR. Needs the privacy default-deny test before any code reaches an extractor.
5. **Observation 7, stride and topic field (S75), after S31/S33/S61.** Small build; decide on the overlap only if the episode leg finds evidence the turn legs miss.
6. **Observation 8 (S76), paraphrase probes by rules.** Cheap, but the measurements say lexical mismatch is not the driver; run it as part of the S73/S74 probe screen rather than alone.
7. **Observation 5 (S72).** Captions as typed evidence (rule) is cheap; world-knowledge enrichment needs an egress and licence decision and has the smallest benchmark effect (16 targeted). Keep opt-in and last.
8. **Observation 1 (S68).** Rule alias binding is small and safe; its LoCoMo effect is at most 9. Build with the stamping work (it rides on `stamping_v2`).

**What to build first, in order:** (i) `read.inference_mode` prompt gate + golden tests; (ii) `read.decompose_v2` detector and combine operators with unit tests; (iii) `write.index_view` and `derive/standalone.py` (the gate, pure and unit-tested, no flag effect until the stage exists); (iv) the attribute registry file plus its consent tests. None needs the GPU to be written; each becomes a screen after the current GPU queue frees.

**Not recommended now.** Another read-time fact chain or slot loop (retired with negative net); a larger reader as an observation fix (GAP, a separate decision); enrichment before captions; raising the window overlap before the episode leg has a verdict.

## 16. Files

- `evals/obs_gaps_measure.py`: the measurement script (no dataset text in the file; text-bearing output goes to the gitignored `evals/runs/_analysis/obs_gaps/`).
- `docs/v2/data/obs_measurements.json`: the counts above.
- `docs/v2/data/observations.json`: one object per observation for the Screen Atlas.
- Proposed screens S68 to S78 and flags are provisional names for the coordinator to renumber; none is added to `v2_queue.tsv` or `packages.py` by this analysis.
