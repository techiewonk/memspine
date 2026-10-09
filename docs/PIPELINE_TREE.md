# MemSpine pipeline tree: write path (injection) and read path (retrieval to reader context)

Scope: the exact path a LoCoMo conversation takes through MemSpine in the local Qwen-stack arms
(`evals/arms/qs-eq06-roff.json`, `qs-eq06-rq4b4.json`, `qs-ejina-rjina.json`), run with
`--memspine-read-mode replay --memspine-batch-turns 32 --budget 4096 --top-k 10 --retrieval-only`
(see `evals/run_qwen_stack.sh`).

Every node gives: `file:line` (verified by reading the code), the controlling config key(s),
the default and our-arm value, and a "gap?" note (what can go wrong there). Anything not
confirmed from code is marked **UNVERIFIED**. Line numbers refer to the working tree on branch
`feat/local-qwen-stack`; `engine.py` is CRLF but line numbers are the same.

Abbreviations: `E` = `src/memspine/engine.py`; `S` = `src/memspine/`; `H` = `evals/memspine_evals/systems/memspine_system.py`
(the harness adapter); `SCH` = `src/memspine/config/schema.py`; `CONST` = `src/memspine/config/constants.py`.

---------------------------------------------------------------------------------------------------

## 0. Effective configuration of our arm (config layering)

