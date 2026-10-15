# Combined engine (Graphiti + SimpleMem) vs MemSpine: document analysis, comparison and gap list (2026-10-11)

Status: analysis only. Nothing was built, run, committed or killed. `memspine-run` was not touched.
Sources read in full: `docs/combined-memory-locomo-eval-v4/docs/COMBINED_MEMORY_ENGINE_AND_LOCOMO.html` (7.1 MB; parsed with
`html.parser`, base64/SVG/script dropped, 2.4 MB of text, 51k lines, every prose section, table, config, prompt and code listing
that bears on the engine was read; the bulk of the remaining lines are full-file source appendices for auth, admin, Postgres and
Weaviate schema code that do not affect memory quality), the bundle code (`module/locomo_eval/*.py`, tests, manifest, README) and our
`docs/PIPELINE_TREE.md`, `docs/PERSPECTIVE_LAYER.md`, `docs/USAGE.md` (config reference), `docs/GAP_TO_SOLUTION_PLAN.md`,
`evals/analysis/GAP_REGISTER.md`, `FULL_RUN_FORENSICS_2026-10-10.md`, `SCREENING_RERANKER_2026-10-11.md`, `evals/QWEN_STACK_RESULTS.md`,
`docs/adr/ADR-064`.

Labels used below: **[D]** stated in their document or code; **[I]** my inference from reading their code (not measured by anyone);
**[O]** our own measured number or our own file:line.

---------------------------------------------------------------------------------------------------

## 0. Headline findings (read this first)

1. **The document contains NO LoCoMo result.** It is an "implementation learning guide" plus a transferable evaluator. It says so
   itself: "No verified 90+ result was found for our combined engine. The inspected repository has no saved combined-engine LoCoMo
   result" and "Proposed enhancements are experiments, not measured improvements" (section 1; repeated in sections 11, 14, 15 and the
   "Verified gaps" table: "No verified 90+ score is currently documented"). Live Bedrock/Neo4j/Weaviate operation, latency, cost,
   retrieval quality and official scores are all marked "Requires the other system". So "it is giving better results" cannot be
   checked from this bundle. If the user has numbers from a run on the other system, we need the run output (`manifest.json`,
   `predictions.jsonl`, `summary.json`) before any comparison is meaningful. See section 2.
2. **Their engine is two independent stores whose outputs are concatenated, with no fusion.** A Graphiti/Neo4j temporal fact graph
   (user turns by default) and a SimpleMem-style Weaviate store of LLM-written "lossless restatements" (assistant turns by default),
   plus optional verbatim last-N replay. The service returns three separate strings; the document states "No global cross-store
   dedupe, conflict resolution or final rerank" [D, R16, section 15].
3. **Almost everything structurally interesting in it is already a flag in MemSpine** (the G-1..G-23 absorb list, ADR-064, `mine_facts`,
   `extract_graph`, `entity_summaries`, `extract_graph.resolve`, `session_summary.incremental`). The difference is not missing
   features; it is that **our LoCoMo arms run raw-turn retrieval only** (no LLM roles, associative memory off, `--max-model-calls 0` on
   the retrieval side; `PIPELINE_TREE.md` section 1.1/1.2 and section 0) while theirs is an LLM-heavy derived-memory pipeline. None of our
   derived-memory features has been screened in a QA arm [O: GAP_REGISTER K rows E01, A05-A07, G01 "built, unscreened"].
4. **The most probable single reason a combined engine could score higher on LoCoMo QA is not its architecture but its reader and
   extraction models.** Their default answer model is Qwen3-32B (`bedrock_retrieval_model`), graph extraction is Qwen3-235B-A22B.
   Our reader and judge are Qwen3.5-9B Q4_K_M, and our own forensics put **138 of 302 failures (46%) on the reader even when all
   gold evidence is in context** (`FULL_RUN_FORENSICS` section 0; reader ceiling 87.1% with complete evidence; a larger reader is
   gap C11, "blocked by GPU memory"). Any head-to-head without a controlled reader swap mixes retrieval quality with reader size.
