# R14 engine survey: how other memory engines solve each generic memory problem (RES-ENGINES)

Tag `R14` marks everything new in round 2026-10-14. `R14-confirmed` marks an existing gap or screen that a surveyed engine confirms or sharpens. Companion data: `docs/v2/research/r14_engines.json`.

## Method and limits

- Starting point: the committed code-traced records `docs/engine_survey/<slug>.json` (13 engines, 57 rows each, with upstream `file:line` evidence and pinned commits), `evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md` (Graphiti + SimpleMem), `docs/FRAMEWORK_TOOLCALL_SURVEY.md`, `docs/v2/SCREEN_ATLAS.html`, `docs/v2/LOCOMO_PROBLEM_MAP.md`, `evals/analysis/GAP_REGISTER.md`.
- The Source column cites `docs/engine_survey/<slug>.json#<row id>`; that row's `e` field holds the upstream file and line (tagged read or doc there). Web sources are cited by URL (Mastra, Supermemory, Mem0 docs, Memobase PyPI and DeepWiki, A-Mem arXiv, Zep arXiv and blog, Graphiti repo and issues, Letta blog).
- Not re-read in this round: upstream code of engines without an `engine_survey` record (Zep/Graphiti beyond the combined-engine document and the cited files, Letta, Memobase, A-Mem, LangMem, Supermemory, Mastra, SimpleMem). For those only public pages were read, and the tables cite the URLs. Supermemory, Mastra, Zep and Letta figures are vendor reports. LangMem and Letta archival memory were not examined beyond the Letta benchmark post. Nothing was run: no model calls, no GPU.
- Evidence counts come from the stage/class labels of `clean-jina-full` (287 wrong of 1,540); they are class sizes, not capture estimates. Heuristic classes (H in the problem map) are noted. Items without a LoCoMo class are marked heuristic with count 0. Question ids are in the JSON.
- Licence: no LoCoMo text appears in this file or the JSON; ids are `conv:index` only.

## 1. Turn rewriting and decontextualisation at ingest

Question: Does the engine rewrite turns into standalone facts, and from what unit (turn, window, episode)?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mem0 (OSS v3) | ADD-only 15-80-word self-contained facts from both speakers; sees top-10 existing memories; relative dates resolved against an Observation Date, but OSS refuses a caller timestamp so replayed history is dated with the run date | write | 1 per add() batch | docs/engine_survey/mem0.json#extract.facts; docs/engine_survey/mem0.json#extract.dates |
| Nemori | Batch LLM segmenter, then per-group third-person episode narrative with absolute dates in parentheses; then predict-calibrate extracts only the facts the prediction missed | write | 1 segment + 1 episode + 2 (predict, extract) per group | docs/engine_survey/nemori.json#extract.episode; docs/engine_survey/nemori.json#extract.neighbours |
| EverOS / EverMemOS | Two stages: episode text per MemCell first (dual date 'last Friday (2023-05-12)'), atomic facts then extracted from the episode, stored with parent_id; the episode is the retrieved unit, facts ride along | write | 1 boundary + 1 episode + 1 facts per cell | docs/engine_survey/evermemos.json#extract.episode; docs/engine_survey/evermemos.json#extract.facts |
| Supermemory | Session chunked into semantic blocks; atomic memories resolve ambiguous references per chunk; the original chunk stays attached and is injected on a hit | write + read | not published | https://supermemory.ai/research/longmembench (3.1, 3.4) |
| SimpleMem / combined engine | 'Lossless restatement' per entry: third person, no pronouns, relative to ISO absolute; speaker labels discarded by the builder | write | 1 per window (live: 1 turn) | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.3 |
| Hindsight | Narrative facts with what/when/where/who/why in one text; event date in the prompt, occurred_start/end returned; entities and causal links from the same call | write | 1 per chunk | docs/engine_survey/hindsight.json#extract.facts |
| Honcho | Explicit atomic 'conclusions', pronouns resolved, peer id as subject, absolute dates from message time | write (deriver) | 1 per 512-token unit | docs/engine_survey/honcho.json#extract.facts |
| LightMem | Standalone facts with source_id per topic segment; LoCoMo prompt separates mention time from event time; entry_type tag is lost on insert (defect) | write | 1 per buffer | docs/engine_survey/lightmem.json#extract.facts; docs/engine_survey/lightmem.json#extract.episode |
| MemOS | Long self-contained third-person statements (key/value/tags); window summary copied into a 'background' field of each fact | write | 1 per 1,024-token window | docs/engine_survey/memos.json#extract.facts; docs/engine_survey/memos.json#extract.episode |
| Mastra Observational Memory | Observer turns raw messages into dated, prioritised observations that REPLACE the messages; 3-6x compression | write (background) | 1 per token threshold | https://mastra.ai/research/observational-memory |
| MIRIX | Per-batch episodic event with LLM occurred_at; harness prepends date-resolution instructions | write | multi-agent, 1 per type per batch | docs/engine_survey/mirix.json#extract.dates |
| MemSpine | window_extract: atomic dated facts quoting a verbatim span, rejected if uncited; window facts alone lost on LoCoMo (S03 -4, S04 -6); S62 bundle queued | write | 1 per window (target 0.2-0.3 per turn) | docs/v2/SCREEN_ATLAS.html; docs/engine_survey/memspine.json#extract.facts |

**What MemSpine has.** Window facts (`write.window_extract=facts`), envelope with one repair, grounding check, temporal anchoring stage W2-14, coverage re-extract W2-15, episode section (window mode `all`). Read-time `[= date]` annotation. Lift derived hits to source turns (`read.lift_before_rerank`, S15). Screens S03/S04/S43-S45/S62.