Layer order, lowest to highest precedence (`S/config/loader.py:1-12`):
schema defaults (`SCH`) -> template chain (`S/config/templates/base.yaml`) -> user YAML (none) ->
environment `MEMSPINE_*` vars (`loader.py:_env_layer`; **UNVERIFIED** whether any are set on the
benchmark machine; none are set by `run_qwen_stack.sh`) -> runtime kwargs (the arm JSON plus the
adapter's own additions).

* The adapter builds `Engine(template="base", **overrides)` (`H:151-184`; `MemspineSystem.__init__` default
  `template="base"`, `H:83`; `experiments.py:361` passes no template). `base.yaml` is therefore the template, not the
  `assistant` default (`CONST:528`).
* The adapter forces: `storage.path` = `<tempdir>/memspine.db` (the arm JSON says `"tempdir"`, `H:166-172`, one file-backed
  store per item), `dotenv_path=None` (`H:175`), `read.record_access=False` default (`H:179-181`).
* Derived stores sit beside the DB file: LanceDB `<db>.lance`, Tantivy `<db>.tantivy` (`E:11610-11619`, `E:11681-11684`).

Resulting values that matter (arm = `qs-eq06-roff`; the other two arms differ only where noted):

| Key | Schema default | base.yaml | Arm JSON | Effective |
|---|---|---|---|---|
| `embedding.provider/model/dim` | fastembed / bge-small / None | - | st / Qwen3-Embedding-0.6B / 1024 | st, 1024-d, cuda, bf16 |
| `embedding.query_instruction` | None | - | "Instruct: Given a question about a user's past conversations, retrieve memories that answer it\nQuery: " | applied to QUERIES only (`st_local.py:111`) |
| `embedding.batch_size` | 32 | - | - | 32 |
| `read.hybrid` | True | - | - | **True** (vector + BM25) |
| `read.lexical_provider/analyzer/dates` | tantivy / default / False | - | - | tantivy, no stemming/stopwords, **no date words** |
| `read.rrf_k` | None -> `CONST RRF_K=60` | - | - | 60 |
| `read.fusion` | rrf | - | - | rrf |
| `read.temporal_leg` | False | **True** | - | **True** (see 2.4) |
| `read.temporal_relative/infer_year/soft/leg_mentions/rank` | False/False/False/False/midpoint | - | - | all default |
| `read.rerank` | off | "off" | roff: "off"; rq4b4: qwen3 + Qwen3-Reranker-4B, cuda, 4bit; ejina-rjina: jina v3.5, cuda | per arm |
| `read.candidate_pool` | 1 | (commented out) | - | **1** -> rerank pool = `top_k` = 10 |
| `read.rerank_keep/gate/blend/context/date_prefix/balanced` | None/None/None/0/False/False | - | - | all off |
| `read.scoring.{recency,importance,utility}_weight` | 1.0 / 1.0 / 0.5 | 0/0/0 | 0/0/0 | 0; relevance_weight stays 1.0, so score == relevance |
| `read.assembly.relative_floor` | 0.0 | 0.3 | 0.3 | 0.3 |
| `read.assembly.theta_abstain` | 0.25 (`CONST THETA_ABSTAIN`) | - | - | 0.25 |
| `read.assembly.mmr_lambda` (assembly MMR) | 0.7 | - | - | 0.7 |
| `read.resolve_relative_dates` | False | True | True | True; `relative_dates_anchored`=False, `relative_week`="calendar" |
| `read.order_by_time_for_ordering` | False | True | True | True |
| `read.render` | plain | - | - | **plain** (engine adds no date prefix; the harness does, H:536-540) |
| `read.default_mode` | auto | replay | - | replay (harness also passes mode explicitly) |
| `read.record_access` | True | False | False | False |
| `memories.{working,episodic,semantic}.enabled` | all False | True | - | working+episodic+semantic ON; **associative OFF** -> no graph store, no Ladybug, no entity nodes |
| `firewall.*` | enabled True, everything else off | - | - | enabled True, redact_secrets off, pii off, skip roles `[]`, skip_injected_recall off |
| `graph.provider` | auto | - | - | irrelevant: graph store is built only if `associative` is enabled (`E:1135`) |
| `decision.provider` | off | - | - | off (so no GLiNER2 anywhere) |
| `llm.roles` | {} | - | - | empty: every LLM stage is unavailable; the harness runs `--max-model-calls 0` |
| `workers.sleep_interval_seconds` | None | - | - | None: no background sleep loop (`E:1271`) |
| `cache.backend` | memory (max 65,536 entries) | - | - | per-engine in-process KV |
| `memories.episodic.policies.*` | {} | - | - | no `subject_tagging`, `forget_detector`, `correction_detector`, `consolidation` |

`describe()`-style config is whatever the CLI `--memspine-config` JSON holds; the run script does `cat arms/<arm>.json`
(`evals/run_qwen_stack.sh:12`). The three arms share everything except `embedding` and `read.rerank*`
(`evals/gen_qwen_stack_arms.py`, base = `local-combo-A.json`).

Harness parameters outside the engine config: `top_k=10`, `budget_tokens=4096` (`run_qwen_stack.sh:12`),
`batch_turns=32`, `read_mode=replay`, `dated=True`, `build_sleep=False` (no `--memspine-build-sleep`).

---------------------------------------------------------------------------------------------------

## 1. WRITE PATH (injection)

```
runner (per item) --> MemspineSystem.insert(turn)                         H:259
  |  turn = Turn(turn_id "D1:14", session_id "session_1", speaker, text [+ " [image: <blip_caption>]"],
  |              timestamp = session stamp "1:56 pm on 8 May, 2023")       datasets/locomo.py:130-147
  |
  +-> buffer in self._buffer; flush when len>=32 or the session_id changes   H:263-275   (batch_turns=32)
  |       (runner also calls flush() before every query and before build)    H:277, H:493
  |
  +-> MemspineSystem._deposit(turns)                                         H:286
        text    = f"{speaker}: {turn.text}"                                  H:293  <-- speaker NAME is content
        role    = "user" for EVERY turn (speaker is not a role)              H:298-315
        stamp   = parse_turn_time(timestamp)  UTC, formats at H:25-31        H:57
        1 turn  : write_messages([...], valid_from=stamp)                    H:298
        >1 turn : each message carries "timestamp": stamp (valid_from not passed) H:306-318
        namespace="eval", session_id=<session_N>, group_id=<session_N>
        |
        v
  Engine.write_messages(messages, namespace, session_id, group_id, valid_from)       E:3078
  |
  |-[1] contents = _depositable_contents(messages)                         E:3119 / E:3268
  |       drops roles in firewall.skip_message_roles ([]), recall-echo turns (skip_injected_recall False),
  |       applies _screen_text (redact_secrets/pii: both off) --> no-op in our arm
  |-[2] _prewarm_embeddings(contents)                                      E:3120 / E:3289
  |       embeds the batch in chunks of embedding.batch_size=32 into the E3 embedding cache
  |       (CachedEmbedding services/cache/semantic.py:34); <2 distinct texts => skipped.
  |       Qwen3 doc side = bare text, no instruction, no date, no speaker split (st_local.py:84-100,
  |       normalize_embeddings=True, vector sliced to dim 1024, max_seq_length<=8192).
  |       gap?: if this raises, NOTHING is written (retry-safe) but the whole 32-turn batch fails.
  |-[3] _prefetch_neighbours(ns, contents)  -> _NeighbourBatch            E:3121 / E:3141 / E:528
  |       ONE batched vector query (top_k = ANOMALY_MIN_NEIGHBOURS 8 + n_quarantined) per chunk so the firewall's
  |       neighbour check needs no per-turn query. Only when firewall.enabled and >=2 distinct contents.
  |-[4] async with _projection_batch()                                     E:3123 / E:10469
  |       projectors defer commits (Tantivy defer_commits) and checkpoint offsets once per call
  |       (E:10492 _flush_projections). Reads in between still see every event.
  |       gap?: Tantivy commit is forced by the first search/flush, so a query before flush would still be correct;
  |            a projector flush failure raises AFTER events are in the log (catch-up re-applies on restart).
  |
  +-> _write_turns(...)  per-turn loop                                     E:3176
        for each message:
        |- role filter  `role in fw.skip_message_roles`                    E:3201   (firewall.skip_message_roles=[], OFF)
        |- recalled-memory filter `_looks_like_recall`                     E:3203 / E:403   (firewall.skip_injected_recall=False, OFF)
        |- tags: assistant_claim (tag_assistant_claims False), recommendation (read.role_aware False),
        |        forget_request (episodic.policies.forget_detector off), correction (correction_detector off)   E:3226-3249   all OFF
        |- stamp = _parse_event_time(message.timestamp) or valid_from      E:3250
        |       gap?: if the harness stamp fails to parse (parse_turn_time -> None), valid_from silently becomes "now"
        |            (records.py valid_from default factory), collapsing the conversation's time axis. The ingest log
        |            exposes it (`valid_from`); no hard failure.
        |
        +-> Engine.write(content, memory_type="episodic",                  E:1325
                source=SourceInfo(role="user", channel="messages", message_id=session_id),
                actor="user", group_id=session_id, valid_from=stamp)
            |- reply_to link (none passed)                                E:1379
            |- `_caller_tags` strips reserved tags                         E:1393 / E:435
            |- subject_tagging "speaker:<Name>" tag                        E:1394-1400   (episodic.policies.subject_tagging: OFF)
            |- implicit parents / read ledger                              E:1401 / E:1556   (integrity.implicit_parents "off": no-op)
            |- build MemoryRecord (record_id uuid4, content_fingerprint, trust default .5, valid_from=stamp,
            |   recorded_at=record_time() strictly increasing, status ACTIVATED)    E:1405-1419 ; core/records.py:200-202
            |   NOTE: NO relative-date resolution here. Relative phrases are resolved at READ time (section 2.9).
            |- _parent_trust_cap(...)  -> None unless integrity.enabled    E:1420 / E:3046
            |- per-namespace asyncio.Lock                                  E:1437
            |
            +-> _write_locked_ex                                           E:3341
                  |
                  |-[A] FIREWALL / PRIVACY: _screen_write                  E:3396
                  |      |- _redact_fields: secrets/PII/sensitive-topic masking/tagging, pii_default_tier    E:3446
                  |      |        firewall.redact_secrets=False, pii="off", sensitive_topics=False  => no-op
                  |      |- _assess_write(record)                          E:8225   (firewall.enabled=True)
                  |      |     neighbour sims: 8 nearest NON-held vectors (batched prefetch or live query)  E:8261-8273
                  |      |     recent_contents(50) for MINJA bridge                                      E:8275
                  |      |     Firewall.assess                              core/firewall.py:259
                  |      |        trust = role matrix: user 0.7, assistant .5, tool .4, operator/system .9  core/policies/trust.py:28
                  |      |        instruction_shaped regexes (base set only; instruction_extended/semantic_risk/query_anomaly OFF)  firewall.py:46-62
                  |      |        embedding_outlier: nearest cosine < 0.05 with >=8 neighbours             firewall.py:295-303
                  |      |        minja_bridge_prefix: first 96 chars equal to one of the last 50 contents  firewall.py:304-316
                  |      |        should_quarantine: trust<0.25 OR (anomalous AND not operator/system)
                  |      |                           OR (instruction-shaped AND (external channel OR tool/assistant role))  trust.py:138-160
                  |      |     size_anomaly / protected_keys (max_content_chars None, protected_keys [])    E:3409-3428  OFF
                  |      |- verdict.apply: stamps trust / instruction_flag / quarantined+status QUARANTINED    firewall.py:229
                  |      |- integrity trust cap / principal reputation  (integrity.enabled False)          E:3433-3443  OFF
                  |      gap?: a LoCoMo turn that is a verbatim 96-char prefix repeat of any of the last 50 turns
                  |           (chatty duplicates), or a far outlier in embedding space, is QUARANTINED (inert: not retrievable)
                  |           although the adapter reports it as written. The ingest log records `written: true` and
                  |           does NOT record `quarantined` or `trust` (H:404-447). **Check this first on any "never retrieved" gold.**
                  |
                  |-[B] quarantined?  -> WRITE event stored inert, return "quarantined"              E:3351-3378
                  |       (still projected into vector+lexical indexes, then dropped at read by _gate_hits, E:3799-3810)
                  |
                  +-> _write_screened                                      E:3516
                        |- CUE_TAG branch (not used)                       E:3525
                        |- memory_type == "semantic" -> SemanticMemory.write (dedup MinHash/LSH + cosine .92, entity
                        |      extraction, M4 conflict ladder)             E:3542 ; memories/semantic/store.py:126-220
                        |      NOT TAKEN: write_messages always uses memory_type="episodic" (E:3254).
                        |      => dedupe, entity extraction (entity_extraction off|llm|gliner, E:11416) and the
                        |         conflict ladder are never applied to LoCoMo turns. Duplicates are stored as-is.
                        |- plain WRITE event for episodic                   E:3554
                              |
                              v
                        Engine._append_and_project(event)                  E:10372
                          |- _inherit_governance (consent/PII inheritance from parents; no parents => no-op)  E:10418
                          |- storage.append_event -> memory_events row, seq  services/storage/sql_base.py:129
                          |       (event_log.mode "full": the append-only SOURCE OF TRUTH; compress False)
                          |- for each projector, in this order (E:1230-1255):
                          |     1. RecordProjector      -> memory_records read model (SQLite)       services/storage/projector.py:90
                          |     2. VectorProjector      -> embed(record.content) (cache hit) + LanceDB upsert   services/vector/projector.py:25
                          |             table per embedder id; cosine; flat exact scan unless quantization (vector.quantization auto,
                          |             manifest declares none) ; services/vector/lancedb_store.py:147 (upsert), :210 (query)
                          |     3. LexicalProjector     -> Tantivy index of record.content ONLY (read.hybrid True)   services/lexical/projector.py:28
                          |             body = content (+ date words only if read.lexical_dates, OFF)      services/lexical/tantivy.py:294
                          |             default tokenizer: alnum runs, lower-case, NO stemming, NO stop words
                          |     4. GraphProjector       -> NOT REGISTERED (associative OFF)
                          |- query-encoder eviction hook                       E:10397
                        then:
                        |- _corroborate(ns, record)                          E:8303   (only does work if quarantined rows exist)
                        |- _evolve_links(ns, record)                         E:10074  (returns at once: associative is None)
                        |- working-memory page-out enforcement               E:3567   (memory_type != working; no-op)
            |- _reopen_session (sessions.passive_after unset => skipped)     E:1442
            |- reply link (none)                                             E:1444
```

Return path: `write_messages` returns the `MemoryRecord` list in message order (E:3264). The adapter
maps `record_id -> turn_id` with `_align` (`H:319-323`, `H:621-642`), and writes the ingest audit line when
`MEMSPINE_FORENSICS_DIR` is set (`_write_ingest_log`, `H:404`): turn, session, source text, stored text,
`text_identical`, `valid_from`, `memory_type`, `group_id`, `session_id`, `batch_size`.

### 1.1 Write-side steps that exist in the engine but do NOT run in our arm

| Step | Where | Why off | Key |
|---|---|---|---|
| Dedup (SimHash/MinHash LSH, cosine .92) | `memories/semantic/store.py:174-220`, `CONST DEDUP_COSINE_THRESHOLD` | only the semantic door dedupes; turns are episodic | `memories.semantic.policies.dedup` |
| Entity extraction (LLM or GLiNER) | `E:11416` `_build_extractor`, `memories/semantic/entities.py` | `entity_extraction` = off and turns never reach the semantic door | `memories.semantic.policies.entity_extraction` (off\|llm\|gliner) |
| GLiNER2 decision provider (planner / entity hook) | `E:10654-10690`, `services/decision/gliner2_decision.py` | provider off | `decision.provider` |
| Graph projection, entity nodes, `mentions` edges, communities | `memories/associative/projector.py:57`, `entities.py` | associative disabled | `memories.associative.enabled`, `.policies.entity_nodes`, `graph.provider`, `graph.entity_embeddings` |
| Ladybug graph store | `E:11698-11747` | only built when associative is on | `graph.provider` (auto picks ladybug if the package exists) |
| Rule-based turn mentions for graph | docs/USAGE.md:286 | needs entity_nodes `{turn_mentions: true}` | `memories.associative.policies.entity_nodes` |
| `A-MEM` link proposals | `E:10074` `_evolve_links` | associative None | - |
| Relative-date mining at write (`happened:` tags) | `workers/pipelines.py:3166` | needs sleep + LLM/rules miner | `consolidation.mine_event_dates` |
| Lexical date words in BM25 | `tantivy.py:294`, `core/temporal_query.py:597` `date_words` | flag off | `read.lexical_dates` (own index + projector `lexical:dates`) |
| English analyzer (stemmer + stop words) | `tantivy.py:163-172` | flag off | `read.lexical_analyzer` |
| Speaker tags `speaker:<Name>` | `E:1394` | policy off | `memories.episodic.policies.subject_tagging` |
| Secret/PII redaction, sensitive-topic tags | `E:3446` | off | `firewall.redact_secrets`, `firewall.pii`, `firewall.sensitive_topics` |
| Integrity / lineage trust caps | `E:3433`, `E:3046` | off | `integrity.enabled` |
| Session lifecycle (PASSIVE sessions) | `E:1442`, `workers/pipelines.py:725` | `passive_after` unset | `memories.episodic.policies.sessions.passive_after` |
| Correction / forget detectors | `E:3235-3249` | off | `memories.episodic.policies.{correction_detector,forget_detector}` |

### 1.2 Background / sleep path (maintenance and derived memories)

```
Engine.sleep()                                   E:10214      (NOT called in our runs: H:358 build() only sleeps if
  |                                                           --memspine-build-sleep; run_qwen_stack.sh omits it;
  |                                                           workers.sleep_interval_seconds None => no scheduler E:1271)
  +-> run_sleep_cycle(runner, ctx)               workers/schedule.py:74
        order (workers/schedule.py:30-47):
        [retention_expire if retention.classes] -> consolidate -> mine_facts -> [predict_calibrate] -> anticipate
        -> reflect_profile -> extract_graph -> [rule_edges] -> summarize_entities -> reorganize -> check_watches
        -> session_lifecycle -> decay_sweep -> compress -> sleep_compute (noop hook) -> event_log_prune
```

| Stage | Code | What it writes | Needs | Our arm (if sleep were run) |
|---|---|---|---|---|
| `consolidate` | `workers/pipelines.py:362` | one semantic SUMMARY record per CLOSED session (>=3 turns, gap 30 min, extractive first-sentence summary if no `summarize` LLM role; `core/policies/consolidation.py:180`) | triggers default `[session_end, sleep_cycle]` | would run and ADD summary records (semantic type) that compete in retrieval |
| `mine_facts` | `pipelines.py:3166` | atomic semantic facts (`atomic_fact` tag, parents = turns) | `consolidation.mine_facts` + (`miner: llm` -> `extract` role; `miner: rules` -> `core/rule_miner.py:240`, no model) | off |
| `predict_calibrate` | `pipelines.py:3406` | surprise facts | `consolidation.predict_calibrate` + LLM | off |
| `anticipate` | `pipelines.py:3256` | retrieval cues on answering turns | `consolidation.anticipate`, LLM | off |
| `reflect_profile` | `pipelines.py:3294` | reflective profile insights | `consolidation.reflect_profile`, reflective memory, LLM | off |
| `extract_graph` / `rule_edges` | `pipelines.py:1885` / `:2270` | edge facts + asserted links | `semantic.policies.extract_graph`, `extract_edges` role / `associative.policies.rule_edges` | off |
| `summarize_entities` | `pipelines.py:2492` | entity summaries | associative + `entity_summaries` | off |
| `reorganize` | `pipelines.py:1080` | community summary parents | graph store + `[community]` extra | skipped (no graph) |
| `check_watches` / `session_lifecycle` / `decay_sweep` / `compress` / `event_log_prune` | `pipelines.py:881/725/772/825/334` | state transitions only | - | bookkeeping |
| list cards (`list_cards`) | `workers/list_cards.py`, `E:11175` | person x class card | `consolidation.list_cards` + miner | off |

Gap notes: (a) sleep-time derived memories are the only place MemSpine builds anything beyond raw turns, and in
our arms none of it exists; every number from these arms measures **raw-turn retrieval only**. (b) If anyone
turns `consolidate` on, summary records enter the same index and cost top-10 slots (see `read.raw_turn_floor`,
`read.cards`).

---------------------------------------------------------------------------------------------------

## 2. READ PATH (retrieval to the context text given to the reader)

### 2.0 Overview tree

```
runner: MemspineSystem.query(text, budget_tokens=4096, top_k=10)                H:488
  |- flush() buffered turns                                                     H:493
  |- with search_forensics() as stages:                                         H:504 ; E:660 (installs _FORENSICS sink)
  |     Engine.read(text, namespace, mode="replay", budget_tokens, top_k)       H:506 ; E:4867
  |       replay_window=2 (signature default, nobody overrides it)              E:4874
  |       |
  |       +-> _read(...)                                                        E:4981
  |             |- mode validation, reply reserve (reply_reserve_tokens=0)      E:4995-4999
  |             |- strong = _raw_evidence_strong()      -> False (cards off)    E:6050
  |             |- headers = _read_headers()            -> [] (all header flags off)   E:5739
  |             |- count_share = _count_allowance()     -> 0                    E:5852
  |             |- gated = _cards_gated()               -> False                E:6070
  |             +-> _read_routed(mode="replay", budget = 4096 - 0 - 0)          E:5026
  |                   (full/auto listing block skipped: mode is "replay")       E:5049
  |                   (compose branch skipped)                                  E:5095
  |                   (aggregate_in_replay off)                                 E:5126
  |                   |
  |                   +-> _assemble_core(query, ns, 4096, top_k=10)             E:4379
  |                   |     want = top_k * read.candidate_pool = 10 * 1 = 10    E:4400
  |                   |     |- probes (statement_probe/multi_intent_split OFF)  E:4401-4408
  |                   |     +-> _search(query, ns, want=10, keep_k=10)          E:3853   <== retrieval proper (2.1-2.7)
  |                   |     |- PRF / second_round / cluster_expand / raw_turn_floor / session_cap: all OFF   E:4429-4517
  |                   |     |- concentration_filter, facts_to_sources, type_quotas: OFF  E:4519-4537
  |                   |     |- integrity trust-weighted rescale: OFF            E:4538
  |                   |     |- evidence_signal: OFF                             E:4549
  |                   |     |- persona pinned from working memory: none         E:4559
  |                   |     |- lead section (standing/timelines): OFF           E:4571
  |                   |     |- _decorate(...)  relative dates etc.              E:4574 / E:4687  (2.9)
  |                   |     |- gist_after: OFF                                  E:4575
  |                   |     +-> AssemblyPolicy.assemble(scored, budget)         E:4586 ; core/policies/assembly.py:143 (2.8)
  |                   |
  |                   +-> REPLAY EXPANSION (the +-2 turn window)                E:5145-5241   (2.10)
  |                   +-> _render(...) plain, no date prefix                    E:5230 / E:4720
  |             |- _count_section / _duration_section (OFF)                     E:5022-5023
  |             +-> _attach_headers (no headers => unchanged)                   E:5024 / E:6128
  |       |- _consent_context (consent.enforce False => unchanged)              E:4975 / E:7294
  |
  |- _write_forensics(directory, query, stages, assembled)                       H:524 / H:449
  |- harness formatting: one line per record = "[YYYY-MM-DD] " + record.content ; lines joined with "\n"   H:535-555
  +-> RetrievedContext(text=body, tokens, evidence, ...)                         H:565
runner then truncate_to_budget(text, 4096) FROM THE TAIL (heuristic 4 chars/token)   evals/memspine_evals/runner.py:793-796 ; tokens.py:74
```

### 2.1 Query embedding (start of `_search`)

`E:3893`: `[query_vector] = await embed_queries(self._embedder, [query])`.
* `embed_queries` (`services/embedding/base.py:52`) -> `CachedEmbedding.embed_queries` (`services/cache/semantic.py:64`, cached under key
  prefix `embq<variant>`, where `variant` hashes the instruction) -> `SentenceTransformersEmbedding.embed_queries` (`st_local.py:111`).
* Qwen arm: `query_prompt_name` is None, so the `query_instruction` string is **prepended to the raw question text**
  ("Instruct: Given a question about a user's past conversations, retrieve memories that answer it\nQuery: <question>").
  Jina arm: `query_prompt_name="query"` (and `document_prompt_name="document"` on the doc side) via sentence-transformers prompts.
* Config: `embedding.query_instruction`, `embedding.query_prompt_name`, `embedding.document_prompt_name`.
* Docs are embedded bare (`embed`, `st_local.py:84`). Only the question side carries the instruction: asymmetric, correct for Qwen3-Embedding.
* gap?: the question is embedded as one string; there is no query expansion in our arm (all probes off). Speaker names in the question
  ("Caroline") are part of the vector query but ALSO appear in nearly every stored turn ("Caroline: ..."), so they carry little signal.

### 2.2 Candidate fetch size and widening

* `use_hybrid = read.hybrid and lexical store exists` -> True (`E:3896`).
* `base_fetch = top_k * LEXICAL_FETCH_MULTIPLIER (3)` = **30** per leg when hybrid (`E:3906`; `CONST LEXICAL_FETCH_MULTIPLIER=3`). Here `top_k` is the
  `_search` argument = `want` = 10.
* A date filter or record scope would raise `fetch_k` to the whole namespace (`E:3912`); not used by the harness (`as_of` not passed).
* Loop `while True` (`E:3921`): `fetch_k = base_fetch * widen`; start `widen=1`. Leave when `len(candidates) >= top_k`, or both legs returned
  fewer than `fetch_k` hits (index exhausted), or `widen >= max_widen` (64) (`E:4046-4050`); otherwise `widen *= 4`.
  The gates (2.5) are what can force a widen (quarantined/archived/superseded rows consume fetch slots).
* Tie handling: each leg is `settle_ties(...)` (`core/ties.py:51`): asks for k+1, doubles up to 8x while the k-th score ties the last, orders ties
  by (recorded_at, fingerprint, id).
* gap?: the per-leg window is only 30 and the **fused list is then cut to `top_k * widen` = 10** (`E:3999`), so anything the fusion ranks 11+ is
  invisible downstream unless `candidate_pool` > 1. This is the single hard recall cap in the pipeline (see Gap hypotheses).

### 2.3 Retrieval legs (all inside the `while` loop, `E:3923-3977`)

| # | Leg | Code | Config key | Default | Our arm | Gap? |
|---|---|---|---|---|---|---|
| 1 | **Vector** (cosine, LanceDB, namespace prefilter) | `_vector_leg` `E:3729` ; `lancedb_store.py:210` | - | always | ON, 30 hits, flat exact scan (no ANN index; `_rescore_active` False) | Qwen3 vectors are normalised, cosine==dot; one vector per whole turn (no chunk/sentence vectors) |
| 2 | **Lexical BM25** (Tantivy) | `_lexical_leg` `E:3763` ; `tantivy.py:285-330` | `read.hybrid`, `read.lexical_provider`, `read.lexical_analyzer`, `read.lexical_dates` | on / tantivy / default / off | ON, 30 hits | query = OR of ALL alnum tokens (<=64 terms, <=1024 chars), no stop-word removal, no stemming: "did/the/when" terms add noise, "camping" != "camp"; date words not indexed |
| 3 | Core-terms BM25 | `_metadata_legs` `E:2010` | `read.core_terms_leg` | False | OFF | would drop interrogatives for a cleaner BM25 query |
| 4 | **Temporal** (turns whose `valid_from` is in an absolute date span named in the question) | `_metadata_legs` `E:2039-2071` ; `core/temporal_query.py:171` | `read.temporal_leg` (+ `temporal_relative`, `temporal_infer_year`, `temporal_soft`, `temporal_leg_mentions`, `temporal_rank`, `temporal_leg_event_dates`) | False | **ON** (base.yaml) but sub-flags OFF | Only fires when the question contains an absolute date/year ("in 2023", "7 May 2023"); with `temporal_relative` off, "last week/yesterday" give no span; no year -> no span (`temporal_infer_year` off). Fires rarely on LoCoMo. When it fires, a LoCoMo session shares ONE timestamp, so the leg = first N turns of that day in write order, unscored (all hit score 1.0). Listing ALL records per query is O(N) (`E:2032`) |
| 5 | Metadata leg (entity named in question) | `E:2072` | `read.metadata_leg` | False | OFF | |
| 6 | Speaker leg | `E:2074` | `read.subject_leg` (needs `episodic.policies.subject_tagging`) | False | OFF | |
| 7 | Assistant/recommendation leg | `E:2077` | `read.role_aware` | False | OFF | |
| 8 | Sentence leg (best sentence overlap) | `E:2080` | `read.sentence_leg` | False | OFF | |
| 9 | Recency leg | `E:2083` | `read.recency_leg` | False | OFF | |
| 10 | View-tag leg | `E:2087` | `read.view_tag_leg` (needs `mine_multiview`) | False | OFF | |
| 11 | Entity (proper-noun) leg | `E:2090-2095` ; `temporal_query.py:501` | `read.entity_leg` | False | OFF | |
| 12 | Speaker probe (vector probe "Name: core terms") | `E:2096-2109` | `read.speaker_probe` | False | OFF | |
| 13 | Probe legs (extra vector + BM25 per probe text) | `_probe_legs` `E:1948`, caller `E:3941` | `read.statement_probe`, `read.multi_intent_split`, `read.prf_expansion`, planner subqueries | False | OFF | |
| 14 | Query-encoder (cue) leg | `_encoder_legs` `E:1977` | `read.query_encoder` ("cues") | none | OFF | |
| 15 | Graph leg (entity walk) | `_graph_leg` `E:2236` | `read.graph_leg`, `graph_depth` 2, `graph_leg_k` 10, `graph_min_trust` | False | OFF (no graph) | |
| 16 | Community gate | `E:2595` | `read.graph_communities` | False | OFF | |
| 17 | Cohesion leg (turns within 5 min of top-3 anchors) | `_anchor_legs` `E:2389` ; `temporal_query.py:530` | `read.cohesion_leg` | False | OFF | LoCoMo turns in a session share a timestamp, so cohesion would equal "same session" |
| 18 | Entity-expand leg | `E:2424` ; `temporal_query.py:550` | `read.entity_expand_leg` | False | OFF | |
| 19 | Graph node search | `_graph_node_legs` `E:2286` | `read.graph_node_search` (+`graph.entity_embeddings`) | False | OFF | |
| 20 | MaxSim sentence leg (embeds candidate sentences) | `_maxsim_leg` `E:2344` | `read.maxsim_leg` | False | OFF | |
| 21 | Person/time structured leg (LLM planner v3) | `_person_time_leg` `E:2115` | `read.planner: llm`, `planner_version: v3` | rules | OFF (needs an LLM role) | |
| 22 | Cluster legs | `_cluster_legs` `E:5984` | `read.cluster_expand` | False | OFF | |
| 23 | Precomputed `fused_legs` | `E:3963` | internal | - | none | |

* Per-leg floors: `read.leg_min_scores` (`E:3964`, default `{}`): OFF.
* Date filter / scope pruning of legs: `E:3971-3977`: not active.

### 2.4 Fusion (inside `_search`)

* Runs when `use_hybrid or extra_legs` (`E:3978`). Our arm: always (hybrid).
* Weights: `_leg_weights_for(query)` (`E:2443`) from `read.leg_weights`, `read.leg_weights_by_shape`, `read.short_query_lexical_weight`;
  all empty/None -> `weights=None` -> unweighted.
* **RRF** (`rrf_fuse`, `services/lexical/base.py:80`): score = sum over legs of `1/(k + rank)`, `k = read.rrf_k or 60`. Ties broken by per-leg rank tuple.
  Alternative `read.fusion: minmax` (`minmax_fuse`, `base.py:119`): OFF.
* `fused = fused[: top_k * widen]` (`E:3999`): **top 10** at widen 1.
* Normalisation to [0,1]: `ranked = score / rrf_max`, with `rrf_max = legs/(k+1)` and `legs = 2 + len(extra_legs)` under hybrid (`E:4006-4014`).
  Without extra legs `rrf_max = 2/61`; with the temporal leg active `3/61`. Consequence: the relevance scale (and so the abstention/floor
  behaviour in 2.8) shifts per query depending on how many legs fired.
  Examples (no rerank): a record at rank 1 in both legs = 1.0; rank 10 in vector only = 0.436; rank 30 in vector only = 0.339 (all above the 0.3 floor).
* gap?: RRF with k=60 over a vector leg and an OR-of-every-token BM25 leg is rank-based; a gold item that is rank 3 in vector but absent from
  BM25 (typical for paraphrase questions) scores 1/63 / (2/61) ~ 0.48, while a lexical-noise item present in both legs at ranks 5/5 scores 0.94.

### 2.5 Gates (after fusion, before the cut) `_gate_hits` `E:3786`

For each fused id: `storage.get_record` -> drop if missing; status != ACTIVATED (unless `as_of`); `quarantined`; consent (`consent.enforce` off);
`memory_type == "shared"`; cue handling (`anticipatory_cues` off); group/tag filter (none); `_passive_hidden` (no passive sessions); memory-type filter; cold-tier
`inflate`. Then GP-9 community filter (n/a), date-filter re-check (none), `hide` callback (None).
Config: `consent.*`, `read.anticipatory_cues`, `memories.episodic.policies.sessions.passive_after`, `integrity.*`.
gap?: quarantined firewall rows (section 1 [A]) die HERE silently, after having consumed leg slots.

Forensics snapshot taken just BEFORE the gate (`E:4017-4024`): `vector` (30), `lexical` (30), `extra_legs`, `fused` (= the already-cut top 10 of ranked ids and normalised scores).

### 2.6 Post-gate candidate transforms and the cut

* `read.anticipatory_cues` dedup (`E:4051`): OFF.
* `read.graph_rerank` (`E:4058`, `_graph_rerank` `E:2495`): OFF.
* `read.causal_walk` (`E:4061`, `_causal_walk` `E:2549`): OFF.
* `rerank_balanced` (`_balanced_pool` `E:896`, applied at `E:4076`): OFF.
* **`candidates = candidates[:top_k]`** (`E:4080`): top_k = 10 (= `want`).
* `read.static_prefilter` (`E:4082`, `_static_prefilter` `E:873`), `read.static_embedding_prefilter` (`E:4087`, `E:8640`): OFF.
* `read.relevance_filter` (`E:4093`, `_relevance_filter` `E:10882`, needs LLM role): OFF.

### 2.7 Rerank stage `E:4095-4148` (OFF in roff; ON in rq4b4 and rjina)

* Provider: `_rerank_provider()` `E:8613` -> `build_reranker` (`services/rerank/factory.py`): `qwen3` -> `Qwen3Reranker` (`services/rerank/qwen3_rerank.py:66`),
  `jina` -> `JinaReranker` (`jina_rerank.py:29`); lazy load; any failure -> reranker None, stage skipped (sticky), counted in `rerank_stats`.
* Gating: `read.rerank_max_top_k` (None), `read.skip_rerank_for_ordering` (False) -> `E:4096-4103`.
* Forensics: `pool` = the post-cut candidates (`E:4106`); `reranker` id.
* **Pool size: 10** (`candidate_pool=1`, so `want = 10`). The reranker only REORDERS the fused top-10; it cannot rescue gold ranked 11+ in fusion.
  Raising `read.candidate_pool` (max 10) makes `want = 10 * pool` (up to 100) and is the only way to widen it (`E:4400`); `read.rerank_keep` then trims after rerank (`E:4172`).
* Document text sent to the reranker: `concat_background(record)` (`services/rerank/base.py:33`) = `"[type: episodic | channel: messages]\n" + content`;
  no date (unless `read.rerank_date_prefix`, `E:4110`), no neighbour turns (unless `read.rerank_context`, 0..3, `E:4116` / `_with_session_neighbours` `E:2457`).
* Qwen3 scoring: instruction (default "Given a question about a user's past conversations, judge whether the memory helps answer it", override `read.rerank_instruction`),
  prompt per `qwen3_rerank.py:45-56`; P(yes) from yes/no logits (`:156`); batch 8; max_length 8192; 4bit bitsandbytes for the 4B model (`:111`).
* Jina scoring: listwise `model.rerank(query, docs)`; only the first 64 docs scored, tail 0.0 (`jina_rerank.py:65-75`).
* Score use (`E:4128-4143`): `read.rerank_gate` (None) / `read.rerank_blend` (None) both off, so
  **relevance := min-max normalised reranker score over the 10 candidates** (`_minmax_normalize` `E:931`): best = 1.0, worst = 0.0.
  The original fusion score is thrown away.
* Failure: logged, `_rerank_failures += 1`, original fusion scores kept (`E:4144`).
* Forensics: `rerank_scores` = raw scores (`E:4123`).
* gaps?: (1) the reranker never sees dates or neighbours; (2) min-max forces one candidate to exactly 0.0 and one to 1.0 whatever the raw scores are, which interacts
  badly with the 0.3 `relative_floor` and the 0.25 abstain threshold downstream (2.8); (3) `rerank_keep` has no effect with `candidate_pool=1`.

### 2.8 Composite scoring, MMR, final ranked list (`_search` tail) and assembly

`_search` tail:
* `composite_score(record, relevance)` (`core/policies/scoring.py:56`): with recency/importance/utility weights 0 and relevance weight 1 the score **equals the relevance** (`:93-102`). `E:4149`.
* Integrity re-ranking: OFF (`E:4153-4166`).
* `rank_pairs` (`core/policies/assembly.py:39`): score desc, then `valid_from`, then fingerprint (deterministic). `E:4167`.
* Embedding MMR `read.mmr_lambda` (G-10, `_mmr_order` `E:2312`): None -> OFF.
* `rerank_keep` cut (`E:4172`): OFF.
* `record_access` RETRIEVE event (`E:4175`): OFF in these arms.
* Forensics `final` = this list (`E:4185`), i.e. **10 (record, score) pairs BEFORE assembly's abstain/floor/MMR**. This is "the final hit list" the analysts see.

`AssemblyPolicy.assemble` (`core/policies/assembly.py:143`; call `E:4586`), budget = `4096 - lead_cost(0)`:
1. **Abstain**: if no evidence has score >= `theta_abstain` (0.25), returns no evidence records (only a persona) (`assembly.py:112`, `:164`). Under rerank the best score is always 1.0, so abstention can never fire; without rerank it can if the best fused relevance < 0.25.
2. **Relative floor**: drop evidence scoring < `relative_floor * best` (0.3 * best) (`assembly.py:121`, `:174`).
3. **Greedy MMR selection** with word-set Jaccard redundancy, `mmr_lambda=0.7` (this is `read.assembly.mmr_lambda`, different from `read.mmr_lambda`), until the token estimate (`len(text)//4 + 1`) reaches the budget (`:207-233`). `dedupe_jaccard=1.0` (off), `latest_slots=0` (off), compression off (`E:4588`, `read.compression`).
   With 10 short turns and a 4096 budget the budget never binds, so MMR only reorders.
4. Cache-aware placement: sort by memory-type stability rank, then score desc (`:243-254`).
5. Return `AssembledContext(records, boundary_index, abstained, tokens_used)`.

gap?: with rerank, the **relative floor (0.3) is applied to min-max-normalised reranker scores**, so every candidate in the lowest ~30% of the reranker's score range for that question is dropped before replay windows are built; a bimodal P(yes) distribution therefore collapses the context to the 1-3 "yes" turns. Without rerank (RRF scores 0.34-1.0) the floor rarely removes anything. So `final` (10) is NOT the set that gets windows in rerank arms. **UNVERIFIED by run**; derived from the code above. Check `context_records` in `forensics.jsonl` vs `final`.

### 2.9 Per-record decoration (`_decorate`, `E:4687`) applied to every record entering context

* B9 claims-only (integrity off), then **relative-date annotation** if `read.resolve_relative_dates` (ON): `_annotate_dates` (`E:6489`) -> `core/temporal_resolve.annotate` appends
  `[= <absolute date>]` after phrases such as "yesterday", "last Friday", "two weeks ago", "last week". Anchor = the record's own `valid_from` (`core/event_date.py:131`).
  Rules only; vague phrases ("recently", "the other day") are left alone (`temporal_resolve.py:5-10`).
  Config: `read.relative_dates_anchored` (False), `read.relative_week` ("calendar").
  gap?: "last week" resolves to the previous Monday-Sunday calendar week, but LoCoMo gold uses "the week before <session date>"; `relative_dates_anchored: true` / `relative_week: preceding_7_days` exist but are OFF (G13/#58).
* Instruction-flag wrapper and marker escaping (`_wrap_instruction` `E:6509`; `escape_markers` defangs engine marker strings found in stored text): no-op for ordinary text.
* Current-state view (`read.current_state_view`, `E:2728`): OFF.
* Untrusted-note wrapper (`integrity.untrusted_wrap_below` 0): OFF.

### 2.10 THE +-2-TURN NEIGHBOUR EXPANSION ("replay") `E:5145-5241`

This is where a 10-hit list becomes a 16-37-record context. It lives in `Engine._read_routed` (`E:5026`), after `_assemble_core` returns `base`, and applies in mode `replay` (and `auto` when it resolves to replay):

```
episodic_hits = [r for r in base.records if r.memory_type == "episodic"]          E:5145   (assembled order: score desc)
for atomic-fact records in base: add their best source turn (H6)                 E:5148-5151   (no facts in our arm)
if mode == "retrieve" or no episodic memory or no hits: return plain render       E:5152
sessions = EpisodicMemory.sessions(ns, SESSION_GAP_MINUTES=30)                    E:5154 ; memories/episodic/store.py:57 ; sessions.py:44
where    = {record_id -> its derived Session}                                     E:5155
chosen   = non-episodic records of base ; used = their token estimate             E:5173-5175
for rank, hit in enumerate(episodic_hits):                                        E:5177
    ids = session.record_ids (time-ordered ids of that derived session)            E:5179  (read.replay_topic_segments False: no topic cut)
    at  = index of the hit in ids
    span = range(at - replay_window, at + replay_window + 1) clipped to the session  E:5185   replay_window = 2
    visit order: the hit first, then neighbours nearest first, older first on a tie  E:5186
    skip ids already chosen (windows of adjacent hits overlap and dedupe)
    neighbour = _replay_neighbour(rid)  -> re-fetched, gated (_live_view), inflated, _decorate'd  E:5190 / E:5243
    cost = len(content)//4 + 1 ; if used + cost > budget_tokens(4096): skip it       E:5193-5195
    add; track best_window for rank 0
    (read.reply_links: also add the answered message; OFF)                          E:5201-5216
stable = non-episodic chosen ; turns = episodic chosen sorted by chrono_key           E:5217-5221
        (valid_from, recorded_at, fingerprint, id)  => CHRONOLOGICAL across sessions, write order within
(read.evidence_first: put best window first; OFF)                                    E:5222
return ReadResult("replay", _render(query, AssembledContext(records=[*stable, *turns], ...), budget))   E:5228
```

Key properties and gaps:
* Window size is the `read(..., replay_window=2)` argument default (`E:4874`); there is **no config key** for it in `ReadConfig`. The harness never passes it (`H:506-513`). Only a code/call change alters it. (`compose_replay` uses the same parameter for compose reads.)
* The expansion works on **derived sessions**, not on the dataset's `session_id`: records are sorted by `valid_from` and split wherever the gap to the previous turn is >= 30 minutes (`sessions.py:44-57`). In LoCoMo every turn of a session has the identical timestamp, so one dataset session = one derived session, **unless** two dataset sessions are < 30 minutes apart (they would merge: windows could cross sessions) or a stamp failed to parse and turns got "now" times (everything would collapse into one giant session, ordered by write order).
* Neighbours are positions in that sorted list (by `recorded_at` within the same stamp), i.e. dataset turn order. Quarantined or deleted turns are absent from the list (`EpisodicMemory._active`, `store.py:37`), so a window "jumps over" them.
* Hits come only from `base.records`, which have ALREADY survived abstain, floor and MMR (2.8). A hit dropped by the floor gets no window.
* Order of window filling = assembly score order; budget only matters beyond ~4096 tokens estimated; the **harness then truncates from the tail** (`runner.py:793`, `tokens.py:74`) using its own count that also includes the harness's 13-char date prefix per line (`H:539`). Because the final order is chronological, a truncation would drop the LATEST turns, not the least relevant ones.
* Windows do not use the rerank score for ordering of the final text; the reader sees a chronological transcript with hits unmarked (no marker distinguishes hit from neighbour; `read.section_captions` and `evidence_first` are OFF).

### 2.11 Final render and the text the reader sees

* `_render` (`E:4720`): lead blocks excluded; `read.focused_excerpt` (OFF); for ordering questions (`core/query_shape.is_ordering`, `order_by_time_for_ordering` ON) volatile records are sorted chronologically (already true in replay); `read.present_order` default relevance (irrelevant after the chrono sort); `read.render == "plain"` so **no dated render and no gap markers inside the engine** (`E:4756-4758`). Budget fit loop only runs for the dated render.
* Back in the harness (`H:535-555`): for each record, `prefix = "[YYYY-MM-DD] "` from `record.valid_from` (day only, no weekday, no time of day), `line = prefix + record.content`; the content has the `[= date]` annotations from 2.9; body = lines joined by `\n`. `tokens = assembled.tokens_used` (engine estimate) else the harness counter.
* The reader prompt (readers.py) and judge are outside the engine; the retrieval-only screens skip them.

---------------------------------------------------------------------------------------------------

## 3. Per-node reference table (config, default vs arm, gap)

| Node | file:line | Config key(s) | Default | Our arm | What can go wrong |
|---|---|---|---|---|---|
| Turn text build | H:293 | - | "Speaker: text" | same, image captions appended by dataset adapter (`datasets/locomo.py:136-140`) | Speaker prefix pollutes BM25 and embeddings with a token present in half of all turns |
| Event time | H:57, E:3250 | - | session stamp UTC | same | Day-resolution only (all turns of a session identical); unparsable stamp => "now" |
| Batching | H:263-284, E:3078 | `--memspine-batch-turns` | 1 | 32 | A session boundary flushes; batches never mix sessions (OK) |
| Embedding prewarm | E:3289 | `embedding.batch_size` | 32 | 32 | n/a |
| Neighbour prefetch | E:3141 | `firewall.enabled` | on | on | n/a |
| Role filter | E:3201 | `firewall.skip_message_roles` | [] | [] | n/a |
| Recall-echo filter | E:3203 | `firewall.skip_injected_recall` | False | False | n/a |
| Secrets/PII | E:3446 | `firewall.redact_secrets`, `.pii` | off | off | n/a |
| Firewall trust/anomaly/instruction | core/firewall.py:259 | `firewall.signals.*`, `.enabled` | on | on | Silent quarantine of benign duplicates/outliers; not visible in ingest log |
| Dedup | store.py:174 | `semantic.policies.dedup` | n/a for episodic | not applied | Duplicates stored |
| Valid_from / recorded_at | E:1417, records.py:200-236 | - | now | session stamp | n/a |
| Group / session ids | E:3255-3258 | - | - | session_N | Replay ignores them (uses 30-min gaps) |
| Event log append | sql_base.py:129 | `event_log.mode/compress` | full/False | same | n/a |
| Record projection | storage/projector.py:90 | - | - | on | n/a |
| Vector projection | vector/projector.py:25 | `embedding.*`, `vector.*` | - | Qwen3-0.6B bf16 | Whole-turn single vector, bare text |
| Lexical projection | lexical/projector.py:28, tantivy.py:294 | `read.hybrid`, `.lexical_*` | on | on, no dates, no stemming | Date words and inflections not searchable |
| Graph / entities | associative/projector.py:57 | `memories.associative.*` | off | off | No entity-centric or multi-hop recall at all |
| Fetch size | E:3906 | `LEXICAL_FETCH_MULTIPLIER` const | 3x | 30 | n/a |
| Vector leg | E:3729 | - | on | on | n/a |
| BM25 leg | E:3763, tantivy.py:285 | as above | on | on | Stop words in OR query |
| Temporal leg | E:2039, temporal_query.py:171 | `read.temporal_*` | off | on, rarely fires | Needs absolute date in question |
| Fusion | E:3978, lexical/base.py:80 | `read.rrf_k`, `.fusion`, `.leg_weights*` | rrf, k=60, unweighted | same | Equal weights for a noisy BM25 and the dense leg |
| Fused cut | E:3999 | `read.candidate_pool` | 1 | 1 | Hard cap of 10 candidates before rerank |
| Gates | E:3786 | various | - | status/quarantine | Held rows eat slots; widening up to 64x |
| Candidate cut | E:4080 | `read.candidate_pool` | - | 10 | Same cap |
| Rerank | E:4095-4148 | `read.rerank*` | off | roff off / qwen3-4B 4bit / jina | Reranks 10 only; no dates/neighbours |
| Score | scoring.py:56 | `read.scoring.*` | blend | relevance-only | n/a |
| Abstain | assembly.py:112 | `read.assembly.theta_abstain` | .25 | .25 | Never fires after rerank (best=1.0) |
| Floor | assembly.py:121 | `read.assembly.relative_floor` | 0 | .3 | Drops low reranker-normalised hits |
| MMR (assembly) | assembly.py:207 | `read.assembly.mmr_lambda` | .7 | .7 | Only reorders under a non-binding budget |
| Relative dates | E:6489, temporal_resolve.py | `read.resolve_relative_dates`, `relative_dates_anchored`, `relative_week` | off | on, calendar week | LoCoMo-style "week before" mismatch |
| Replay window | E:5145-5241 | none (`replay_window` arg) | 2 | 2 | See 2.10 |
| Render | E:4720 | `read.render`, `read.gap_markers` | plain | plain | No hit marker; harness adds the date |
| Harness date prefix | H:536-540 | `dated` ctor arg | True | True | Day only |
| Tail truncation | runner.py:793 | `--budget` | - | 4096 | Drops latest turns first |
| Forensics | E:4017-4024, 4105-4107, 4123-4127, 4185; H:449 | `MEMSPINE_FORENSICS_DIR` | off | on if env set | `fused` is already cut to 10; `final` is before floor/abstain/MMR; windows not recorded except as `context_records` |

---------------------------------------------------------------------------------------------------

## 4. Config surface not yet used by our arm (recall / temporal / multi-hop relevant)

All defaults are from `SCH` / `CONST`; "needs" lists prerequisites. Everything below is OFF in `qs-eq06-roff`.

| Key | Default | One-line description | Needs |
|---|---|---|---|
| `read.candidate_pool` (1..10) | 1 | Fetch `top_k x pool` candidates (rerank pool and assembly pool); the only way to widen beyond 10 | - |
| `read.rerank_keep` | None | After rerank with pool>1, keep best N | reranker, pool>1 |
| `read.rerank_context` (0..3) | 0 | Reranker sees +-N session neighbour turns of each candidate | reranker |
| `read.rerank_date_prefix` | False | Prefix reranker docs with `[Date: ...]` | reranker |
| `read.rerank_blend` / `rerank_gate` | None | Blend rerank with retrieval score / keep retrieval order if reranker unsure | reranker |
| `read.rerank_balanced` | False | Reranker shortlist takes each leg's best in turn | reranker |
| `read.rerank_instruction` | None | Task instruction for Qwen3 reranker | `rerank: qwen3` |
| `read.fusion: minmax` | rrf | Min-max score fusion instead of RRF | - |
| `read.rrf_k` | 60 | RRF constant (Graphiti uses 1) | - |
| `read.leg_weights`, `leg_weights_by_shape`, `short_query_lexical_weight` | {} / {} / None | Per-leg / per-question-shape fusion weights | - |
| `read.leg_min_scores` | {} | Drop weak vector/BM25 hits before fusion | - |
| `read.lexical_analyzer: english` | default | Stemming + stop words for BM25 | - (own index) |
| `read.lexical_dates` | False | Index date words ("2023-05-07 7 May 2023 Sunday") with each turn | - (own index) |
| `read.core_terms_leg` | False | Extra BM25 leg on the question minus interrogatives | hybrid |
| `read.temporal_relative`, `temporal_infer_year`, `temporal_soft`, `temporal_leg_mentions`, `temporal_rank: overlap`, `temporal_leg_event_dates` | False/False/False/False/midpoint/False | Make the temporal leg fire on relative phrases / yearless dates, widen softly, match dates mentioned inside turns, rank in-span turns by overlap, use mined `happened:` dates | `temporal_leg` (on) |
| `read.relative_dates_anchored`, `read.relative_week: preceding_7_days` | False / calendar | LoCoMo-style "the week before <d>" resolution of relative phrases | `resolve_relative_dates` (on) |
| `read.cohesion_leg` | False | Leg of turns within 5 min of top-3 hits (same-session proxy) | - |
| `read.entity_expand_leg` | False | Leg of turns naming the proper nouns/years of the top hits | - |
| `read.entity_leg` | False | Leg of turns naming the question's proper nouns/years | - |
| `read.metadata_leg`, `subject_leg`, `sentence_leg`, `role_aware`, `recency_leg`, `view_tag_leg` | False | Rule legs (entity field, speaker tag, best-sentence overlap, assistant recs, recency, view tags) | `subject_leg` needs `episodic.policies.subject_tagging`; `view_tag_leg` needs mining |
| `read.maxsim_leg` | False | Best-sentence cosine leg inside multi-sentence turns | - |
| `read.speaker_probe`, `statement_probe`, `multi_intent_split`, `prf_expansion`, `second_round`, `cluster_expand` | False | Extra search probes / pseudo-relevance feedback / second-round / cluster neighbourhood legs | `second_round` needs `evidence_signal` + `evidence_weak_below` |
| `read.session_cap` | None | At most N hits per session from a 4x wider search (spread evidence over sessions) | - |
| `read.session_digest` | False | Header with the 2 sentences per session most like the question | - |
| `read.recent_exchanges` | 0 | Header with the last N turns | - |
| `read.replay_topic_segments` | False | Replay windows stay within the hit's topic segment | - |
| `read.evidence_first`, `read.section_captions` | False | Put best window first / caption the retrieved section | - |
| `read.reply_links` | False | Replay shows the message a reply answers | `write(reply_to=)` |
| `read.aggregate_in_replay` + `aggregate_top_k` | False / None | Widen list/count questions to `aggregate_top_k` in replay | - |
| `read.order_by_time_for_ordering` | on | (already on) | - |
| `read.gist_after`, `focused_excerpt`, `concentration_filter`, `type_quotas`, `facts_to_sources`, `raw_turn_floor` | off | Shrink/shape hits so more distinct evidence fits; keep derived records from displacing raw turns | `raw_turn_floor`, `facts_to_sources` matter once derived records exist |
| `read.mmr_lambda` | None | Embedding-space MMR on final hits | - |
| `read.planner: decision|llm`, `planner_version: v2|v3`, `completeness_check`, `compose_replay`, `compose_rewrites` | rules / v1 / False | LLM/GLiNER2 routing of aggregate questions to compose with subqueries and persons/time legs | LLM role or `decision.provider: gliner2` |
| `read.default_mode` / `Engine.read(mode="auto")` | replay | `auto` = full context if it fits (LoCoMo conversations are ~20k+ tokens, so rarely) else compose for aggregation questions else replay | - |
| `Engine.read(replay_window=)` (arg, not config) | 2 | Window radius of the neighbour expansion | code/call change |
| `read.cards: header` (+ `cards_*`), `profile_header*`, `profile_slots_header`, `novelty_exclusions`, `count_timeline`, `span_line`, `topic_timelines`, `standing_instructions` | off | Read-time header blocks of mined facts / profile / list cards / dated occurrences / computed durations | `memories.episodic.policies.consolidation.mine_facts` (+ `miner: rules` needs no LLM) and a sleep run |
| `memories.episodic.policies.consolidation.*` (`mine_facts`, `miner: rules`, `mine_event_dates`, `mine_multiview`, `list_cards`, `anticipate`, `reflect_profile`, `session_summary.incremental`, `predict_calibrate`) | all off | Sleep-time derived memory (atomic facts with event dates, multi-view tags, list cards, cues, profile, session summaries) | run `Engine.sleep()` (`--memspine-build-sleep`); several need an LLM role |
| `memories.episodic.policies.subject_tagging` | False | `speaker:<Name>` tag on each turn | needed by `read.subject_leg` |
| `memories.associative.enabled` + `.policies.entity_nodes` (`{turn_mentions: true}`) | off | Entity nodes + `mentions` edges, graph store (Ladybug) | enables all graph legs |
| `read.graph_leg`, `graph_depth` 2, `graph_leg_k` 10, `graph_node_search`, `graph_rerank: distance|ppr`, `graph_communities`, `causal_walk`, `cards_include_edges`, `entity_summaries` | off | Graph legs, graph rerank, edge-facts block, entity summaries | associative + entity_nodes (`graph.entity_embeddings` for node search) |
| `memories.semantic.policies.entity_extraction: gliner|llm`, `extract_graph`, `write_pipeline: graph` | off | Entity/edge extraction into semantic facts | `[ner]` extra / LLM role; semantic writes (not turn writes) |
| `decision.provider: gliner2` | off | GLiNER2 planner and entity hook | `[ner]` extra |
| `vector.quantization`, `vector.namespace_index`, `vector.isolation` | auto/False/shared | Index/isolation tuning, no recall effect except quantised ANN | - |
| `read.static_prefilter`, `read.static_embedding_prefilter` | False | Cheap pre-rerank gates | - |
| `read.record_access` | False | RETRIEVE events (changes recency/utility); keep off for independence | - |
| `firewall.signals.*`, `firewall.enabled: false` | on | Turning the firewall off removes any silent quarantine of benign turns (ablation only) | - |
| `read.assembly.{theta_abstain, relative_floor, dedupe_jaccard, latest_slots}` | .25 / .3 (arm) / 1.0 / 0 | Abstain/floor/dedupe/guaranteed-latest slots | - |

---------------------------------------------------------------------------------------------------

## 5. Stage vocabulary (where a gold memory can be lost)

Use these names in analyses; the forensics file (`MEMSPINE_FORENSICS_DIR/forensics.jsonl`, one row per question; `ingest.jsonl` per turn) carries the matching columns.

| Stage | Meaning | Evidence column / how to test | Typical causes |
|---|---|---|---|
| **ingest** | Gold turn never became a retrievable record, or was stored wrongly | `ingest.jsonl`: `written`, `text_identical`, `valid_from`, `group_id`; quarantine NOT logged (check `memory_records.quarantined` in the tempdir DB if kept) | firewall quarantine (MINJA prefix / embedding outlier), unparsable timestamp, redaction, batch failure, alignment error in `_align` |
| **recall (legs)** | Gold absent from every leg's fetch window (30) | `vector`, `lexical`, `extra_legs` ranks | weak embedder/instruction, BM25 stop-word noise, paraphrase, multi-hop (needs info from 2+ turns), no entity/graph leg, no date words in BM25 |
| **fusion** | Gold in a leg (<=30) but outside fused top-10 | `fused` (already cut to 10) vs `vector`/`lexical` ranks | RRF rank dilution, equal leg weights, `candidate_pool=1` |
| **gate** | Gold in fused but removed before the cut | present in `fused`, absent in `pool` | quarantined/archived/superseded status, scope filters |
| **rerank** | Gold in `pool` but reordered below the keep point or scored low | `pool` vs `rerank_scores`; `final` rank | reranker sees no date/neighbours, wrong instruction, min-max effect, `rerank_keep` (not set) |
| **assembly (abstain/floor/MMR)** | Gold in `final` but dropped before expansion | `final` vs `context_records` (turns missing although in `final`) | `relative_floor` on min-max reranker scores, abstain threshold (non-rerank), budget (rare) |
| **expansion** | Gold is a neighbour of a hit: it is in context only if within +-2 positions of a surviving hit in the derived session | `context_records` contains gold though not in `final` | window radius 2 fixed; derived-session boundaries (30-min gaps); windows skipped by budget |
| **render / harness truncation** | Gold in the engine context but cut or garbled for the reader | compare `context_records`, harness `RetrievedContext.text`, runner `context_truncated` | tail truncation at 4096 (latest turns lost first), date prefix tokens, `[= date]` annotation errors |
| **reader** | Gold fully present in context but answer wrong | context + answer | reader capability, context ordering (chronological, hits unmarked), date arithmetic (dates present only as `[YYYY-MM-DD]` per line) |

Rule of thumb: "gold in `final`" = retrieval success; "gold in `context_records`" = engine success (includes the expansion); anything later is harness or reader.

---------------------------------------------------------------------------------------------------

## 6. Gap hypotheses noticed while mapping (ordered by expected impact)

1. **Hard 10-candidate funnel.** `candidate_pool=1` means vector (30) + BM25 (30) are fused and cut to 10 (`E:3999`, `E:4080`); the rerank pool is exactly those 10 (`E:4106`). The reranker cannot recover gold at fusion rank 11-30, and `rerank_keep` is inert. Test: set `read.candidate_pool` 3-5 (+ `rerank_keep: 10`) and compare the "gold in `final`" rate.
2. **Relative floor on min-max-normalised reranker scores.** After rerank the best candidate is always 1.0 and the worst 0.0 (`E:931`), so `relative_floor 0.3` plus no abstention (`assembly.py:112,174`) can discard most of the 10 hits before the window expansion; reranker arms may therefore build windows around very few hits. Test: compare `final` count with distinct hit-centred windows in `context_records`; try `relative_floor: 0.0` for the rerank arms or `rerank_blend`.
3. **Neighbour window is fixed at +-2 and cannot be configured**, and it runs on 30-minute time-gap "sessions" rather than the dataset's `session_id` (`E:4874`, `E:5154`, `sessions.py:44`). Multi-hop and temporal questions whose evidence is >2 turns from any hit, or in a different session, get nothing; there is no cross-session, entity-based or temporal expansion in the arm.
4. **No graph, entity, or derived-memory layer in the arm.** Associative memory, entity nodes, GLiNER, mined facts/cards, session summaries and sleep are all off, and turn writes bypass the semantic door entirely (episodic only). Multi-hop questions that need two sessions' facts joined have only embedding+BM25 to rely on. `consolidation.miner: rules` and `entity_nodes {turn_mentions: true}` need no LLM.
5. **Temporal handling is thin.** Dates are not in the indexed text (`lexical_dates` off, embeddings bare), the temporal leg fires only on absolute dates in the question (`temporal_relative/infer_year/leg_mentions` off), relative phrases in turns resolve to the previous calendar week rather than LoCoMo's "week before" (`relative_dates_anchored` off), the rerankers see no dates (`rerank_date_prefix` off), and the reader gets only a day-level `[YYYY-MM-DD]` prefix per line.
6. **Silent firewall quarantine.** Default-on anomaly signals (96-char prefix repeat, embedding outlier) can quarantine benign LoCoMo turns; the adapter's ingest log reports `written: true` and omits `quarantined`/`trust` (`H:404-447`), so a never-retrievable gold turn would look like a recall failure. Cheap to rule out per item.
7. **BM25 query quality.** Unfiltered OR over every token including stop words and the speaker name present in nearly every turn, no stemming (`tantivy.py:76-96`, `:310-327`); `core_terms_leg`, `lexical_analyzer: english` are available.
8. **Forensics blind spots.** `fused` is already cut to 10 (cannot show rank 11-30 of the fusion); `final` precedes floor/abstain/MMR and replay; the quarantine flag is not logged. These limit what stage attribution can prove.