5. **Their temporal handling is richer in storage than in what the reader sees.** Edges carry `valid_at / invalid_at / created_at /
   expired_at` [D], but the formatted graph context printed to the reader has fact text only, raw episode text without the session date,
   and no validity dates ("Date validity fields and detailed provenance are also largely absent from the production formatted
   text", R08 [D]). Only the vector side bakes absolute dates into the restatement text. We render a `[YYYY-MM-DD]` prefix per line and
   `[= date]` annotations for relative phrases [O: PIPELINE_TREE 2.9, 2.11]; temporal is where we should be ahead of the graph path.
6. **Their evaluation harness is cleaner than their engine in places we can borrow** (official-evaluator delegation, error rows kept in
   the denominator, dataset/evaluator hashing, exclusive-create outputs, explicit refusal to call local F1 "official"). We already have
   most of the hygiene; we lack an official LoCoMo F1 / token-F1 view (section 4, G-C7).

---------------------------------------------------------------------------------------------------

## 1. Document summary: what the combined engine is

### 1.1 Identity and scope [D]

"MOFSL memory server" (a Motilal Oswal-style financial-assistant product; the stock/ICICI examples in the reformulation prompt and the
custom edge-extraction instruction ("asking about, requesting, analyzing, querying, or showing interest in it") are product-specific
and not LoCoMo-tuned). Python >= 3.13, FastAPI, vendored `graphiti_core` (module/graphiti, a recent version with sagas,
`extract_summaries_batch`, deterministic-first dedupe), a rewritten async SimpleMem (`module/dev_mem/short_term`, not the vendored
`module/simplemem/core`), AWS Bedrock for all models (region ap-south-1), Neo4j (graph), Weaviate (vectors + transcript + sessions),
PostgreSQL (admin/auth only), optional Valkey (migration coordination). Bundle revision `070ccd91d859afa478d5ed50ead1fb3c07a69870`;
the bundle is an overlay and does NOT contain the engine.

### 1.2 Stores and roles [D, sections 2, 6]

| Store | Content | Scope |
|---|---|---|
| Neo4j graph (Graphiti) | `Episodic` (raw turn text + raw IDs), `Entity` (name, summary, name_embedding), `RELATES_TO` fact edges (fact text, fact_embedding, valid_at, invalid_at, created_at, expired_at, episodes[]), `MENTIONS`, `Saga`/`NEXT_EPISODE` ordering, custom `User`/`Thread` nodes with `user_summary` and per-session `summary`; communities optional (off) | group = `project_user` (USER-WIDE across sessions) |
| Weaviate short-term (SimpleMem) | one row per LLM-written atomic entry: `lossless_restatement`, keywords, persons, entities, location, topic, timestamp (TEXT), 1024-d vector | filter = project AND user AND session (SESSION-scoped) |
| Weaviate transcript | one row per turn, paired by `request_id`; active/passive collections with archival | project/user/session |

### 1.3 Write path [D: sections 3-6, I00-I20, R-code]

Per turn: validate -> ensure session -> persist transcript row -> `_fanout` (sequential: graph, then vector).
**Asymmetric role policy (production mode):** user turns -> graph episodes; assistant turns -> vector extraction only; assistant
episodes in the graph only if `ingest_assistant_episodes`; system turns never. "both does not mean every source turn is written to
both backends." The LoCoMo adapter maps speaker A -> user, speaker B -> assistant (`dataset.py`), with a separate `--ingestion corpus`
mode that writes every turn to both (experimental, "must never be labelled unchanged production behavior").

**Graph write (Graphiti `add_episode`, one episode per turn, serial per-thread queue, awaited in evaluation):**
1. previous episodes (window 5, `episode_window_len`) fetched for reference resolution;
2. LLM entity extraction (`extract_message` prompt: speaker always first node, pronouns disambiguated, no dates/relations as entities;
   typed `ExtractedEntities`);
3. entity resolution, deterministic first (exact / fuzzy MinHash-style similarity with an entropy gate on short names, in
   `dedupe_helpers`), LLM `dedupe_nodes` prompt only for the holdouts;
4. LLM edge extraction (`extract_edges.edge`: subject/object names restricted to the extracted entity list, `relation_type`
   SCREAMING_SNAKE_CASE, `fact` paraphrase, `valid_at` / `invalid_at` ISO strings; `REFERENCE_TIME` = the turn's historical timestamp;
   "if ongoing set valid_at = reference time"; "do not hallucinate temporal bounds"); custom instruction forces "user actions and
   interests" into edges;
5. edge resolution: candidate search (hybrid) -> LLM `resolve_edge` returns `duplicate_facts` and `contradicted_facts` idx lists; then
   deterministic `resolve_edge_contradictions` sets `invalid_at = new.valid_at` and `expired_at = now` on an older contradicted edge
   whose `valid_at` is earlier (the bi-temporal invalidation rule) [D, code listing];
6. LLM entity attribute / summary hydration (`extract_summaries_batch`, per-entity summary refresh);
7. embeddings (entity name, fact) with `cohere.embed-english-v3`; persist; saga order.
Then wrapper-derived state: **session summary** updated on every user turn (incremental, 250 words, JSON, full bounded rebuild every
20 user episodes over <= 200 saga episodes; built from user questions, not assistant text), **user summary / profile** (plain text,
starts at 10 episodes, incremental each turn, full rebuild every 10 episodes over a 150-episode lookback). Community detection off by
default and skipped in migration.

**Vector write (SimpleMem `MemoryBuilder`):** assistant message only; fresh builder per call so a live call is a window of 1 (window size
default 10 only matters for batches; `Dialogue(speaker="")` - speaker labels are DISCARDED, the LoCoMo adapter prefixes the name into the
text); one Qwen3-32B call (temperature 0.1, up to 3 parse retries) with the extraction prompt: third-person, no pronouns, relative time ->
ISO-8601 absolute date, "each memory entry must be independently understandable", skip greetings/trivia, JSON fields
`lossless_restatement, keywords, timestamp, location, persons, entities, topic`. Only `lossless_restatement` is embedded. No
dedupe/consolidation of stored entries ("No stable content hash or semantic duplicate consolidation is enforced"; "no explicit computed
entropy score or threshold").

### 1.4 Read path [D: sections 7-9, R01-R18]

1. **Query reformulation** (one Qwen3-32B call, temperature 0.1, 150 tokens, 2 s timeout): last <= 5 answered exchanges of the session
   (assistant text truncated to 200 chars) -> "if dependent, rewrite as standalone, else return the exact original"; errors fall back to the
   original; no equivalence or non-empty validation. Chat-oriented ("explain that", "and what about ICICI?").
2. **Graph branch** (user-wide): two overlapping calls, `search_memory` and `get_thread_context`. Search layers: edges
   (BM25 + cosine + BFS depth 3, RRF), episodes (BM25 only, RRF), communities (BM25 + cosine; retrieved but NOT formatted), nodes (off in
   `current`, on in `enhanced`). Limits 8 edges / 8 episodes / 5 communities / 10 nodes. **Default reranker is RRF; the Cohere v3.5
   cross-encoder client exists but is not invoked; MMR exists but is not the default.** `min_score` 0.6 and `mmr_lambda` 0.5 are config
   fields; the `relevance_score = 0.8` returned by the wrapper is a hard-coded placeholder [D].
   The text sent on is `get_thread_context.formatted_context`: `[USER PROFILE]` (user summary), `[RELEVANT RELATIONSHIPS]` (fact strings
   only), `[PEOPLE, PLACES & THINGS]` (entity name: summary; only if node search on), `[RELEVANT PAST MESSAGES]` (raw episode content,
   no date), and `[THIS SESSION SO FAR]` (session summary).
3. **Vector branch** (session-scoped): one planning LLM call (Qwen3-32B) -> up to **2** targeted semantic queries + keywords + persons /
   entities / location / time constraints (planner resolves "last week" against the **machine wall clock**, not the conversation date,
   which the document flags as a temporal risk). Semantic top-5 (cosine >= 0.5), BM25 top-5 (score >= 1.0, over `lossless_restatement`
   only), optional structured search (off). Union by `entry_id`; no learned fusion. Optional **reflection** (default off; `enhanced` = 1
   round): an LLM checks completeness and issues extra queries; it stops when the first retrieval is empty. Returns formatted
   restatement + metadata text.
4. **Optional last-N** verbatim exchanges (off by default in the evaluator).
5. Response = three strings: `long_term_context`, `short_term_context`, `last_n_messages_context`.
6. **Answer** (outside the memory API, in the evaluator): `ANSWER_SYSTEM` = "Answer only using the retrieved memories ... Use historical
   event dates ... Include every requested list item. If unsupported say exactly 'Not mentioned in the conversation'. Return JSON with a
   concise 'answer'". Input = JSON `{question, memories:{...3 blocks}}`; temperature 0; if all blocks are empty the abstention is returned
   without a model call. Default answer model = the vector retrieval model (Qwen3-32B) unless `--answer-model`.

### 1.5 "Compression / consolidation" in their stack [D, I]

SimpleMem's "semantic structured compression" is here the write-time LLM restatement (coreference + absolute time + atomic split). The
"online semantic synthesis / adaptive retrieval" of the SimpleMem paper is only partly present: planning (up to 2 queries) yes;
reflection optional and off; no entropy gate, no cross-entry synthesis or merge, no adaptive query-complexity budget [D, section 5
"What is not established"]. Graph-side condensation = entity summaries + session summary + user summary (lossy, bounded; the document
tells the reader to keep raw episodes).

### 1.6 Models, configs and budgets [D, config files]

| Role | Value |
|---|---|
| Graph entity / edge extraction | `qwen.qwen3-235b-a22b-2507-v1:0` (Bedrock) |
| Graph summaries, vector extraction, vector planning/reflection, reformulation, default answer model | `qwen.qwen3-32b-v1:0` |
| Embeddings | `cohere.embed-english-v3`, 1,024-d; documented defect: queries are embedded in `search_document` mode (the `is_query` flag is ignored) |
| Reranker | `cohere.rerank-v3-5:0` constructed, not used by default |
| Judge (optional) | any Bedrock model via `--judge-model`; none specified |
| Graph search | methods edges `bm25,cosine,bfs`; episodes `bm25`; nodes off; reranker `rrf`; BFS depth 3; limits 8/8/5/10 |
| Vector | window 10, workers 8, semantic top-5 (>= 0.5), keyword top-5 (>= 1.0), structured off, planning on (max 2 queries), reflection off |
| Summaries | session 250 words / rebuild every 20; user 250 words / min 10 episodes / rebuild every 10 / lookback 150 |
| `enhanced` profile (eval-only) | vector top-K >= 10, reflection 1 round, graph node search on, edge and episode limits >= 16 |
| Cost / tokens | **none recorded**: "No token/cost claim: ... model-specific token accounting and billing are not implemented" [D, section 15] |

LLM calls per accepted turn [I, from the code listings; not measured]: user turn about 5-8 (entity extract, optional dedupe, edge extract,
edge resolve per candidate-bearing edge, attribute/summary hydrate, session summary, user summary incremental or rebuild); assistant
turn about 1 (vector extraction); plus per question: reformulation 1, planning 1, optional reflection 1-2, answer 1, judge 1.
On the 5,882 LoCoMo turns in production mode that is the order of 20k-25k ingestion calls (corpus mode: about 35k), on a 235B MoE and
a 32B model. Ours: **zero** LLM calls on the write path in the LoCoMo arms [O].

### 1.7 Profiles: current vs enhanced [D]

`current` = the configured defaults. `enhanced` = eval-only retrieval ceilings (top-K up to 10, one reflection round, node search on,
limits 16). It "bundles several changes", "is not guaranteed to improve quality", and no result for either profile is reported.

---------------------------------------------------------------------------------------------------

## 2. Their LoCoMo evaluation: protocol and comparability

### 2.1 Protocol (from the bundle code) [D]

| Item | Their harness |
|---|---|
| Dataset | LoCoMo-10 (`locomo10.json`, SHA-256 `79fa87e9...ff4`): 10 conversations, 5,882 turns, 1,986 questions. Category counts they print: 1 multi-hop 282, 2 temporal 321, 3 open-domain 96, 4 single-hop 841, 5 adversarial 446. |
| Categories scored | 1-4 as the headline (`*_categories_1_4_pct`); category 5 only as a local abstention check (`adversarial_abstention_pct`), reference forced to "Not mentioned in the conversation". |
| Sample size | CLI default is `--samples 1 --questions 20` (a **prefix** of questions, "not a representative stratified sample"); `--full` for everything. Smoke and ablation commands in the doc use 1 sample x 5-20 questions. |
| Ingestion | `production` (A -> user -> graph, B -> assistant -> vector; names prefixed into content; transcript rows inserted directly; writes awaited; timestamps = dataset session time, UTC assumed, +1 microsecond per turn) or `corpus` (every turn to both stores, whole sessions batched into vector extraction). |
| Retrieval scope | `--session-scope all` (default): calls the service once per session of the sample, concatenates the blocks, drops exact duplicate blocks (a benchmark extension, repeats reformulation and user-wide graph search per session) or `latest`. `--memory-types both|longterm|shortterm`. `--last-n` optional (off). |
| Leakage controls | Only `question` + retrieved `memories` enter the answer prompt; gold, category, evidence never do; test suite checks this. Gold evidence `dia_id`s are saved but unused (no evidence-recall metric; returned text carries no `dia_id` mapping). |
| Metrics | (a) `token_f1`: local, unstemmed, article/punctuation stripped, symmetric F1, **explicitly "not the official LoCoMo metric"**; (b) `official_locomo_score` by loading the upstream `task_eval/evaluation.py::eval_question_answering` (stemmed token F1, category-specific rules: multi-answer split for cat 1, reference transformation for cat 3, own rule for cat 5; the file imports numpy/regex/nltk/bert-score); (c) optional `strict_judge_pass_pct`: a 0/1 LLM judge ("require all requested facts ... reject incorrect dates, quantities, people, ... incomplete lists; accept synonymous wording and equivalent date formats only"; temperature 0, JSON). Errors stay in the denominator (scored 0 / blank prediction), duplicates rejected, empty-context rows counted. |
| Judge model | not fixed; caller supplies a Bedrock model id. No judge result exists. |
| Cost / latency | records `retrieval_ms`, `answer_ms`, ingestion `elapsed_ms`, context character counts. No tokens, no money. |
| Reported results | **none** (section 0). The doc says the "90+" mentioned in its title is an unverified target and warns against reading a permissive judge pass rate as 90% F1 (it points at the standalone SimpleMem evaluator's lenient judge prompt and its two-option adversarial prompt as the likely source of an inflated number). |

### 2.2 Is anything comparable with our numbers? Verdict: NO (nothing to compare, and the protocols differ)

| Dimension | Theirs | Ours [O] | Comparable? |
|---|---|---|---|
| Result numbers | none in bundle | 80.4% (contaminated prompt, relative only), 81.4 with protected pool (+15 net, noise +/-12.9), BEST dev 82.7 | nothing to compare |
| Metric | official stemmed F1 and/or strict 0/1 judge (not run) | rubric LLM judge, accuracy; Mem0-official judge available as an offline second view (`second_view.py`), not yet run on the full set | F1 is a different quantity from judge accuracy (SimpleMem's own paper number is token-F1 43.24 [O: SOTA_MEMU_MEMOS_LOCOMO.md]; Mem0-style J-scores are 60-92); do not mix |
| Judge model | unspecified Bedrock model | local Qwen3.5-9B, the same model as the reader; known judge artifacts (empty answers credited, gaps A1-A3) | no |
| Reader model | Qwen3-32B default (hosted) | Qwen3.5-9B Q4_K_M, thinking off | no: 3.5x parameters; our reader ceiling is the dominant loss |
| Extraction models | Qwen3-235B-A22B + Qwen3-32B at write | none (raw turns) | not an apples-to-apples pipeline |
| Categories | 1-4 headline + cat 5 abstention side check | 1-4 headline; cat 5 optional guard only | same 1,540 questions if `--full` |
| Sample | default 1 conversation x 20 questions | all 10 conversations, 1,540 | only `--full` matches |
| Ingestion / scope | A->graph, B->vector (production) or both (corpus); all-session union | every turn to one episodic store; 32-turn batches; session replay window +/-2 to +4 | different information available to each store |
| Context shape | 3 text blocks, summaries + facts + raw episodes, no dates in graph part | 10-20 retrieved turns + neighbour window, dated lines, ~0.7-1.6k tokens | different, ours dated |
| Errata | none handled (23 known gold errors count against them) | errata file; headline with and without | no |
| Dev/held-out | none | exposed (all 10 conversations are now one development pool) | no |

Explicit caveats to flag to the user:
1. If a "better result" comes from a lenient judge (their own doc warns about the SimpleMem evaluator), a two-option adversarial prompt,
   a prefix subset, cat 5 abstention, or an F1/judge mismatch, it says nothing about retrieval quality.
2. Even a clean judge-accuracy number from their engine would be confounded by the 32B reader and the 235B extractor.
3. Our own 80.4 is contaminated by the gold-example prompt (I80) and judged by the reader model; our relative deltas are the usable
   signal. A clean re-baseline (Jina reranker + clean prompt) is already the next planned step [O: commit 888b888].
4. The meaningful comparison is **paired and controlled**: same questions, same reader, same judge, saved contexts. Their harness saves
   the exact retrieved context per question in `predictions.jsonl`, so a context-swap test (their contexts + our reader/judge, and ours
   + theirs) is possible without trusting either side's judge. See Q1-Q3 in section 5.

---------------------------------------------------------------------------------------------------

## 3. Side-by-side architecture, stage by stage

Our evidence column cites `PIPELINE_TREE.md` (PT) sections, `USAGE.md` config keys, or files. "arm" = the LoCoMo QA arm (BEST dev +
perspective + protected pool).

| Stage | Combined engine (theirs) [doc section] | MemSpine (ours) | Notes |
|---|---|---|---|
| Write / ingest | Transcript row + serial per-thread queue; graph episode per eligible turn (awaited in eval), vector call per assistant turn; best-effort fan-out, `stored_in` is not a receipt [3, 4, 15] | `Engine.write_messages` -> event log (sole source of truth) -> projectors (record, LanceDB vector, Tantivy BM25) transactional per batch, retry-safe; 32-turn batches [PT 1] | ours is lossless, replayable, no model call; theirs derives memories at write |
| Extraction | LLM on every turn: entities, edges (typed), summaries, atomic restatements; prompts above [1.3] | none in arm. Opt-in sleep-time: `mine_facts` (`extract@session4`), `extract_graph` (`extract_edges`, `@session`), `rule_miner`, `rule_edges`, `entity_summaries` [USAGE 1107-1147] | built, unscreened in QA |
| Storage | Neo4j (episodes, entities, edges), Weaviate (restatements, transcript, sessions) [2] | SQLite event log + record read model; LanceDB vectors; Tantivy BM25; graph store only if associative on (LadybugDB default, ADR-064) [PT 0, 1] | |
| Indexing | cosine + BM25 on edges; BM25 on episodes; cosine + BM25 on restatement text; no HNSW tuning noted; Cohere embed v3 1024-d; query embedded in document mode (defect) [8, 9, gaps table] | Qwen3-Embedding-0.6B 1024-d, asymmetric query instruction; flat exact cosine; Tantivy BM25 over raw turn content (no stemming) [PT 1, 2.1, 2.3] | ours has a proper query/doc asymmetry |
| Temporal model | Bi-temporal edges: `valid_at/invalid_at` (event time), `created_at/expired_at` (system time); episode `valid_at` = turn's historical time; contradiction -> `invalid_at`, `expired_at` [2 time table, 4] | `valid_from` per record = turn timestamp; `recorded_at`; `valid_to/superseded_at/invalid_at` on records; semantic M4 conflict ladder; as-of reads; read-time relative-date resolver with `[= date]` annotations; `extract_graph.close_ended` (G-1), `contradictions` (G-2), `event_identity: dated` (G-3) [records.py:166-210, PT 2.9, USAGE 1099-1102] | they invalidate edges but never show validity in the reader context; we show dates on every line |
| Entity resolution | Graphiti: exact + similarity + entropy gate, LLM holdouts, per-turn [4] | `extract_graph.resolve: rules` (alias table, embedding candidates, entropy gate, MinHash >= 0.9) / `llm` batched per sweep (GP-7) [USAGE 1148] | absorbed; off in arm |
| Retrieval legs | edges: BM25 + cosine + BFS(3); episodes: BM25; (communities: not shown); vector: planned semantic (<= 2 queries) + BM25 (+ structured off); last-N optional [6, 8, 9] | vector + BM25 (hybrid) + temporal leg (absolute-date questions) + perspective leg (subject, protected); protected pool slots per leg (`pool_protect_per_leg: 3`, I75a); 20+ further legs built and off (graph, entity, sentence, probes, PRF, cluster...) [PT 2.3, GAP_TO_SOLUTION 1.1] | |
| Fusion | RRF within graph types only; vector: union by `entry_id`; **no cross-store fusion** [8, 9, 15] | RRF over legs (k=60), unweighted; min-max reranker scores; MMR at assembly; list mode (pool 30) [PT 2.4, 2.7, 2.8] | |
| Rerank | RRF only; Cohere cross-encoder constructed but unused; MMR available [2, 8] | cross-encoder: Qwen3-Reranker-4B 4-bit (BEST) or Jina v3.5 (screening/next baseline); `rerank_keep` 10; relative floor skipped for rerank [QWEN_STACK, SCREENING] | we use a real reranker |
| Context assembly | 3 blocks, concatenated, each backend-ordered; `[USER PROFILE]`, facts, entity summaries, raw messages without dates, session summary; sizes bounded by top-K only [8] | ranked turns + replay window (before 2 / after 4 turns), chronological order, `[YYYY-MM-DD]` prefix per line, relative-date annotations, token budget 4,096 [PT 2.10, 2.11] | |
| Compression | write-time restatements (atomic, coref-resolved, ISO-dated); entity/session/user summaries [5, 4] | none in arm; consolidation summaries, atomic fact cards, entity summaries built (opt-in); lossless raw turns authoritative with `parents` provenance [USAGE 216-218, 984-990, 1107-1117] | |
| Answer prompt | short grounded prompt, JSON `{answer}`, exact abstention phrase, "use historical event dates", "include every requested list item"; same prompt for all categories; temperature 0 [R17] | `grounded`-family prompts with list mode, refusal retry (neutral), date repair, duration solve, count verify, slot verify (all opt-in/screened); thinking off [FORENSICS 5, GAP_REGISTER A-K] | ours is far more engineered (and the prompt contamination found in I80) |
| Verification | none; the self-rated reflection coverage is explicitly "not benchmark accuracy" [9] | `verify_answer`, `--verify-slots`, assertion check, judge conventions v2, errata, contamination lint [GAP_REGISTER K, DEVELOPMENT_VERIFICATION] | |
| Evaluation | official-evaluator delegation, strict 0/1 judge, local F1, abstention check, manifests with hashes, exclusive-create outputs, errors in denominator [11-13, code] | rubric LLM judge (Qwen3.5-9B) + guards, Mem0-official second view (prepare/collect), errata-aware, paired flip analysis, sign tests, noise floor, forensics (query analysis, per-leg ranks, cuts) [FORENSICS 1, `second_view.py`] | we lack an F1 view; they lack error taxonomy / paired stats |
| Security / governance | API-key auth, project/user/session scope; no firewall; summaries can leak across sessions by design (user-wide graph) [6, gaps table] | write-time firewall (trust, instruction shape, MINJA bridge, quarantine), trust lineage, consent, erasure, privacy tiers [PT 1 [A], docs] | our N1 differentiator |
| Cost model | high LLM cost per turn at write; low per query besides 2-4 calls | $0 write, local GPU; read-time rerank + one reader call | |

---------------------------------------------------------------------------------------------------

## 4. Gaps: what they have or do that we lack or do worse

Method: for each candidate I checked (a) is it in their code/doc, (b) is it in MemSpine (and its status), (c) which of our measured
loss classes it attacks (FULL_RUN_FORENSICS, 302 wrong of 1,540: recall 69 [multi 34, open 22, single 9, temporal 4], fusion/pool cuts 61,
reader 138 [wrong detail 47, inference 23, date 25, list 22, count 8, refusal 11], judge 11, errata 23), and (d) whether it is a general
capability (rule I37: nothing LoCoMo-specific). **All expected impacts below are hypotheses on measured class sizes, not measurements,
and not promises that their engine scores higher** - the document has no score. Effort: S = flag/screen/harness change, M = engine wiring
plus a screen, L = new subsystem. Generality: H / M / L.

### G-C1. Reader scale and extraction-model scale (confounder, not a memory feature)
- Evidence: default answer model = `bedrock_retrieval_model` = Qwen3-32B; graph extraction Qwen3-235B-A22B [1.6, models.py]. Ours Qwen3.5-9B Q4.
- MemSpine equivalent: C11 "larger reader (27-32B)", blocked by GPU memory; A9 thinking mode rejected; reader failures with complete
  evidence 136 + judge 10 + errata 22 = the 87.1% ceiling [FORENSICS 0, 5 row 9].
- Status: not tested (blocked). Expected impact: largest single lever on any engine: reader class 138 questions (10-30 plausible per
  forensics, uncounted). By category: open-domain inference (23), single-hop detail (47), temporal arithmetic (25), lists (22).
- Effort: S if a hosted or larger local reader can be run (Bedrock Qwen3-32B, or a quantised 27-32B on a bigger GPU); Generality: H.
- Use: a controlled reader swap is the prerequisite for reading ANY cross-engine number (section 5 Q2).

### G-C2. Write-time LLM atomic restatements as a retrieval unit (SimpleMem)
- Evidence: extraction prompt [1.3, I16]: third-person, no pronouns, relative time -> absolute ISO date, independently understandable,
  one embedding per restatement, BM25 over restatements. Their own gaps table notes it loses user facts when only assistant turns are
  extracted and that live windows are size 1 with no neighbours (known defects).
- MemSpine equivalent: `consolidation.mine_facts` (C6'), `mine_by_topic` (H15), `mine_multiview` (#28), `list_cards` (#30),
  `read.cards: header|assemble` (G1b), `read.view_tag_leg`, `extract@session4` prompt; `miner: rules` zero-model variant (W5);
  facts keep `parents` provenance (theirs have none). Status: **built, opt-in, never screened in the QA arm** (arms `local-rulecards*` exist
  as early rule-card screens; the LLM miner with Qwen3.5-9B has not been compared on the full set). Our arm uses read-time relative-date
  annotation instead of write-time coreference.
- Expected impact: recall class 69 and fusion cuts 61 (paraphrase and pronoun/"that" turns whose gold is only findable through a
  resolved statement), multi-hop (34 recall misses) first, then single-hop; temporal modestly (we already annotate). Risk: the miner can
  drop details (FORENSICS: wrong-detail is already 47) so raw turns must stay authoritative, as designed.
- Effort: M (one local 9B pass per session over 5,882 turns; no new code needed, screen first; cost is GPU time). Generality: H (any chat).

### G-C3. Fact-edge graph as a retrieval unit with BFS expansion and entity summaries (Graphiti)
- Evidence: edges BM25 + cosine + BFS depth 3 with RRF, entity summaries, user-wide graph [8, R06-R07].
- MemSpine equivalent: `extract_graph` (C2/C3, `granularity: session`, `event_identity`, `close_ended`, `contradictions`, `cardinality`,
  `relation_types`, `entity_types`), `entity_nodes`, `graph_leg`, `graph_node_search` (GR-6), PPR, communities, `entity_summaries`
  (GP-6), `resolve` (GP-7), LadybugDB with entity embeddings (ADR-064). Status: **built, opt-in, associative memory is OFF in the QA arm**
  (PT section 0, 1.1), so none of it has contributed to a LoCoMo number.
- Expected impact: multi-hop (recall 34 + cuts 20) and open-domain (recall 22) via entity hops; little for single-hop/temporal. Their
  doc gives no evidence that the graph helps LoCoMo; their gaps table says community results are not even delivered.
- Effort: M (needs an `extract_edges` LLM role on the local stack, then the existing `graph_leg`/`graph_node_search` flags); Generality: H.

### G-C4. Evidence heterogeneity: parallel blocks of facts + raw sources + summaries
- Evidence: the reader sees a profile, extracted facts, raw episodes and a session summary simultaneously [R08]. The doc itself worries that
  this floods the prompt and that there is "no final cross-store ranking" [14 "Both performs worse than one backend"].
- MemSpine equivalent: single ranked list of raw turns + neighbours; `read.cards`, `entity_summaries`, `lead` sections are the
  analogues (header blocks within `cards_budget_share`); fusing derived records into the same RRF with protected slots is possible
  (`pool_protect_per_leg`). Status: built, unscreened.
- Expected impact: an unmeasured upper bound on recall; risk of context flooding (our reranker arms cut context 40-60% for a 1-3 point
  loss, so we know more tokens is not free). Effort: M (bundle with G-C2/G-C3 screens, not separately). Generality: H.

### G-C5. Cheap single-call LLM query planner (<= 2 targeted queries + keyword/entity/time signals)
- Evidence: `_plan_and_generate_queries`, one Qwen3-32B call [R10]; caps at two queries; fallback to the question.
- MemSpine equivalent: `read.planner: llm` v2/v3, `plan@contract.yaml`, `multi_intent_split`, `statement_probe`, `prf_expansion`, bridge hop
  (R2-1, not adopted: +5/-7), GLiNER2 planner leg. Status: built; screened and mostly neutral/negative on our stack.
- Expected impact: low incremental (our R2 bridge screen was negative). Note their planner uses wall-clock "now", a bug we must not copy.
  Effort: S (a screen of `planner: llm` with the local reader exists); Generality: M.

### G-C6. Bounded reflection round for missing information
- Evidence: SimpleMem reflection (default off; `enhanced` = 1 round); stops on empty initial retrieval; self-rated coverage [R13].
- MemSpine equivalent: `read.agentic` / `agentic_mode: query|slot` (I67, E02), `sufficiency*.yaml`, A03 contract line. Status: built,
  screen pending (FORENSICS row 3: 20% capture assumed, 14 questions). Expected impact: recall 69 (multi-hop). Effort: S (screen).
  Generality: H. Theirs gives no evidence it helps.

### G-C7. Official LoCoMo F1 (and local token F1) as a second metric view
- Evidence: `metrics.py` + `load_official_evaluator` delegating to the upstream `evaluation.py`; local F1 explicitly labelled
  non-official; cat 5 abstention separated; hash of evaluator pinned [11, 12, F].
- MemSpine equivalent: judge accuracy only; Mem0-official judge second view exists offline (`second_view.py`); no F1, no stemmer-based
  official scorer in `evals/memspine_evals/metrics.py`. Status: gap.
- Expected impact: none on accuracy; enables comparison with F1-reporting systems (SimpleMem 43.2 token-F1, MemoryOS, A-Mem, LightMem
  papers) and removes the "judge model" confound for those comparisons. Effort: S (vendored scorer call at pinned hash, per-category
  output). Generality: H (any short-answer QA).

### G-C8. Bi-temporal edge invalidation as an automatic write-time operation (valid_at / invalid_at / expired_at)
- Evidence: edge prompt, `resolve_edge`, `resolve_edge_contradictions` code [4, listing].
- MemSpine equivalent: M4/M5 conflict ladder for semantic records, as-of reads, latest-wins, cardinality one/many (I17), `close_ended` (G-1),
  `contradictions` (G-2), `event_identity` (G-3), `invalidate_edge` role. Status: built, opt-in, needs LLM role; no LoCoMo effect expected
  (LoCoMo temporal questions ask event dates, not "current value"; knowledge-update items are rare). Their own formatted context drops
  the validity interval, so it did not help the reader there either.
- Expected impact on LoCoMo: ~0; on real assistants and MemoryAgentBench Conflict_Resolution (our planned blind slice): high.
  Effort: S to screen on the blind slice. Generality: H.

### G-C9. Rolling session summary and user profile injected on every read
- Evidence: `[USER PROFILE]` (user summary, 250 words, incremental per turn) and `[THIS SESSION SO FAR]` (session summary) prepended [4, R08].
  In their eval the user summary is of speaker A only (user role).
- MemSpine equivalent: `session_summary.incremental` + `rebuild_every` (#56), `summarize@structured`, `profile_pack`, Lambda profile slots,
  `read.cards: header`. Status: built, unscreened for LoCoMo QA. Hypothesis: helps open-domain inference (23 reader-inference
  failures + 22 recall) because the model gets a persona; hurts when summaries are lossy (their doc warns).
- Expected impact: open-domain (+), multi-hop (+/-); Effort: M (local 9B summary pass); Generality: H (profile is the main chat use case).

### G-C10. Query reformulation from chat history
- Evidence: `QueryContextManager` + reformulator [R02]; lightweight 150-token rewrite; fallback to the original.
- MemSpine equivalent: `query_rewrite.yaml` and `query_rewrite@compose.yaml` (built), `query_contract`. Status: built; LoCoMo
  questions are standalone, so no measurable effect expected on this benchmark. Hazard in their LoCoMo harness: the "history" is the last 5
  exchanges of each session (the tail of the pre-loaded conversation), so independent questions can be mis-rewritten under all-session scope;
  there is no equivalence check. Effort: S; Generality: H for chat, N/A for LoCoMo.

### G-C11. Product surface: sessions, paired transcript, active/passive archival, follow-up click correlation, per-thread queue semantics
- Evidence: sections 3, 6, I01-I07. MemSpine has REST v0.1, session lifecycle (#53 passive sessions, off by default), reply links
  (A07), feedback (#54). Gap is product breadth, not retrieval quality. No LoCoMo impact. Effort: L; Generality: M. Defer.

### Things in their harness we should adopt regardless (S, general)
- Dataset and evaluator SHA-256 recorded in the manifest; run output directories created exclusively; failures kept in denominators
  (we already keep errors); explicit "do not label local F1 as official" convention; category-5 treated as a separate abstention number.
- A `--ingestion corpus` style switch is irrelevant to us (every turn already goes to one store).

### Things in their engine we should NOT copy (documented defects in their own doc)
Placeholder relevance 0.8; no dates and no validity intervals in the printed graph context; wall-clock planner; query embedded in
document mode; speaker labels discarded in the vector builder; live extraction window of 1 with no neighbouring turns; empty string on
backend failure (cannot tell failure from no result); no provenance on vector entries; no dedupe of restatements; summaries without
parents (cannot cascade erasure); fan-out receipts that overstate durability; the all-session union that repeats whole blocks.

### Things WE have that they lack (keep as differentiators)
1. Lossless event-sourced store: raw turns are authoritative, every derived record carries `parents` (erasure cascades, trust min), rebuild
   by projector replay; theirs is derive-and-forget with LLM-lossy restatements.
2. A real cross-encoder reranker in the read path and a per-leg protected pool (+15 net questions measured, `r7-protect-full`); theirs is RRF only.
3. Dated, chronologically ordered reader context with relative-date annotation and replay windows (neighbour turns); their graph context has
   no dates and no neighbours.
4. Perspective / attribution layer (speaker, addressee, subject, asker, modality, polarity, reported speech): their pipeline loses speaker
   identity in the vector store and maps one LoCoMo person to the "assistant".
5. Write-time firewall (trust, instruction-shaped, MINJA bridge, quarantine) and consent/erasure (N1).
6. Zero-LLM, local, $0 write path with measured throughput (22 min for the full 1,986-question retrieval grid; 10-13 min with batching).
7. An evaluation culture they do not have: gold-error errata, paired flip analysis, stage forensics (write / recall / cut / reader / judge),
   contamination lint, dev/held-out discipline, judge-calibration, noise floors, cost per question.
8. Answer-side machinery (list mode, count/date/duration solvers, refusal retry, verification) that their single prompt lacks.

### Ranking: top 10 by expected impact x generality (all hypotheses)

| Rank | Gap | Why here | Effort | Class attacked |
|---|---|---|---|---|
| 1 | G-C1 Reader/extractor scale control | biggest class (reader 46%), any engine, decides how to read every other number | S (if capacity) | reader 138 |
| 2 | G-C2 LLM atomic restatements as a leg (mine_facts + multiview + cards) | the one SimpleMem idea with a direct mapping to recall/cuts; built, unscreened | M | recall 69, cuts 61 |
| 3 | G-C3 Fact-edge graph leg + entity summaries (Graphiti) | multi-hop and open-domain hops; built, associative off | M | recall (multi, open) |
| 4 | G-C9 Session summary + profile block | open-domain inference and persona use; built, unscreened | M | open-domain 46 |
| 5 | G-C6 Bounded reflection / agentic slot read | recall on set-shaped questions; built, screen pending | S | recall 69 |
| 6 | G-C4 Heterogeneous block assembly with protected slots | needed to combine 2-4 without flooding | M | cuts 61 |
| 7 | G-C7 Official F1 + token F1 second view | makes cross-paper comparison honest, no engine change | S | measurement |
| 8 | G-C8 Auto edge invalidation (bi-temporal) | high generality, ~0 LoCoMo effect; screen on MemoryAgentBench | S | knowledge updates (blind slice) |
| 9 | G-C5 LLM single-call planner | already screened neutral/negative on our stack | S | recall (multi) |
| 10 | G-C10 History-based query reformulation | chat generality only; no LoCoMo effect | S | n/a |

(G-C11 product surface is deliberately unranked.)

---------------------------------------------------------------------------------------------------

## 5. Questions for discussion (decisions needed before building anything)

1. **Where does "better results" come from?** The bundle has none. Do you have a `summary.json` / `predictions.jsonl` / manifest from a run on
   the other system? Which metric (official F1, strict judge, or the SimpleMem evaluator's lenient judge), which answer and judge models, how
   many samples/questions (default is 1 conversation x 20 questions), which ingestion (`production` or `corpus`) and scope (`all` or `latest`)?
   Without those, any comparison is a guess. If it came from the standalone SimpleMem evaluator, its document warns that pass rate is inflated.
2. **Control experiments before adoption.** Prefer (a) our retrieval + a 32B reader (hosted Qwen3-32B or a quantised 27-32B) to learn what
   a larger reader alone gives us; and (b) their saved retrieved contexts for the same 1,540 questions answered and judged with OUR reader
   and judge (the harness saves `contexts` per row), and the reverse. Do you want that? It needs a run of their engine on the other system.
3. **Integrate or port?** Options: (i) write a MemSpine harness adapter that calls their `MemoryService.get_memory_context` (needs Bedrock,
   Neo4j, Weaviate; useful as a baseline system arm, not as a dependency); (ii) port ideas as MemSpine flags (we have already absorbed most:
   G-1..G-23); (iii) both. Recommended: (ii) for engine work, (i) only if the other system can run it, as a baseline.
4. **Adopt the official LoCoMo F1 as a second view?** Recommended yes (S); it requires vendoring or pinning `task_eval/evaluation.py`
   (numpy, regex, nltk, bert-score imports) and recording its hash. Also run the Mem0-official judge second view we already built.
5. **Graphiti-style edge invalidation (G-C8):** keep it opt-in and screen it on the planned blind slice (MemoryAgentBench
   Conflict_Resolution + ConvoMem), not on LoCoMo, where we expect no gain. Agree?
6. **SimpleMem-style compression (G-C2):** it costs LLM calls on the write path, which conflicts with our "$0, no-model write" property and
   the deferred-ingest study. Is it acceptable to run `mine_facts` as a sleep-time job with the local Qwen3.5-9B (one pass per session) and
   screen it as an added RRF leg with raw turns authoritative? Which budget (GPU hours) is allowed?
7. **Reader capacity:** C11 is blocked by GPU memory. Can we use a hosted model (Bedrock Qwen3-32B or another) for a bounded diagnostic
   run, accepting that those numbers are labelled non-local and not mixed with the local table?
8. **Order of operations:** finish the clean re-baseline (Jina reranker + clean prompt, contamination-free) first, so every comparison
   uses a clean control; then the G-C2, G-C3 and G-C9 screens in that order, each with the existing paired-flip and noise-band rules.
9. **Rule I37 check:** all items above are general (profile, facts, graph, reflection, metrics). Nothing in this list needs a
   LoCoMo-specific trick; the only LoCoMo-flavoured temptation is tuning the `enhanced`-style K and limit increases to this dataset,
   which I recommend we do not do.
10. **Cat 5:** their headline excludes cat 5 and measures abstention separately, as we do (guard slice only). Keep consistent?

---------------------------------------------------------------------------------------------------

## Appendix: limits of this analysis

- I did not run, install or import any of their code; claims about call counts, context sizes and likely behaviour are inferred from the
  embedded listings, not measured.
- The embedded source ZIP and full-file appendices (about 45k lines) were skimmed for stage-relevant files (graph, vector, config, prompts,
  query context, evaluator); auth/admin/Postgres/Valkey/startup code was not studied beyond what the narrative states.
- I did not verify that the vendored `resolve_extracted_edges` issues one `resolve_edge` LLM call per extracted edge (the per-turn LLM call
  estimate in section 1.6 depends on it).
- Their numbers cannot be reproduced here; every "expected impact" is a capture-rate hypothesis on our measured class sizes, as in our own
  forensics, and should be treated as such.