**The gap.** MemSpine writes facts straight from raw turns and retrieves them as competing records; the two best vendor LoCoMo systems (EverOS 93.5, Mem0 platform 92.5) retrieve a larger unit (episode, or memory plus source chunk) and use facts as keys. The window-facts arms regress on their own, so the open question is the unit that is returned, not the quality of the fact text. The replay-date defect (Mem0 OSS, Memori, Nemori facts) is a known trap that MemSpine must keep tested.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-decon-1 | H | ingest | 59 q (measured) | Problem: Derived facts are retrieved as stand-alone records and lose to raw turns; the retrieval unit and the index key are the same object. Engines that win use facts (or lossless restatements) as KEYS to a larger parent unit (episode or source chunk) and return the parent. Gap: No arm combines window facts as index keys with parent-window lift and MaxSim ranking; S03/S04 test facts as competing records and lose. Hypothesis: Rank the parent window by max similarity over its facts (MaxSim) plus the raw window, then return the parent window with its facts as annotations; this removes the fact-versus-raw-turn competition. Expected: recover part of the implicit, vocabulary and temporal recall misses on the GC sample (hypothesis +2 to +4 net), no loss on lexical questions because the raw-window leg stays. | **Facts as keys, parent lift (EverOS VECTOR method)** [EverOS: docs/engine_survey/evermemos.json#read.legs; docs/engine_survey/evermemos.json#rank.fusion; cost 0 extra calls at read; write cost unchanged (window_extract already runs); risk EverOS's own header calls the cross-encoder net negative on hybrid episodes; test with and without the Jina rerank]; **Contextual atomic memory plus attached source chunk (Supermemory)** [https://supermemory.ai/research/longmembench 3.1/3.4; Nemori appends raw turns of its top-2 episodes: docs/engine_survey/nemori.json#answer.shape; cost 0 calls; more tokens (budget 4,096); risk token budget; MemSpine is at 672 tokens/q so headroom exists] | New GC arm: S43 window facts + `maxsim` leg + S15 lift; screen on fail_retrieval and fail_temporal slices; compare with S62. A J run is inert (needs derived layers). |
| R14-decon-2 | M | ingest | 23 q (measured) | Problem: The replay timestamp must reach the extractor for every line. Three engines silently lose it: Mem0 OSS refuses a caller timestamp and dates facts with the run date; Memori facts carry only ingest time; Nemori facts get wall-clock created_at and the reader then sees it as the fact time. Gap: No invariant test that no derived record falls back to wall-clock time when the source turn has a session date. Hypothesis: A property test over the write path (fake clock far from the data dates) finds zero derived records whose valid_from or rendered date equals the wall clock; any hit is a bug that costs temporal accuracy. Expected LoCoMo effect: none if clean; protects every replayed benchmark. | **Fake-clock invariant test** [Negative lessons: docs/engine_survey/mem0.json#extract.dates; docs/engine_survey/memori.json#extract.dates; docs/engine_survey/nemori.json#extract.dates; cost 0 model calls (fake extractor); risk none] | Unit test in tests/ with FakeClock and a fake extractor; no GPU. |
| R14-decon-3 | L | ingest | 4 q (measured) | Problem: Restatement erases who said it. SimpleMem discards speaker labels; LightMem groups facts per speaker in the prompt; MemOS ingests each conversation once per speaker perspective so search sees both views. Gap: R14-confirmed: MemSpine's perspective layer already covers this; the only open item is the proposed 'answer provenance check' (no key yet). Hypothesis: R14-confirmed I59/I63. No new arm: keep the speaker in every rendered fact line and in the fact's cited span. MemOS's double-perspective ingest corresponds to our perspective leg, already in the 81.4 arm. | **Per-speaker perspective ingestion (MemOS script) and grouped speaker blocks (LightMem)** [docs/engine_survey/memos.json#eval.rigour; docs/engine_survey/lightmem.json#answer.shape; cost 0; risk already covered] | None new; reuse the speaker_confusion/distractor slice. |
| R14-seg-1 | M | ingest | 0 q (heuristic) | Problem: Window boundaries ignore the calendar-day or session change that the best segmenters cut on first (EverOS prompt: calendar-day change is the highest-priority split, then topic shift, task done, gap over 4 h; hard cap 65,536 tokens or 500 messages). MemSpine's topic-shift test is 'never tested across a session change'. Gap: R14-confirmed S45: the cut order (session/day first, topic second) is only a screened-for-cost setting. Hypothesis: Cutting on session change before topic shift removes cross-session windows, so every fact gets one unambiguous date; measured as fewer facts_ungrounded and fewer facts whose cited lines span two dates. Cost neutral. | **Day/session-first boundary order** [docs/engine_survey/evermemos.json#segment.rule; Nemori LLM rule docs/engine_survey/nemori.json#segment.rule; Honcho token gate docs/engine_survey/honcho.json#segment.rule; cost 0; risk more, smaller windows means more calls; measure calls per turn] | Count cross-date windows in stored LoCoMo ingest logs (CPU) before and after S45. |

## 2. Entity, alias and coreference linking

Question: How are mentions linked to one identity, and where does coreference get resolved?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Hindsight | Rule-based, no LLM: pg_trgm candidates; score = 0.5 name similarity + 0.3 selectivity-weighted co-occurrence + 0.2 temporal proximity (7-day decay); merge at >= 0.6 | write | 0 | docs/engine_survey/hindsight.json#graph.resolution |
| Graphiti / Zep | Speaker always the first node; deterministic exact/fuzzy first with an entropy gate on short names; LLM dedupe only for holdouts; edge dedupe search restricted to edges between the same entity pair | write | 1 extract + LLM only for holdouts | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.3; https://arxiv.org/html/2501.13956 (2.2.2-2.2.3) |
| Mem0 | spaCy mentions (no LLM); merge on exact normalised text or top-1 cosine >= 0.95; entity-to-memory back-pointers only | write | 0 | docs/engine_survey/mem0.json#graph.resolution |
| Cognee | Deterministic name-based ids merge same-name entities; fuzzy merge is an explicit memify pipeline (0.85) | write / memify | 0 (default) | docs/engine_survey/cognee.json#graph.resolution |
| Honcho | Peers only (client-declared); aliases are free text in the peer card | n/a | 0 | docs/engine_survey/honcho.json#graph.resolution |
| Memori | sha256 of lower-cased alphanumerics; non-Latin names collapse to one empty key (defect) | write | 0 (hosted NER) | docs/engine_survey/memori.json#time.dedup |
| Supermemory | Atomic memories resolve ambiguous references inside a chunk; no entity table described | write | n/a | https://supermemory.ai/docs/concepts/graph-memory |
| MemSpine | Ladder: exact, alias table, top-15 embedding candidates, entropy gate, MinHash Jaccard >= 0.9, then one resolve_entity@batch call per 50 names; trust-gap merges become contested; GLiNER2 option | sleep | 0 + 1 per 50 unresolved | docs/engine_survey/memspine.json#graph.resolution |

**What MemSpine has.** Full ladder, rules/llm resolve modes, entity nodes with NFKC casefold ids, kin/possessive resolution against the speaker (I54), window coreference in the extractor prompt. Entity leg (S13, S32) is inert until the GC arm builds entities.

**The gap.** The ladder is at least as complete as any surveyed engine. The one signal MemSpine lacks that a surveyed engine uses is context overlap (shared neighbours, co-occurrence, temporal proximity) as a merge score. LoCoMo has a near-fixed cast, so the effect there is small; it matters for open-cast benchmarks (OP-Bench, MAB, BEAM).

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-ent-1 | L | ingest | 0 q (heuristic) | Problem: String and embedding similarity alone merge or split entities wrongly when names are short or share a surname; co-occurrence and time proximity disambiguate without a model call. Gap: No context-overlap score in the rules tier; ambiguous names go straight to the LLM batch. Hypothesis: Add a weighted co-occurrence and temporal-proximity term to the rules tier (Hindsight weights 0.5/0.3/0.2 as the starting point) and measure the fraction of names sent to the LLM batch; expected: fewer resolve_entity calls at equal merge precision on a hand-built alias fixture. No LoCoMo accuracy effect expected. | **Context-weighted resolution score (Hindsight)** [docs/engine_survey/hindsight.json#graph.resolution; cost 0 calls; risk thresholds tuned on one corpus; use only as a rules-tier pre-filter] | Synthetic alias fixture plus the entity logs of S02; count LLM batch calls. |

## 3. Temporal normalisation: absolute dates, event time versus record time

Question: Which clocks does a record carry, and how is time used at read?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mastra OM | Every observation carries observation date, referenced date (date inside the content) and a computed relative offset; grouped under dated headings | write | 1 per threshold | https://mastra.ai/research/observational-memory (Temporal anchoring) |
| Mem0 platform | Separate temporal pass reads each new memory with its source conversation and date: event time, ongoing versus completed, precision, memory type (event, state, plan, preference, relationship, absence) | write | extra pass per batch (platform only) | https://docs.mem0.ai/core-concepts/memory-evaluation |
| Supermemory | documentDate (source authored) and eventDate (event happened) on each memory | write | n/a | https://supermemory.ai/research/longmembench 3.3 |
| Hindsight | event_date, occurred_start/end, mentioned_at, created_at; temporal arm = window overlap, semantic choice inside the window, proximity boost +/-10% at rerank | write + read | 0 at read (dateparser) | docs/engine_survey/hindsight.json#time.fields; docs/engine_survey/hindsight.json#read.temporal |
| Graphiti / Zep | Bi-temporal edges valid_at/invalid_at/created_at/expired_at from a reference time; the formatted context printed to the reader omits the validity dates | write | within edge extraction | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md headline 5 |
| Cognee | Timestamp leaf nodes normalised to YYYY, YYYY-MM, YYYY-MM-DD; TEMPORAL search: one LLM call extracts a query interval, rerank by date-window overlap | write + read | 1 at read | docs/engine_survey/cognee.json#read.temporal |
| Mnemon | Session date stored on every event and value; relative dates resolved by the reader from the conversation date | write | 0 | docs/engine_survey/mnemon.json#extract.dates |
| MIRIX | LLM occurred_at; query temporal phrase parsed by rules into a date range relative to wall-clock now (not the conversation date) | read | 1 topic call | docs/engine_survey/mirix.json#read.analysis |
| MemOS | reference_time declared on the request but never read in the OSS tree | read | 0 | docs/engine_survey/memos.json#read.temporal |
| MemSpine | valid_from/valid_to/recorded_at/superseded_at, as_of reads, absolute-span temporal leg (relative spans opt-in), read-time [= date], duration and milestone solvers (harness) | write + read | 0 at read | docs/engine_survey/memspine.json#time.fields; docs/engine_survey/memspine.json#read.temporal |

**What MemSpine has.** Three clocks (S48), W2-14 temporal anchoring, `read.temporal_relative`/`temporal_infer_year`/`temporal_leg_event_dates` (S24/S62/S63), I79 annotator, I57 date repair, I76 duration solver, E05 milestones.

**The gap.** MemSpine has the richest clock set. Missing: a precision tag and an ongoing/completed status next to the event time (Mem0 platform), a referenced-date versus observation-date split in the rendered line (Mastra), and an in-window semantic selection with a proximity boost instead of a hard span filter (Hindsight).

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-time-1 | M | ingest | 33 q (measured) | Problem: A date alone does not say how precise it is or whether the state is still running; arithmetic and 'how long' questions need both. Gap: No precision (day/week/month/season) and no status (ongoing/completed/planned) on the temporal envelope. Hypothesis: Adding `precision` and `status` to the W2-14 envelope lets the duration solver pick floor versus nearest and say 'about'; expected to shift part of the duration-arithmetic class (hypothesis +1 to +3 net on the date slice) at 0 extra calls, because the fields come out of the existing window call. | **Precision, status and type in the temporal pass (Mem0 platform)** [https://docs.mem0.ai/core-concepts/memory-evaluation (Temporal Reasoning); cost 0 extra (same window call); risk 9B schema adherence; the envelope validator already repairs once]; **Three-date line (observation, referenced, relative)** [https://mastra.ai/research/observational-memory (Temporal anchoring); cost 0; risk longer lines] | S62 arm with the envelope fields; compare on fail_temporal and the date_* classes. |
| R14-time-2 | M | retrieve | 14 q (measured) | Problem: Read-time relative phrases must be anchored to the question or reference date, not the wall clock. MIRIX parses against wall-clock now; the combined engine's planner does the same; Hindsight takes a query_timestamp. Gap: R14-confirmed I8 (anchor never tested): no guard that the temporal parse path never calls the system clock. Hypothesis: A static test (no datetime.now or time.time in the temporal-query and query-shape modules) plus a fake-clock test keeps relative phrases correct under replay; zero accuracy change if already clean. | **Explicit reference_time on every read** [docs/engine_survey/hindsight.json#read.analysis; negative: docs/engine_survey/mirix.json#read.analysis; docs/engine_survey/memos.json#read.temporal; cost 0; risk none] | Unit test; no GPU. |
| R14-time-3 | M | retrieve | 14 q (measured) | Problem: A hard window filter drops partially matching evidence; the engines that do best choose inside the window by relevance (not recency) and then add a bounded proximity boost. Gap: Window used as a leg; no proximity boost at rerank and no neutral-for-non-temporal rule. Hypothesis: Add a +/-10% proximity factor to the rerank score only when a window was parsed (neutral otherwise); expected to recover part of the 8 temporal fusion cuts without hurting non-temporal questions (guard: PARITY on non-temporal items). | **Window overlap plus proximity factor (Hindsight)** [docs/engine_survey/hindsight.json#rank.rerank; docs/engine_survey/hindsight.json#read.temporal; cost 0; risk boost size tuned on one corpus; stay within the stated +/-10%]; **LLM interval extraction for unsure phrases (Cognee TEMPORAL)** [docs/engine_survey/cognee.json#read.temporal; cost <=1 call on unsure questions; risk 9B parse errors] | Screen on fail_temporal; report flips on non-temporal control items. |

## 4. Personal profile and attributes with history

Question: Is there a per-user profile, how is it updated, and does it keep history?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Memobase | Profile slots topic/sub_topic/content with dates; separate event timeline with gists; buffer flush at about 1,024 tokens or 1 h idle, never in the hot path | write (background) | 1 per flush | https://pypi.org/project/memobase/0.0.12 ; https://deepwiki.com/memodb-io/memobase |
| Honcho | Peer card per observer-observed pair; 4 prefixes (IDENTITY/ATTRIBUTE/RELATIONSHIP/INSTRUCTION), 200 chars x 40 entries, written only by the dream deduction specialist | background (dream) | 1 per dream | docs/engine_survey/honcho.json#derive.profile |
| MemMachine | Profile features (category, tag, feature, value) over 37 fixed tags; update shows up to 50 old features and the LLM returns ADD/DELETE; consolidation once 20+ per tag; old values destroyed | write (background) | 1 per >=5 messages or 5 min | docs/engine_survey/memmachine.json#extract.neighbours; docs/engine_survey/memmachine.json#time.update |
| EverOS | Per-owner profile (explicit_info, implicit_traits) via add/update/delete/none ops, 60-item cap; the LoCoMo run extracts it but never reads it | write | 1 per update | docs/engine_survey/evermemos.json#derive.profile |
| Mnemon | Changeable values keyed 'thing . attribute' with dated history and a conflict flag; directives shown first | consolidation | 1 per 10,000-char batch | docs/engine_survey/mnemon.json#extract.facts; docs/engine_survey/mnemon.json#time.fields |
| MIRIX | Core blocks human/persona (5,000 chars) rewritten by an agent, always injected | write | 1 core agent call | docs/engine_survey/mirix.json#derive.profile |
| MemU | Agent-curated topic markdown files; no profile object in HEAD | agent | host agent | docs/engine_survey/memu.json#derive.profile |
| MemSpine | profile_card_v2 (no read consumer, S46 skipped), standing answers for keyed state facts (S34), value histories (S42), persona pinned | sleep | 0-1 per entity | docs/engine_survey/memspine.json#derive.profile |

**What MemSpine has.** `offline.profile_card_v2`, `offline.standing_v2` + `read.standing_v2`, `offline.histories_v2`, `read.profile_header_packing`, persona pin.

**The gap.** The card is built but unread; standing answers and histories exist but are unscreened on a keyed-state arm. Only Mnemon and Memobase keep dated values; MemMachine and MIRIX destroy old values (documented defect).

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-prof-1 | M | compose | 62 q (measured) | Problem: Attribute questions ('what is X's ...', 'did X ever ...') are answered from whichever raw turn ranks first; a keyed value with its dated history gives the current value and the change. Gap: R14-confirmed: the card has no read consumer. Missing is the shape: a compact 'slot: value (date), previous value (date)' block selected by the contract subject. Hypothesis: Render a value-history block only when the query contract names a subject and a relation matching a keyed slot (Mnemon fold model); expected: small gain on attribute and 'changed' questions, no change elsewhere; hypothesis +0 to +3 net on the 80 sample. | **Keyed value plus dated history block (Mnemon)** [docs/engine_survey/mnemon.json#extract.facts; docs/engine_survey/mnemon.json#answer.shape; cost 1 call per consolidation batch (sleep); risk LoCoMo has few changeable values; 9B key stability]; **Slot grammar with prefixes and character caps (Honcho)** [docs/engine_survey/honcho.json#derive.profile; cost sleep only; risk low] | S34+S46 wiring on gc3 with a read.profile_header consumer; slice: wrong_detail single-hop. |
| R14-prof-2 | L | ingest | 0 q (heuristic) | Problem: Destructive profile edits (MemMachine DELETE+ADD, MIRIX rewrite) leave no history, so 'what was it before' is unanswerable. Gap: Confirmed MemSpine is ahead; keep the invariant that a profile edit supersedes, never deletes. Hypothesis: Add a regression test: any profile or card update leaves the prior value readable with as_of; no accuracy change expected. | **Supersede-not-delete invariant** [docs/engine_survey/memmachine.json#time.update; docs/engine_survey/mirix.json#time.update; cost 0; risk none] | Unit test. |

## 5. Session, topic and episode summaries

Question: What higher-level units exist, who produces them, and what is retrieved?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Nemori | LLM topic episodes (2-15 turns), title + narrative + event timestamp; merge of same-event episodes; top-2 episodes also expose raw turns to the reader | write | 1 per group + merge calls | docs/engine_survey/nemori.json#extract.episode; docs/engine_survey/nemori.json#time.dedup |
| EverOS MemCell | Episode per cell; summary field (200-character slice in the pinned version); retrieved unit = episode, facts nested | write | 1 per cell | docs/engine_survey/evermemos.json#extract.episode |
| Honcho | Rolling recursive summaries every 20 messages (<=1,500 tokens) and 60 (<=6,000); degenerate output (<35% distinct 4-grams) rejected | write | 1 per 20/60 messages | docs/engine_survey/honcho.json#derive.session |
| MemMachine | Short-term rolling summary replaces evicted turns | write | background worker | docs/engine_survey/memmachine.json#derive.session |
| Memori | Rolling conversation summary, overwritten each update, attached to recalled facts through the fact-mention table | write | hosted | docs/engine_survey/memori.json#extract.episode |
| MemOS | 120-200-word window summary copied into the 'background' of each fact; no summary node | write | same call | docs/engine_survey/memos.json#extract.episode |
| Graphiti | Session summary 250 words updated each user turn; full rebuild every 20; user summary from 10 episodes | write | 1 per turn | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.3 |
| LightMem StructMem | Cross-event summaries over time windows from 15 related seeds, 200-350 words, in a separate collection (manual call) | offline | 1 per window | docs/engine_survey/lightmem.json#derive.session |
| Mastra OM | No retrieval: observations replace history; Reflector condenses past a second threshold | background | 1 per threshold | https://mastra.ai/research/observational-memory |
| MemSpine | session_close_v2: summary + episodes per closed session (>=3 turns), extractive fallback, idempotent by membership fingerprint; episode_leg_v2 lifts episode hits to member turns | sleep | 1 per session | docs/engine_survey/memspine.json#derive.session |

**What MemSpine has.** S01/S31/S33/S61/S64 queued; call-2 content (stitched narrative) still a stub.

**The gap.** Two ideas are not in MemSpine: (a) render the parent summary next to each retrieved fact; (b) a stable, dense, always-in-context log instead of retrieval, the Mastra design, which reports 84.2 (gpt-4o) to 94.9 (gpt-5-mini) on LongMemEval-S and keeps the prefix cacheable.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-epi-1 | M | compose | 60 q (measured) | Problem: A retrieved fact or turn lacks the situation it came from; engines attach it (MemOS 'background', Memori summaries linked to recalled facts, Supermemory source chunk). Gap: Lift goes episode to turns; the reverse (turn or fact to a one-line parent summary) is not rendered. Hypothesis: Render at most one 25-word parent-episode line under the top-3 hits; expected to help inference and implicit questions at 60-80 extra tokens per question (we are at 672); hypothesis +1 to +3 net on the GC arm. | **Fact plus parent summary line** [docs/engine_survey/memos.json#extract.episode; docs/engine_survey/memori.json#answer.shape; cost 0 at read (summary built at sleep); risk token budget; duplicated summaries] | GC arm S33 plus a render flag; compare tokens and flips. |
| R14-epi-2 | L | ingest | 0 q (heuristic) | Problem: Small local models emit looping summaries; Honcho rejects outputs with <35% distinct 4-grams and keeps the previous summary. Gap: No repetitiveness guard on the summarize role. Hypothesis: A distinct-4-gram floor on summary output removes degenerate summaries; expected effect: fewer bad L2 records on a 9B role, measured as rejected count. | **Degeneracy guard** [docs/engine_survey/honcho.json#derive.session; cost 0; risk none] | Unit test with a looping fake output. |
| R14-epi-3 | M | compose | 110 q (measured) | Problem: Retrieval cannot return what the query does not name. Mastra drops retrieval entirely: a dated, prioritised observation log of the whole history (3-6x compression, about 30k tokens average context on LongMemEval-S, stable prefix) beat the oracle with gpt-4o. Gap: Nothing places a conversation-wide dated log in the context as a stable prefix; our packing is a 300-token header. Hypothesis: A priority-tagged dated observation log capped at about 1.5-2k tokens as a stable prefix, plus normal retrieval, covers recall-stage misses where the gold is a short event line. Risk is large: Mastra shows gpt-4o loses 10 points on temporal and 27 on preference versus gpt-5-mini with the same log, so a 9B reader may not use dense logs. Treat as a screen, not an expectation. | **Observer/Reflector log (Mastra OM)** [https://mastra.ai/research/observational-memory (Methodology); cost 1 observer call per threshold plus reflector calls (sleep side); risk reader size; prompt budget 4,096] | New GC arm 'obs-log'; slice fail_retrieval (b+cf) and the 80 sample; measure reader use by whether cited lines come from the log. |

## 6. Linking new facts to old ones

Question: How is a new fact connected to what is already stored (dedupe, supersede, extend, graph link)?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mem0 | Top-10 existing memories shown to the extractor; the extractor's linked_memory_ids are never read; exact-text MD5 dedupe | write | 0 extra | docs/engine_survey/mem0.json#extract.neighbours |
| Nemori | Predict-calibrate: the episode retrieves top-10/20 existing facts; the model predicts the episode from them; extraction keeps only what the prediction missed | write | 2 calls | docs/engine_survey/nemori.json#extract.neighbours |
| A-Mem | Per new note: LLM picks links among top-k neighbours, then 'memory evolution' rewrites the neighbours' context and tags | write | 2+ calls per note | https://arxiv.org/html/2502.12110v2 |
| Hindsight | memory_links: temporal (<=24 h, weight 1-gap/24h), semantic kNN (top 50, cos >= 0.7), caused_by from the extraction call; expansion = tanh(0.5 x shared entities) + link weights | write (no extra LLM) + read | 0 extra | docs/engine_survey/hindsight.json#graph.edges; docs/engine_survey/hindsight.json#read.traversal |
| Supermemory | New memory linked to existing by updates / extends / derives; isLatest keeps retrieval on the current fact | write | n/a | https://supermemory.ai/docs/concepts/graph-memory |
| MemOS (reorganise) | CAUSE/CONDITION/RELATE/CONFLICT edges; top-5 neighbours above 0.8 LLM-judged contradictory, redundant or independent | background, off by default | 1+ per node | docs/engine_survey/memos.json#graph.edges |
| Mnemon | Fold prompt lists existing threads and every state key with its latest value; the model reuses ids and repeats a key to record a change | consolidation | 1 per batch | docs/engine_survey/mnemon.json#extract.neighbours |
| Graphiti | Edge candidate search restricted to the same entity pair; LLM returns duplicate and contradicted indices; deterministic interval rule invalidates | write | 1 per edge batch | https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py |
| MemSpine | Asserted LINK + lineage DAG; conflict ladder (13 rungs); entity graph in sleep; G-W4 (extractor sees earlier facts) not built | write + sleep | 0 / sleep | docs/engine_survey/memspine.json#extract.neighbours |

**What MemSpine has.** par(m) lineage, ladder, extract_graph (sleep), neighbours_v2 (temporal neighbours only), fact chain at read (E01, opt-in).

**The gap.** Two gaps. (1) No LLM-free semantic/temporal link table over facts to expand from seeds (Hindsight), so multi-evidence recall depends on extract_graph LLM calls. (2) G-W4: the extractor does not see keys of earlier facts, so same-key supersession is not model-driven.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-link-1 | H | retrieve | 70 q (measured) | Problem: Multi-session evidence needs a hop from a seed hit to related stored items; a graph built by an LLM per session is expensive and unscreened, while a kNN + time + shared-entity link table costs only embeddings. Gap: No write-time, LLM-free link table over facts/turns and no summed-signal expansion score. Hypothesis: Precompute for each record its top semantic neighbours (cos >= 0.7, cap 50) and 24 h temporal neighbours with weights; at read expand one hop from the top-20 seeds and score tanh(0.5 x shared-entity count) + semantic + temporal weights. Expected to add a few of the multi-evidence/list recall misses on the fail_retrieval slice at zero model calls. Copy Hindsight's guards: at most 200 targets per entity and a timeout that drops the entity leg. | **Link-table expansion (Hindsight)** [docs/engine_survey/hindsight.json#graph.edges; docs/engine_survey/hindsight.json#read.traversal; cost 0 model calls (embeddings exist); risk kNN links duplicate the vector leg (Hindsight notes convergent signals win); the expansion must not crowd the pool (see R02 reserved slots)] | CPU prototype over stored embeddings of one conversation; then a GC arm on fail_retrieval and fail_mix_120. |
| R14-link-2 | M | ingest | 25 q (measured) | Problem: Without the keys of earlier facts, the extractor cannot reuse an identity, so the same state or event gets a new key at each mention; Mnemon shows only keys and latest values, so key reuse is the supersession signal. Gap: R14-confirmed G-W4. Sharpening: show keys, not texts (token-cheap), restricted to entities in the window; delta-only output as in Nemori. Hypothesis: Adding up to 20 known keys with latest values to the window call lets the extractor repeat a key for an update; expected: fewer duplicate-key facts and more UPDATE verdicts on the ladder at about 150 prompt tokens per window; no LoCoMo accuracy claim. | **Known-keys context (Mnemon)** [docs/engine_survey/mnemon.json#extract.neighbours; cost 0 extra calls; risk 9B may over-reuse keys; the ladder trust gate protects]; **Predict-calibrate delta extraction (Nemori)** [docs/engine_survey/nemori.json#extract.neighbours; cost +1 call per window; risk cost; Nemori facts stay append-only] | Count duplicate-key facts and UPDATE verdicts on gc3 write logs, before and after. |
| R14-link-3 | L | ingest | 8 q (measured) | Problem: A-Mem 'memory evolution' rewrites the context and tags of neighbours when a new note arrives, so older records gain the vocabulary of later ones. Gap: No neighbour-enrichment step. Hypothesis: At sleep, for clusters touched by new facts, regenerate keywords for the oldest member; expected tiny. A-Mem reports multi-hop gains with weak models but costs 2+ calls per note, so low priority. | **Memory evolution at sleep (A-Mem)** [https://arxiv.org/html/2502.12110v2; cost 1 call per touched cluster; risk drift of old records; lineage must keep the original text] | Not before R14-link-1. |

## 7. Query rewriting, expansion and decomposition at read

Question: What happens to the question before search?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mnemon | Planner writes 3 searches worded as a stored record would word them plus 1 invented answering note (HyDE); a parallel need-plan lists <=3 needs, 'ALL' for count/list; the loop pages while new positives appear | read | 2 planner + judge calls | docs/engine_survey/mnemon.json#read.rewrite; docs/engine_survey/mnemon.json#read.analysis |
| EverOS | Agentic: sufficiency call; if insufficient 2-3 queries (keyword, question, HyDE statement). LLM_MULTIROUND decider: up to 3 sub-queries x 3 rounds | read, opt-in | 1-4 calls | docs/engine_survey/evermemos.json#read.rewrite |
| MemMachine | Agent mode: ToolSelect to SplitQuery / ChainOfQuery (<=3 rewrites after sufficiency) / direct; agent 91.69 versus memory mode 91.23 on 1,540 q, 5,566 versus 2,727 tokens/q | read, opt-in | 2-5 calls | docs/engine_survey/memmachine.json#read.rewrite; docs/engine_survey/memmachine.json#eval.models |
| MIRIX | Forced-tool call turns the conversation into ';'-separated topics plus a temporal phrase; topics become the BM25 query | read | 1 call | docs/engine_survey/mirix.json#read.rewrite |
| MemOS (fine) | TaskGoalParser: rephrased query, keys, tags, extra memories to embed; optional sub-query split | read, opt-in | 1+ | docs/engine_survey/memos.json#read.rewrite |
| Cognee | Rule router (no LLM); COT follow-ups; DECOMPOSITION; temporal interval call | read | 0-4 | docs/engine_survey/cognee.json#read.rewrite; docs/engine_survey/cognee.json#read.analysis |
| Graphiti / combined | Standalone reformulation from the last <=5 exchanges (unvalidated); planner emits <=2 queries + keywords; reflection optional | read | 1-2 | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.4 |
| Mem0, Nemori, LightMem, Memori, Hindsight, Honcho | No rewriting on the default read path | - | 0 | docs/engine_survey/mem0.json#read.rewrite; docs/engine_survey/nemori.json#read.rewrite; docs/engine_survey/hindsight.json#read.rewrite |
| MemSpine | Rules shape/contract, optional LLM planner subqueries, compose_rewrites, bridge hop, second round; all off in the 81.4 arm; read.hyde_leg proposed only | read | 0 (ref) / 1 | docs/engine_survey/memspine.json#read.rewrite |

**What MemSpine has.** `read.planner=llm`, `compose_rewrites`, `second_round`, `agentic_mode query|slot` (slot net -3 at about 8x cost), `read.deep_v2` sufficiency, typed query contract. No screen is queued for planner/rewrites ('no screen' in the problem map).

**The gap.** Most top engines run no rewrite on the default path; the ones that do are agentic variants whose LoCoMo gain is small (MemMachine +0.46 points for about 2x tokens). The one rewrite style MemSpine lacks and can specify exactly is a rewrite shaped like its own stored record (dated third-person fact).

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-qry-1 | H | retrieve | 41 q (measured) | Problem: The question and the stored record use different vocabulary and person; the rewrite should be in the store's own register (record-styled searches plus an answer-hypothesis note). Gap: R14-confirmed: the HyDE leg is only proposed and no screen exists for the planner. New detail: style rewrites like the stored fact format, which MemSpine controls through window_extract output. Hypothesis: One local-LLM call returns 3 searches phrased as a dated third-person fact plus one hypothetical answer sentence; each becomes a vector+BM25 leg in the leg-protected pool. Expected: recover a share of the recall-stage misses in the four classes (hypothesis +2 to +4 net on fail_retrieval) at 1 call per question. | **Record-styled multi-search plus HyDE note (Mnemon)** [docs/engine_survey/mnemon.json#read.rewrite; docs/engine_survey/mnemon.json#read.legs; cost 1 call/q; risk rewrites hallucinate entities; guard with a named-entity retention check]; **Keyword + question + HyDE trio on insufficiency (EverOS)** [docs/engine_survey/evermemos.json#read.rewrite; cost 1-2 calls on a fraction of q; risk needs the deep_v2 sufficiency role] | Planner arm on the fail_retrieval slice with the leg-protected pool; PARITY check on a right-answer control. |
| R14-qry-2 | M | retrieve | 62 q (measured) | Problem: List and count questions need every instance. Mnemon marks such needs ALL and keeps paging the search while new positive records appear; the stop rule is the useful part. Gap: R14-confirmed I67/E02. Sharpening: a 'page until no new positive' loop, not a free-form slot loop, bounded at 3 pages. Hypothesis: For ALL-typed questions (contract cardinality many/count) fetch up to 3 extra pages of the main query and stop when a page adds no record the pre-judge accepts; expected cost at most 3 reranks and 0-1 extra LLM call. | **ALL-need paging (Mnemon)** [docs/engine_survey/mnemon.json#rank.agentic; docs/engine_survey/mnemon.json#read.analysis; cost reranks + judge; risk Mnemon's judge is a proprietary model; ours is the 9B] | fail_list slice (exists); compare with the agentic slot arm. |
| R14-qry-3 | L | retrieve | 0 q (heuristic) | Problem: Dependent chat questions need a standalone rewrite; the combined engine returns the original on error but does not check equivalence or emptiness. Gap: Only chat benchmarks (OP-Bench, ConvoMem, PrefEval) need it; LoCoMo questions are standalone. Hypothesis: Rewrite only when the question contains an unresolved pronoun or deictic; validate named-entity and number retention, else keep the original. No LoCoMo effect expected. | **Validated standalone reformulation** [evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.4; cost <=1 call on triggered q; risk none] | OP-Bench dev slice. |

## 8. Multi-hop and graph traversal

Question: How does the engine reach evidence that the question does not name?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Hindsight | One-hop link expansion from <=20 semantic seeds; <=200 targets per entity, 10 s timeout drops the leg; no multi-hop walk | read | 0 | docs/engine_survey/hindsight.json#read.traversal |
| Cognee | Graph projection around vector hits, k-hop from <=10 seeds; CONTEXT_EXTENSION; NL to Cypher | read | 0-6 | docs/engine_survey/cognee.json#read.traversal |
| Graphiti | Edges: BM25 + cosine + BFS depth 3, RRF; nodes optional; communities retrieved but not formatted | read | 0 | evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.4 |
| MemMachine | No graph; contextual retrieval expands timeline neighbours (expand_context 3 in the LoCoMo script); CoQ/RaRag for multi-hop | read | 0 / agent | docs/engine_survey/memmachine.json#read.traversal |
| EverOS | No graph; agentic cluster-scoped expansion | read | opt-in | docs/engine_survey/evermemos.json#read.traversal |
| Honcho | get_reasoning_chain walks the premise-to-conclusion DAG; no entity traversal | read (agent) | agent loop | docs/engine_survey/honcho.json#read.traversal |
| Mnemon | Bottom-up 'light-up' of topics and values linked to the View's records; re-read linked records (<=8) | read | judge calls | docs/engine_survey/mnemon.json#read.traversal |
| MemSpine | Entity BFS depth 2 (cap 3), degree 50, <=10 hits, trust-gated; PPR opt-in; bridge hop; cluster expand; all off in the reference | read | 0 | docs/engine_survey/memspine.json#read.traversal |

**What MemSpine has.** graph_leg_v2/legs_v2/edge_compiler_v2 (S30, S38, S39), PPR, bridge hop, fact chain (E01), second round.

**The gap.** Surveyed engines do at most one hop; none shows a multi-hop gain on LoCoMo specifically. MemSpine already exceeds them in options, but they are all unscreened or inert on J runs.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-hop-1 | L | retrieve | 42 q (measured) | Problem: Agentic multi-round retrieval is the usual answer to multi-hop; the only paired vendor comparison shows a very small LoCoMo gain for roughly double the context. Gap: R14-confirmed: our slot loop net -3 at about 8x cost matches MemMachine agent versus memory mode (91.69 vs 91.23; 5,566 vs 2,727 tokens/q). Hypothesis: Do not prioritise agentic loops for LoCoMo; one-hop link expansion (R14-link-1) and one sufficiency check plus one re-search (deep_v2) are the only variants worth the cost. | **Single sufficiency check plus one re-search (EverOS agentic, MemMachine CoQ capped)** [docs/engine_survey/evermemos.json#rank.agentic; docs/engine_survey/memmachine.json#rank.agentic; cost 1-2 calls on a fraction; risk small gain] | S55/S63 already queued. |

## 9. Answer prompts: inference versus refusal

Question: What does the reader prompt allow when evidence is partial?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| EverOS (benchmark) | 7-step chain-of-thought incl. a time-calculation step; 'do not dismiss reasonable inferences'; up to 32,768 output tokens | reader | 1 | docs/engine_survey/evermemos.json#answer.prompt |
| Mem0-derived (Nemori, LightMem, MemMachine, MemOS) | Prefer the most recent memory on conflict, convert relative to absolute dates from memory timestamps, <= 5-6 words, step by step | reader | 1 | docs/engine_survey/nemori.json#answer.prompt; docs/engine_survey/lightmem.json#answer.prompt; docs/engine_survey/memos.json#answer.prompt |
| Mem0 proxy | Tells the model not to say no information was found | reader | 1 | docs/engine_survey/mem0.json#answer.prompt |
| Mnemon / combined engine | Answer from memory; exact 'not mentioned' string if unsupported; an empty context returns the abstention with no call | reader | 0-1 | docs/engine_survey/mnemon.json#answer.prompt; evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.4 (6) |
| Supermemory | 'I don't know' if context insufficient; question date given; start from memory titles then chunks | reader | 1 | https://supermemory.ai/research/longmembench (Reproducing) |
| MIRIX (harness) | Verify, aggregate lists, stop after 2-3 searches, 'unknown' only after three failed searches, otherwise guess; LoCoMo-specific examples | reader agent | <=6 | docs/engine_survey/mirix.json#answer.prompt |
| Letta filesystem agent | Agent decides what to search and how often over stored files; 74.0 with gpt-4o-mini | agent | tool loop | https://www.letta.com/blog/benchmarking-ai-agent-memory/ |
| MemSpine | Clean grounded prompt (post-I80), neutral refusal retry, date repair, duration solver; reader variants dated_infer / routed_generic exist but have no screen | reader (harness) | 1-2 | docs/engine_survey/memspine.json#answer.prompt |

**What MemSpine has.** `--retry-refusal`, `readers.py` variants (dated_world, dated_infer, grounded_detail, routed_generic), `--verify-answer`, verify-slots diagnosed, I80 gold-lint.

**The gap.** The inference-permitting vendor prompts are part of why their open-domain scores are high, and several are paired with lenient judges. MemSpine has the variants but never screened them: the two big reader classes (inference, refusal) list 'no screen'.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-ans-1 | H | reader | 40 q (measured) | Problem: When the evidence supports an inference but not a quote, a grounded prompt refuses; EverOS, Mem0 and MIRIX prompts push toward a reasoned answer, and a structured evidence-then-compute-then-answer layout helps date and inference questions. Gap: R14-confirmed: no screen. New detail: the 3-step layout (list the evidence lines with dates; compute; answer) as a prompt variant, on a thinking-off 9B. Hypothesis: routed_generic with a 3-step layout recovers part of the reader-side inference/refusal misses without raising wrong answers on right ones (guard: PARITY on a 60-question right-answer control); hypothesis +3 to +6 net on the 40-question slice. | **Seven-step CoT trimmed to three steps (EverOS)** [docs/engine_survey/evermemos.json#answer.prompt; cost 0 extra calls; more output tokens; risk prompt contamination (I80): run gold-lint; same-model judge bias (A1)]; **Larger reader (27-32B)** [evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md headline 4 (their Qwen3-32B reader); Mastra: reader size moves OM from 84.2 to 94.9; cost GPU memory; risk blocked by GPU memory] | Prompt screen on the fail_reader slice; report flips and refusal rate. |
| R14-ans-2 | M | reader | 0 q (heuristic) | Problem: Abstention policy is set per engine without a shared test: exact string, 'I don't know', never abstain, guess after N searches. Category 5 (446 questions in the full set, excluded by most vendors) is where this matters and MemSpine has never measured it. Gap: R14-confirmed I2/I29. Hypothesis: Use a conditional abstain: empty context allowed only when the gate finds no evidence AND no named entity is present; measure cat5 correctness and category 1-4 non-regression on the same arm; S20 is expected to move cat5 (stated in the atlas). | **Empty-context abstention without a model call (combined engine; Memori threshold 0.1)** [evals/analysis/COMBINED_ENGINE_COMPARISON_2026-10-11.md 1.4 (6); docs/engine_survey/memori.json#rank.gate; cost 0; risk named-entity bypass needed (I74)] | sample_slice_loc_cat5 plus OP-Bench dev; S20. |

## 10. Image and multimodal memory

Question: How do image turns become memory?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mem0 | With enable_vision the LLM describes image parts before extraction; without it image turns are silently dropped | write | +1 vision call | https://docs.mem0.ai/open-source/features/multimodal-support ; docs/engine_survey/mem0.json#extract.facts |
| Hindsight | Images/files inline in retain content; separate vision slot only for chunks with attachments (1,500 chars per image, <=8 per chunk); facts keep a link to the attachment | write | vision call per attachment chunk | docs/engine_survey/hindsight.json#extras (multimodal inline attachments) |
| MemOS | Image/file/URL parsers (markitdown, vision LLM) | write | vision call | docs/engine_survey/memos.json#extras (Tool, skill and multimodal memory) |
| EverOS | Optional parser turns image/PDF/audio/video into text with a multimodal LLM | write | parser calls | docs/engine_survey/evermemos.json#extras (Multimodal ingest) |
| MIRIX | Screenshot ingestion in the paper; the accumulator is not wired into the server | write | n/a | docs/engine_survey/mirix.json#extras (multimodal / screen ingestion) |
| Zep (benchmark critique) | LoCoMo image questions sometimes unanswerable because the BLIP caption lacks the detail | data | - | https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory |
| MemSpine | E04: dataset's own image URLs fetched once, hash-cached, vision port (qwen2.5vl:3b); read.asset_evidence cached/fetch; reader caption-line variant | read | vision call at read | evals/analysis/GAP_REGISTER.md E04 |

**What MemSpine has.** `ingest.assets: on`, `read.asset_evidence`, `asset:<hash>` provenance, blob cache.

**The gap.** MemSpine resolves the original image at read time; every surveyed engine that handles images does it at ingest. A silent-drop path (Mem0) is the main pitfall.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-mm-1 | M | ingest | 6 q (measured) | Problem: Reading the image when asked costs a vision call on the latency path and only helps asked-about images; an ingest-time description stored once is indexed and searchable. Gap: Cached mode exists; there is no ingest-time derived record 'image description' with asset provenance that is embedded and searchable. Hypothesis: Store a short vision description per image as a derived record linked to the turn (asset:<hash>), indexed by both legs; expected to fix some caption-misuse items and a share of image-related recall misses, cost one vision call per image at write. | **Ingest-time image description record (Mem0 / Hindsight attachments)** [https://docs.mem0.ai/open-source/features/multimodal-support ; docs/engine_survey/hindsight.json#extras; cost 1 vision call per image (once, cached); risk Mem0 trap: missing vision config must record assets_skipped, never drop silently] | Unit test for the skipped path; the caption-misuse slice is small, so add the image questions of the full set. |

## 11. World-knowledge tools

Question: Do memory engines call external knowledge, and where does it enter?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| MemOS | internet_search intent parsed in fine mode; web results (OuterMemory) shown in a separate block; internet leg off by default | read | 0-1 | docs/engine_survey/memos.json#answer.shape; docs/engine_survey/memos.json#read.legs |
| Cognee | Natural-language-to-Cypher and agentic tool loop with skills; no web tool on the default path | read, opt-in | loop | docs/engine_survey/cognee.json#rank.agentic |
| Hindsight reflect | Tool-calling agent over its own memory only (mental models, observations, recall) | read, opt-in | <=10 iterations | docs/engine_survey/hindsight.json#rank.agentic |
| Letta | Agent-defined tools (files, search); no fixed world tool | agent | tool loop | https://www.letta.com/blog/benchmarking-ai-agent-memory/ |
| Zep open-domain result | Highest open-domain F1 49.56 / J 76.60 in the Mem0 paper table, from memory plus LLM prior, no web | - | - | https://arxiv.org/pdf/2504.19413 (open-domain paragraph) |
| MemSpine | E03: wikipedia provider (keyless), privacy filter, cache, call budget, separate [public knowledge] block that never proves a private fact; S23 skipped (no provider, network egress) | read, opt-in | <=N | evals/analysis/GAP_REGISTER.md E03 |

**What MemSpine has.** E03 external evidence (off), reader prompt world-knowledge clause (`dated_world`).

**The gap.** No surveyed engine reports a web tool in a LoCoMo run; the leading open-domain scores rely on the reader's prior. MemSpine's separation of public from private evidence matches MemOS's design.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-wk-1 | L | reader | 54 q (measured) | Problem: Open-domain questions need world knowledge to combine with a personal fact; the lever is mostly the reader's prior and the recall of the personal fact, not an external lookup. Gap: R14-confirmed: keep E03 skipped; no vendor evidence supports a tool gain. Hypothesis: Order of effect for the open-domain misses: recall of the personal line (R14-qry-1, R14-link-1) > reader prompt/size (R14-ans-1) > external tool. The labels already split them into recall-stage and reader-stage, which checks this ordering. | **Separate public-knowledge block (MemOS pattern)** [docs/engine_survey/memos.json#answer.shape; cost 0 (already built); risk egress] | None new. |

## 12. Conflict and knowledge update

Question: How are contradicted or changed facts handled, and when?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| Mem0 | Extraction is ADD-only; both versions survive; no supersession | write | 0 | docs/engine_survey/mem0.json#time.update |
| Graphiti / Zep | LLM returns contradicted indices per new edge; a deterministic rule sets invalid_at = new valid_at and expired_at = now. Known issues: a non-reasoning judge silently misses contradictions (#1666); invalidation searches the whole graph and dates retirements to ingest time (#1728) | write | 1 per edge batch | https://github.com/getzep/graphiti/issues/1666 ; https://github.com/getzep/graphiti/issues/1728 |
| Hindsight | Raw facts append-only; a consolidation LLM per batch of <=8 facts creates/updates/deletes observations: prefer update, cascade a state change, keep 'previously X, now Y' | background | 1 per batch | docs/engine_survey/hindsight.json#time.update |
| Supermemory | updates / extends / derives relations; isLatest keeps retrieval on the current fact; its own report lists 100% on knowledge update | write | n/a | https://supermemory.ai/docs/concepts/graph-memory ; https://supermemory.ai/research/longmembench |
| Mnemon | Value appended with a conflict flag; at read an LLM judge withdraws 'no longer current' records (p >= 0.5) and lists them | write + read | judge calls | docs/engine_survey/mnemon.json#time.update |
| LightMem | Offline update queue (top 10 of 20 neighbours); LLM update/delete/ignore, hard delete | offline | 1 per entry over threshold | docs/engine_survey/lightmem.json#time.update |
| Cognee | Opt-in 'contradicts' edge (non-destructive) and temporal resolution for declared functional relations | write, opt-in | 1 per touch | docs/engine_survey/cognee.json#time.update |
| Honcho | Dream deduction specialist updates/deletes, old value not kept | dream | 1 per dream | docs/engine_survey/honcho.json#time.update |
| Mastra OM | Reflector removes superseded observations | background | 1 per threshold | https://mastra.ai/research/observational-memory |
| MemSpine | Deterministic 13-rung ladder: NOOP/ADD/UPDATE/INVALIDATE/CONTEST, 7 rungs on by default; LLM judge escalation and cross-key invalidate opt-in | write | 0 / opt-in 1 | docs/engine_survey/memspine.json#time.update |

**What MemSpine has.** Ladder with trust gate, retract tags, interval order, histories (S42), reconcile (S41), contest state, as-of reads.

**The gap.** MemSpine's key-scoped deterministic design avoids the whole-graph search flaw (#1728). The open risk is the optional LLM judge: Graphiti's report shows a small non-reasoning judge fails silently, and MemSpine's judge is a local 9B.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-upd-1 | M | ingest | 0 q (heuristic) | Problem: A contradiction judge that returns an empty list is indistinguishable from 'no contradiction'; on small models this silently leaves stale facts. Gap: No recall self-test and no judge_empty counter for the escalation path. Hypothesis: Seed 30 known contradicting pairs through the escalation with the local 9B; if recall < 0.8 keep escalation off and rely on the deterministic rungs. Expected: a measured go/no-go for I17, which has no metric today. | **Judge recall self-test plus judge_empty metric** [https://github.com/getzep/graphiti/issues/1666; cost 30 judge calls once; risk none] | MAB-CR blind slice; unit test for the counter. |
| R14-upd-2 | L | compose | 0 q (heuristic) | Problem: Read-time 'no longer current' withdrawal (Mnemon) and prefer-observations dedupe (Hindsight) both remove stale or covered records at render; MemSpine's dedupe before assembly screened INERT on LoCoMo (I31). Gap: R14-confirmed I31: inert on LoCoMo; only matters with real updates. Hypothesis: Re-screen dedupe/prefer-consolidated on MAB-CR, not on LoCoMo. | **Withdraw covered or superseded records at render** [docs/engine_survey/hindsight.json#answer.shape; docs/engine_survey/mnemon.json#answer.shape; cost 0 (Hindsight) / judge calls (Mnemon); risk Mnemon uses a proprietary judge] | MAB-CR blind split. |

## 13. What each engine reports on LoCoMo and LongMemEval, and with which judge

Question: Can the numbers be compared with MemSpine's 81.4 (Qwen3.5-9B reader and judge, cat 1-4, 1,540 q)?

| engine | approach | where in pipeline | model calls | source |
|---|---|---|---|---|
| MemSpine | 81.4 (reference); official F1 42.0; Qwen3.5-9B Q4 as reader AND judge, thinking off; 672 tokens/q (Jina) to 1,571; errata with both headlines; cluster bootstrap | local | - | docs/engine_survey/memspine.json#eval.models; docs/engine_survey/memspine.json#answer.size |
| EverOS | 94.42 / 93.51 / 93.05; gpt-4.1-mini reader, gpt-4o-mini judge; lenient judge ('touches on the same topic'), max over judge runs; 2,296-2,596 tokens | vendor | - | docs/engine_survey/evermemos.json#eval.rigour |
| MemU | 92.09; gpt-4.1-mini reader, gpt-4.1 judge hard-coded, profile injected whole; v1 harness, not HEAD | vendor | - | docs/engine_survey/memu.json#eval.rigour |
| Mem0 platform | 92.5 at top_200, 6,956 tokens; models unstated; OmniMemEval independent run 77.68 | vendor / independent | - | docs/engine_survey/mem0.json#eval.rigour |
| Hindsight | 92.0 (leaderboard, Gemini reader and a generous Gemini judge); paper 85.67-89.61 with a GPT-OSS-120b judge; about 24-36k tokens | vendor | - | docs/engine_survey/hindsight.json#eval.rigour |
| MemMachine | 91.69 agent / 91.23 memory; gpt-4.1-mini + gpt-4o-mini judge; paid Cohere rerank | vendor | - | docs/engine_survey/memmachine.json#eval.rigour |
| Mnemon | 91.7 standard / 89.2 without consolidation; gpt-4.1-mini; no held-out benchmark; 3,814 tokens | vendor | - | docs/engine_survey/mnemon.json#eval.rigour |
| Honcho | 89.9; haiku-4-5 reader, judge unnamed in the blog, repo judge lenient | vendor | - | docs/engine_survey/honcho.json#eval.rigour |
| MemOS | 88.83 OmniMemEval cloud; 75.80 T-Mem harness; 63.6 on LoCoMo-Refined with a strict Qwen3-14B judge | independent + vendor | - | docs/engine_survey/memos.json#eval.models |
| Cognee | 83.48 OmniMemEval; vendor 90.7 on 1,762 q incl. cat 5; 32,532 tokens/q | independent + vendor | - | docs/engine_survey/cognee.json#eval.rigour |
| MIRIX | 85.38 paper (GPT-4.1 judge); repo harness gpt-4o-mini generous judge, LoCoMo-tuned prompts | vendor | - | docs/engine_survey/mirix.json#eval.rigour |
| Nemori | 79.4 (gpt-4.1-mini, gpt-4o-mini judge); repo harness reader = judge; 2,745 tokens | vendor | - | docs/engine_survey/nemori.json#eval.rigour |
| Memori | 81.95 paper, 87.0 README; independent OmniMemEval 41.34 | vendor / independent | - | docs/engine_survey/memori.json#eval.rigour |
| LightMem | 71.95-72.99 with two judges reported; about 2,600 tokens | vendor | - | docs/engine_survey/lightmem.json#eval.rigour |
| Zep | Mem0 paper 65.99; Zep's own corrected run 75.14 +/- 0.17; zep-papers issue #5 claims 58.44 | vendor + third party | - | https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory ; https://github.com/getzep/zep-papers/issues/5 |
| Letta filesystem | 74.0 with gpt-4o-mini | vendor | - | https://www.letta.com/blog/benchmarking-ai-agent-memory/ |
| LongMemEval-S (500 q) | Mastra OM 94.87 gpt-5-mini / 84.23 gpt-4o; Hindsight 91.4; Supermemory 85.2 (its report also prints 97% for gpt-4o in a table labelled Recall@20, not comparable); Zep 71.2; full context 60.2; Mem0 platform 94.4 | vendor | - | https://mastra.ai/research/observational-memory ; https://supermemory.ai/research/longmembench ; https://docs.mem0.ai/core-concepts/memory-evaluation |

**What MemSpine has.** Errata (47 entries), official F1 port (0 mismatches on 1,748 cases), mem0-official judge prompt flag, cluster-bootstrap CI, noise-floor tools, A1 same-model confound declared.

**The gap.** No published number shares reader, judge and question set with ours. Judges differ and several are described as lenient; the same engine moves a lot between harnesses (Zep 58-75, Mem0 77.7-92.5, Memori 41-87). MemSpine's context is the smallest of the set (672-1,571 tokens against 2.3k-36k), the one axis on which a fair claim exists.

**Hypotheses and solutions (R14).**

| id | pri | layer | evidence | problem, gap, hypothesis | solutions (source; cost; risk) | test |
|---|---|---|---|---|---|---|
| R14-eval-1 | H | judge | 7 q (measured) | Problem: Headline numbers are not comparable across judge strictness; a 9B same-model judge can be stricter or looser than gpt-4o-mini in unknown directions. Gap: R14-confirmed I35. Missing: the stored clean-jina-full predictions have not been re-judged under the other published prompts, and no leniency estimate exists. Hypothesis: Re-judge the 1,540 stored answers under (a) the mem0-official prompt and (b) a 'touches the same topic' prompt (Nemori/EverOS style) with the local judge; the spread between (a), our strict prompt and (b) is the comparability band to print next to 81.4. Expected: (b) >= (a) >= ours by several points; the report states the three numbers and the token count. | **Triple-judge band on stored predictions** [docs/engine_survey/nemori.json#eval.rigour; docs/engine_survey/evermemos.json#eval.rigour; docs/engine_survey/honcho.json#eval.rigour; cost 3 x 1,540 local judge calls (GPU later; not run here); risk same-model confound stays (A1)]; **Token-efficiency table next to accuracy** [docs/engine_survey/cognee.json#answer.size; docs/engine_survey/memspine.json#answer.size; cost 0; risk none] | Re-judge script over results.jsonl; deltas per class. |
| R14-eval-2 | L | data | 25 q (measured) | Problem: Gold errors cap every engine; Cognee's vendor run excludes 143 golds, LoCoMo-Refined revised 1,382 questions, and Zep lists unusable category 5 and caption-dependent items. Gap: R14-confirmed. Report both headlines (already done). Hypothesis: Keep the errata-adjusted number as secondary and never compare it against a vendor's exclusion set. | **Dual headline with explicit exclusion set** [docs/engine_survey/cognee.json#eval.rigour; docs/engine_survey/memos.json#eval.models; cost 0; risk none] | None new. |

## Top techniques worth adopting (ranked)

1. **R14-link-1** (H, 70 q): Link-table expansion (Hindsight). Source: docs/engine_survey/hindsight.json#graph.edges; docs/engine_survey/hindsight.json#read.traversal. Cost: 0 model calls (embeddings exist).
2. **R14-qry-1** (H, 41 q): Record-styled multi-search plus HyDE note (Mnemon). Source: docs/engine_survey/mnemon.json#read.rewrite; docs/engine_survey/mnemon.json#read.legs. Cost: 1 call/q.
3. **R14-ans-1** (H, 40 q): Seven-step CoT trimmed to three steps (EverOS). Source: docs/engine_survey/evermemos.json#answer.prompt. Cost: 0 extra calls; more output tokens.
4. **R14-decon-1** (H, 59 q): Facts as keys, parent lift (EverOS VECTOR method). Source: EverOS: docs/engine_survey/evermemos.json#read.legs; docs/engine_survey/evermemos.json#rank.fusion. Cost: 0 extra calls at read; write cost unchanged (window_extract already runs).
5. **R14-eval-1** (H, 7 q): Triple-judge band on stored predictions. Source: docs/engine_survey/nemori.json#eval.rigour; docs/engine_survey/evermemos.json#eval.rigour; docs/engine_survey/honcho.json#eval.rigour. Cost: 3 x 1,540 local judge calls (GPU later; not run here).
6. **R14-time-1** (M, 33 q): Precision, status and type in the temporal pass (Mem0 platform). Source: https://docs.mem0.ai/core-concepts/memory-evaluation (Temporal Reasoning). Cost: 0 extra (same window call).
7. **R14-epi-3** (M, 110 q): Observer/Reflector log (Mastra OM). Source: https://mastra.ai/research/observational-memory (Methodology). Cost: 1 observer call per threshold plus reflector calls (sleep side).
8. **R14-seg-1** (M, 0 q): Day/session-first boundary order. Source: docs/engine_survey/evermemos.json#segment.rule; Nemori LLM rule docs/engine_survey/nemori.json#segment.rule; Honcho token gate docs/engine_survey/honcho.json#segment.rule. Cost: 0.
9. **R14-time-3** (M, 14 q): Window overlap plus proximity factor (Hindsight). Source: docs/engine_survey/hindsight.json#rank.rerank; docs/engine_survey/hindsight.json#read.temporal. Cost: 0.
10. **R14-qry-2** (M, 62 q): ALL-need paging (Mnemon). Source: docs/engine_survey/mnemon.json#rank.agentic; docs/engine_survey/mnemon.json#read.analysis. Cost: reranks + judge.

## Cross-cutting findings

- Context size: MemSpine 672 tokens/q (Jina) to 1,571, against EverOS 2.3-2.6k, LightMem 2.6k, MemMachine 2.6-2.7k, Nemori 2.7k, Mnemon 3.8k, MemOS 5.4k, Mem0 7.0k, Hindsight 24-36k, Cognee 32.5k (engine_survey `answer.size` rows). A like-for-like claim exists on this axis only.
- The two top-scoring systems return a larger unit (episode, or memory plus chunk) and use facts as keys; MemSpine's facts compete as records and lose (S03 -4, S04 -6). This is the strongest structural lead (R14-decon-1).
- Few surveyed engines rewrite queries or loop on the default path; the agentic gain on LoCoMo is small (MemMachine +0.46 for about 2x tokens), consistent with our slot loop net -3.
- Date handling fails silently in three engines on replayed data (Mem0 OSS, Memori, Nemori facts) and in two read-side parsers (MIRIX, the combined planner use the wall clock). Cheap invariant tests protect MemSpine (R14-decon-2, R14-time-2).
- Inference-permitting answer prompts plus lenient judges explain part of the vendor-to-local gap; the judge band (R14-eval-1) quantifies it without new generation.
- Not worth adopting: per-note A-Mem evolution (2+ calls per note), Mnemon's hosted 'needed?' judge (about 35k tokens/q), MemMachine/MIRIX destructive profile edits, Mem0's unread linked_memory_ids.
