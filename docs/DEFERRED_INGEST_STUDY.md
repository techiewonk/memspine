# Deferred ingest enrichment (gap I71): study

Status: study only, 2026-10-10. Nothing was implemented, run or changed in the engine. I71 is on hold
(user). Sources: the code at branch `feat/local-qwen-stack` (file:line below), the existing run traces
in `evals/runs/*--memspine/trace.jsonl`, and the engine clones used by `docs/FRAMEWORK_TOOLCALL_SURVEY.md`.
Where a number is an estimate and not a measurement it says so.

## 0. Summary (read this first)

1. The I71 register row says enrichment "runs inline". That is only partly true. In MemSpine the
   expensive LLM enrichment (fact mining, list cards, anticipation cues, profile reflection, graph
   extraction, entity summaries) is **already background**: it lives in the sleep cycle
   (`workers/schedule.py:30`, `engine.sleep` `engine.py:11567`), runs per closed session, is idempotent,
   and is driven by an explicit `sleep()` or the opt-in `SleepScheduler`. With the default config a write
   makes **zero LLM calls** (no `deposit` row in any of the 118 existing eval traces shows `model_calls` above 0).
2. What is inline today: the firewall, redaction, tagging, embedding, vector and lexical projection, link
   evolution, and, only when switched on, a few optional LLM or encoder steps (semantic entity extraction,
   the graph write pipeline, the decider modes of perspective and sensitivity).
3. Measured hot-path cost (existing traces, GPU embedder, all default write features): about 55-60 ms per
   turn when batched 20 turns per call, and about 150-210 ms per turn when written one at a time.
   There is no per-step timing in the repo, so the split inside that number is an estimate.
4. So the real value of deferral is narrower than the register implied: (a) take the optional inline LLM
   steps off the hot path, (b) make mined facts, cards and cues available seconds after a turn instead of
   at the next sleep (today they need a closed session: at least 3 turns and a 30-minute gap), (c) batch
   LLM calls across turns, (d) retries, back-pressure and cost control. For the default config and for the benchmark path it buys nothing on latency.
5. Two findings that matter whether or not I71 goes ahead: no per-step write timers exist, and a mined
   fact can be deposited after its source turn was hard-forgotten (section 4.3).
6. Recommendation: keep I71 on hold, do the two small prerequisites (timers, parent-liveness recheck),
   then decide with data. If it goes ahead, build it as a namespace-scoped, idle-debounced "micro-sleep"
   that reuses the existing sleep stages, not as a new per-record job system (section 6).

## 1. What deferred ingest enrichment is

Hot path: what the caller waits for. Store the raw turn exactly as given (after the security checks) and
build the minimum index so it can be found by the very next search. Enrichment: everything that makes the
memory richer or better organised but that no immediate read depends on. Run it later, from a queue.

```
                      TODAY (sync)                                 DEFERRED
 caller --write--> [firewall][redact][tags][embed+index]    caller --write--> [firewall][redact][tags]
                   [dedup/entity LLM?][link evolution]                         [embed+vector+lexical index]
                   [graph write pipeline LLM?]                                 record_id returned  <-- caller resumes
                   record_id returned  <-- caller resumes                              |
                                                                                       v  enqueue(ns, job, record_id)
 later: sleep() / scheduler tick                                          per-namespace ordered queue
   consolidate -> mine_facts -> cues -> profile                              worker: link evolution, LLM mining,
   -> extract_graph -> summaries -> decay -> compress                        cards, cues, profile, graph (batched)
                                                                             flush(ns) = wait until queue empty
```

The wording in the register ("queue cues/fact mining/entity linking/summaries") describes the right
target, but in MemSpine those already happen at sleep time. The change would be **when** and **how
promptly** (idle-debounced, per namespace, after the turn) and **for which optional inline steps**.

## 2. Inventory of every write-path step

Cost column: C = CPU, G = GPU or local model, L = LLM call (Ollama or API), S = SQLite/disk. "Estimate"
means reasoned from the code, not measured. The measured aggregates are in 2.2.

### 2.1 Table

"Needed by next read" asks whether a search or assemble run immediately after the write gives a wrong
answer without this step. "Defer?" is the verdict: **no** (keep inline), **yes**, **opt** (yes, with a
stated cost).

| # | Step | What it does | Where | Today | Cost per turn | Next read needs it? | Defer? |
|---|---|---|---|---|---|---|---|
| 1 | Namespace validation, `reply_to` check, caller tags | refuses bad ns, foreign `reply_to`, forged `shared` type | `engine.py:1496-1512` | inline | C, one S read if `reply_to` | yes (rejects) | no |
| 2 | Speaker tag (`subject_tagging`) | `spk:` tag from `Name: text` | `engine.py:1513-1519` | inline | C, regex (est. <1 ms) | yes, filters use it | no |
| 3 | Perspective tags (I39), rules mode | speaker, addressee, subject, stance, ack tags; stateful per ns (`known`, `last`, `acked`) | `engine.py:1520-1525`, `_annotate_perspective` `:2599`, `core/perspective.py` | inline, off unless `policy.perspective` set | C (est. <1 ms) | yes if perspective read legs are on | no (order dependent) |
| 4 | Perspective tags, `decider` mode | typed stance questions and `acknowledges` through the decider port (opendecider-nano, ~395M encoder) | `engine.py:2634-2663`; `services/decision/opendecider_nano.py` | inline, only if `mode: decider` | G, one or two encoder passes (est. tens of ms GPU, hundreds CPU; not measured) | no, it only refines rule tags | opt (stale refinement) |
| 5 | Implicit-read parents | links the write to what the session just read (B0 ledger) | `engine.py:1526-1529`, `_consume_reads` `:1688` | inline | C | yes (trust cap) | no |
| 6 | Parent trust cap | MTI-D: trust never above least-trusted parent | `engine.py:1552-1566`, `:3591` | inline | S, one read per parent | yes | no |
| 7 | Per-namespace write lock | one writer per ns | `engine.py:1569` | inline | wait time only | n/a | no |
| 8 | Redaction and PII tag | masks secrets, tags PII, sensitive topics, raises tier | `_redact_fields` `engine.py:4067`; `core/redaction.py` | inline | C, regex (est. <1 ms) | yes (must not store raw secrets) | **no** |
| 9 | Governance labels (I48, I52, I53) | sensitivity grade tag, participant and visibility tags, `src:inferred` | `_govern_write` `engine.py:4159`; `_grade_sensitivity` `:4139` | inline, all off by default | C (lexicon); `decider` mode adds G per turn | yes: these are **access-control labels** (a late `visibility:private` is a leak window) | **no** |
| 10 | Firewall assessment | embeds the content, nearest-neighbour similarities (embedding outlier), last 50 contents (MINJA prefixes), instruction flag, size and protected-key checks, trust matrix, hearsay cap, principal reputation | `_assess_write` `engine.py:9576`, `_screen_write` `:4003` | inline | G one embed (cached later), S neighbour query + `recent_contents(50)` | yes: it decides quarantine | **never** |
| 11 | Quarantine write | stores held content inert | `engine.py:3957-3985` | inline | S | yes | no |
| 12 | Semantic dedup annotate | minhash sketch, LSH candidates, cosine confirm | `memories/semantic/store.py:176, 244` | inline, semantic writes only | C (thread), one embed batch if candidates | yes (merge vs add) | no |
| 13 | Semantic entity extraction | LLM keys an unkeyed fact as (entity, attribute) | `memories/semantic/store.py:186-205`; built `engine.py:12776` | inline, only `entity_extraction: llm` (default off) | **L**, one call per unkeyed semantic write (cached by content, E3) | yes: without a key the fact skips the conflict ladder | opt (see 4.2) |
| 14 | Conflict ladder | dedup, supersede, merge, coexist, contest; optional LLM judge on escalation | `memories/semantic/store.py:207-218, 327`; `core/policies/conflict.py` | inline, semantic writes | S, C; **L** only on judge escalation | yes | no |
| 15 | Graph write pipeline (C3) | relationship edges extracted at write time, each written through the ladder | `memories/semantic/write_pipeline.py`; `store.py:139`; `engine.py:12751` | inline, only `write_pipeline: graph` (default `single`) | **L**, one to `max_rounds` calls per semantic write, plus one ladder write per edge | no (edges are an index over the fact) | **yes** (best candidate) |
| 16 | Event append | WRITE event into the log | `_append_and_project` `engine.py:11725`; `_write_screened` `:4272` | inline | S | yes | no |
| 17 | Record projector | SQL read model | `services/storage/projector.py:90` | inline | S | yes | no |
| 18 | Vector projector | embeds the content (cache hit after step 10) and upserts | `services/vector/projector.py:18` | inline | G embed (cached), vector upsert | yes: otherwise not searchable | no |
| 19 | Lexical projector | BM25 index; commit held to end of a batch | `services/lexical/projector.py:19` | inline, hybrid on | C, S; one commit per batch | yes when `read.hybrid` | no |
| 20 | Corroboration | scans quarantined records; a trusted independent write promotes a held one | `_corroborate` `engine.py:9654` | inline | S, one list of quarantined | no for the written record; yes for the promoted one | opt (security adjacent, keep inline) |
| 21 | Link evolution (A-MEM) | embeds (cached), vector top-k, proposes `related` LINK events to similar records within budget | `_evolve_links` `engine.py:11427`; `memories/associative/evolution.py:39` | inline, only with associative memory; best effort (failure logged, never raised) | G cached embed, one vector query, up to N link events | no (only graph expansion reads links) | **yes** |
| 22 | Reply link | `reply_to` LINK beside the tag | `_link_reply` `engine.py:1580` | inline | S | no (tag already links) | yes |
| 23 | Session reopen | a write to a passive session reopens it | `_reopen_session` `engine.py:1599` | inline, lifecycle on | one `list_records` per ns after start, then C | yes (default reads hide passive sessions) | no |
| 24 | Turn-level detectors | recommendation, forget-request, correction tags; correction supersedes a fact | `engine.py:3792-3814, 3833`; `_apply_correction` `:10878` | inline | C regex; correction path S scan of semantic records | correction: yes | no |
| 25 | Batch prefetch and prewarm | one batched embed and one batched neighbour query per `write_messages` call; one lexical commit and one offset checkpoint per call | `engine.py:3664-3684, 3686, 3896, 11822` | inline | G amortised | n/a (this is the batching, not deferral) | n/a |
| 26 | Resource ingest | extract text, chunk, firewall each chunk, WRITE per chunk | `Engine.ingest` `engine.py:10066`; `memories/resource/store.py:54` | inline, holds the ns lock for the whole document | C (extract, chunk), G one embed per chunk (not batched), S | yes per chunk | opt (see 3c) |
| 27 | Consolidate | one summary per closed session (extractive, or LLM if a `summarize` role is bound) | `workers/pipelines.py:364` | **background (sleep)** | C; L only with a summariser | no | already deferred |
| 28 | `mine_facts` | one LLM call per consolidated session (per topic segment with `mine_by_topic`), optional batched dating call, then one write-door deposit per fact | `pipelines.py:3173`, `_run_session_stage` `:3022`, deposit `engine.py:12412` | **background (sleep)** | **L**, at least 1 per session | no | already deferred |
| 29 | List cards | per-person class labelling call and card deposit after mining | `workers/list_cards.py:261`; deposit `engine.py:12532` | background (sleep) | **L**, about 1 per person per cycle | no | already deferred |
| 30 | Anticipate (cues) | LLM-predicted questions stored as cue records | `pipelines.py:3263`; `engine.py:12317` | background (sleep) | **L**, 1 per session | no | already deferred |
| 31 | Reflect profile | LLM profile reflection per session | `pipelines.py:3301`; `engine.py:12286` | background (sleep) | **L**, 1 per session | no | already deferred |
| 32 | Extract graph (+ rule edges) | LLM edge extraction per session or record, entity resolution, retract contradicted edges | `pipelines.py:1887, 2277` | background (sleep) | **L**, 1+ per session | no | already deferred |
| 33 | Entity summaries | batched entity summary calls | `pipelines.py:2499` | background (sleep) | **L** | no | already deferred |
| 34 | Reorganize (communities) | graph partition and summary parents | `pipelines.py:1082` | background (sleep) | C, optional L | no | already deferred |
| 35 | Decay, compress, prune, session lifecycle, watches | maintenance | `pipelines.py:774, 827, 336, 727, 883` | background (sleep) | C, S | no | already deferred |

### 2.2 What the existing run logs say about cost

Source: `evals/runs/*--memspine/trace.jsonl`, `deposit` rows, field `latency_ms` (time the eval harness
spent in `write_messages` for that flush) and `delta_t.model_calls`. Script: reads only, no engine run.
Hardware per `evals/QWEN_STACK_RESULTS.md`: one RTX 5080, Qwen3-Embedding-0.6B bf16; some runs shared the
GPU with a second arm.

| Run family | Turns | Turns per `write_messages` | ms per turn (total / records) | Notes |
|---|---|---|---|---|
| a9, cd-*, f3, f6, f7 (GPU, batched) | 419 to 1,292 | about 20 | **52-60** | median flush 650-870 ms for 20 turns |
| f2-v2, f5-trel (GPU, batched) | 419 to 788 | about 20 | 84-94 | arms with extra features; the trace does not say which step costs the difference |
| qa-full-_test-*-cpu (CPU embedder, batched) | 419 | about 20 | 90-104 | median flush 1.5-1.9 s |
| qs-* grid (GPU, unbatched) | 5,882 per run | 1 | **150-210** | two arms shared the GPU, so conservative-high |

- `model_calls` is 0 in every deposit row of every trace that has deposits (118 files scanned): no LLM call is made at write time in any run so
  far. All LLM cost in the evals is at `build_sleep` (`memspine_system.py:580`) or at read.
- Batching (`--memspine-batch-turns 32`) already cuts per-turn write cost by about 3x (150-210 to 55-60 ms),
  consistent with the note in `QWEN_STACK_RESULTS.md` that batching halves wall clock. That is batching of
  embeddings, neighbour queries and commits (rows 10, 18, 19, 25), not deferral. The two are complementary.
- LLM reference points (`evals/runs/_logs/bench_llm.jsonl`, qwen3.5:9b on Ollama): about 2.7 s mean
  latency per call at 2k context and about 34 completion tokens, 0.38 request/s serial and about 1.1
  request/s at concurrency 4. Mining answers are longer than 34 tokens, so a per-session mining call is
  probably several seconds to tens of seconds (estimate). One inline LLM call per turn would therefore be
  roughly 50x the whole measured hot path.
- Missing: per-step timers. The split of the 55-60 ms between the embed, the neighbour query, the SQL
  writes, the lexical commit and the tag rules is unknown. Adding timers around steps 10, 18, 19, 21 is
  the cheapest next step and would settle several of the questions in section 7.

## 3. Where to use deferral, by scenario

### (a) Live chat or agent hot path

- With the default config the hot path is about 55-150 ms and LLM free; deferral would not change the
  user-visible latency. It matters when the deployment turns on the inline LLM or encoder options: rows 4,
  9 (decider), 13, 15. Those add one or more LLM calls (seconds each) per turn.
- The gain a chat user would see is **freshness of derived memory**: with a debounce of a few seconds after
  the last turn, mined facts and cues could exist before the next session, instead of at the next `sleep()`
  after the session has closed (>= 3 turns and a 30-minute gap, `docs/PIPELINE_TREE.md:237`).
- Within the live session the raw turns are always searchable (rows 16-19), and replay-style reads
  (`read_mode: replay`) work from raw turns, so the current conversation does not depend on mined facts.
- Verdict: worth it only if the deployment enables rows 13/15 or wants minutes-not-hours freshness. Defer
  rows 15 and 21; keep 1-12, 14, 16-19 inline.

### (b) MCP and tool writes - removed

Out of scope by user decision (2026-10-10): MCP / agent tool writes are not a deferred-ingest scenario. Tool
writes (I65 / I66) stay synchronous; this study makes no recommendation for them.

### (c) Bulk ingest (documents, histories)

- Documents (`ingest`, row 26): the whole document is written under one namespace lock, one embed per
  chunk, firewall per chunk. The ingest path has no LLM step, so deferral does not help the chunk write.
  Two real inefficiencies are independent of deferral: chunks are not batched through
  `_prewarm_embeddings` the way `write_messages` is, and the lock is held for the whole document. A
  deferral queue would not fix either; batching would.
- Histories (`write_messages`, `write_episode`): already batched (row 25). The enrichment for a
  100-session history is today one `sleep()` at the end. A queue would let enrichment overlap with ingest
  (GPU embed for turns while the LLM mines the previous session), which only helps if the LLM and embedder
  do not contend for the same GPU. On the single RTX 5080 they do contend (the grid notes show parallel
  arms slowing each other), so the gain is likely small on this machine and real on a deployment with a
  remote LLM.
- Batching LLM calls: mining already sends one whole session per call. A queue could pack several short
  sessions into one prompt (fewer calls, shared instructions), at the risk of cross-session attribution
  errors; that is a prompt-design question, not a queue question.
- Verdict: overlap and LLM batching are the benefit; measure on a remote-LLM setup before building.

### (d) Evals

- The benchmark path must produce identical records and scores with deferral on or off. Rules:
  1. default `write.mode: sync`, so every existing arm is byte-identical;
  2. under `deferred`, the harness calls `await engine.flush(namespace)` in the places it already drains
     state: `MemspineSystem.flush()` (`memspine_system.py:495`, called by the runner before every query and
     before `build`) and before `build_sleep`'s `engine.sleep()` (`:580-603`);
  3. `batch_turns` is orthogonal: it only changes how many turns go into one `write_messages` call
     (`memspine_system.py:468-480`); the deferred queue sits behind the engine API and sees the same calls;
  4. a test in the style of `evals/tests/test_batch_consistency.py` (batch 1 vs 32 must store the same
     records) should assert sync vs deferred+flush store the same records, tags and mined facts, on a fixed
     fixture with the LLM stubbed.
- Cost accounting: the harness reads `model_calls()` before and after a deposit to charge write cost
  (`memspine_system.py:_deposit`). Deferred LLM calls would land after the deposit and be mis-attributed or
  missed unless `flush()` returns the call delta and the harness adds it to the item ledger.
- Retrieval-only screens never call the LLM at write time, so deferral changes nothing there.
- Verdict: evals stay synchronous. Deferral is a production feature; it must not be a benchmark variable.

### (e) Multi-tenant servers

- Per-namespace sequential queues (Graphiti: one `asyncio.Queue` and one worker per `group_id`,
  `graphiti/mcp_server/src/services/queue_service.py:25-78`) fit the engine's own model: one writer per
  namespace already exists (`engine.py:1569`, `_namespace_lock` `:11862`). One tenant's slow LLM job then
  cannot delay another tenant's writes.
- Needed on top: a global concurrency cap on LLM calls across namespaces (otherwise 50 tenants each start a
  job), fair scheduling, per-namespace queue depth limits, and queue state included in `erase_namespace`.
- Today the opposite problem exists: one `sleep()` iterates all namespaces serially in one pass
  (`run_sleep_cycle`), so a large tenant's mining delays everyone's maintenance.
- Verdict: the strongest case for a queue is here, mainly for fairness and back-pressure rather than speed.

## 4. How it would enhance the engine, and the risks

### 4.1 Gains

| Gain | Applies when | Evidence |
|---|---|---|
| Lower write latency | optional inline LLM or decider steps are on (rows 4, 9, 13, 15) | LLM call about 2.7 s vs hot path about 0.06-0.2 s |
| Fresher derived memory | live agents, long sessions | today needs a closed session plus a sleep |
| LLM call batching | many short turns or sessions | mining is per session today (`pipelines.py:3173`); a queue can group |
| Retries with backoff | flaky local model or API | today a failed stage retries at the next cycle (`pipelines.py:3064`), minutes to hours later |
| Back-pressure and cost control | multi-tenant, tool agents | no limit on enrichment work exists; a depth cap and a daily call budget would add one |
| Fairness | multi-tenant | see 3(e) |

Honest limit: none of this lowers benchmark latency or changes benchmark scores.

### 4.2 Risks, with what the code already gives and what it does not

1. **Read-after-write staleness.** After a deferred write, the raw turn is searchable at once (rows 16-19)
   but derived records (mined facts, cards, cues, edges) are not. Today the same is true until the next
   `sleep()`, so deferral narrows the window rather than creating one. Real new exposure: row 13 (entity
   extraction). An unkeyed semantic write cannot enter the conflict ladder, so for a few seconds the old
   value of "lives_in" is still the active fact after the user said they moved. Mitigation: do not defer
   row 13 for tool or user "fact" writes that pass `entity`/`attribute` (keyed writes skip extraction,
   `store.py:186`); defer only unkeyed free text.
2. **Ordering and conflict-ladder races (supersede order).** Two jobs on the same (entity, attribute) key
   must apply in write order. One worker per namespace with a FIFO queue gives that, and the deposit takes
   the same namespace lock as every writer (`engine.py:12489`, `memories/semantic/store.py:126`). A fact
   mined from an older turn already carries `valid_from` of that turn (`pipelines.py:3218`), so a
   deferred deposit that lands after a newer inline correction should lose to it; this is exactly what
   happens with sleep-time mining today. I did not run a test for the supersede case; it should be the
   first test of the feature (inline correction at t2 between a turn at t1 and its deferred mining).
3. **Crash mid-queue.** An in-memory queue (Graphiti's design) loses jobs on a crash and swallows errors:
   its worker only logs and moves on (`queue_service.py:58-72`). MemSpine can do better with what it has:
   stages are idempotent, with a `stage_done` MARKER per session and a `members_fp` fingerprint in the
   event log (`stage_marker` `pipelines.py:2825`; `_run_session_stage` `:3022-3074`), and projections
   re-apply after a crash from the offsets. So the queue itself can be disposable if on start-up the
   engine recomputes "what is unprocessed" from the log (sessions with no `stage_done`). That gives
   at-least-once with no new storage. Memobase persists its buffer in a database table with `idle` status
   (`memobase/.../controllers/buffer.py:33-60`); the event log plays that role here.
4. **Firewall must stay inline.** Rows 8, 9, 10, 11, 20 are security or access-control steps. A deferred
   firewall means unscreened content is searchable, and a deferred visibility or sensitivity tag means a
   private turn is readable by the wrong viewer until the label lands. Rule: any step whose output a read
   uses to *exclude* a record stays inline. The enrichment output (derived facts) must still pass
   `_screen_derived` (`engine.py:3988`) when deposited, and it already does.
5. **Erase or forget while enrichment is pending.** What exists: `forget(hard=True)` cascades to every
   record derived through `source.parents` (`engine.py:7991-7994`, `_descendants` `:8049`), takes the namespace
   lock, redacts log payloads and purges caches (`_forget_many` `:8112`). Mining skips forgotten turns when
   it *starts* (`_live_members` `pipelines.py:2997`, active and unquarantined only). **Gap found:** the LLM
   call for a session runs before its deposits and outside the namespace lock, and `_deposit_mined_fact`
   re-reads its parents but does not stop when they are gone (`engine.py:12450`, `sources = [... if r is not
   None]`; `cap` becomes None and the write proceeds, `:12485-12489`). A hard-forget that lands between the
   LLM call and the deposit therefore leaves a derived fact that restates erased content, and the cascade
   already ran so nothing removes it later. Read from the code, not reproduced. This exists today in the
   sleep path and a queue would widen the window (jobs wait longer). Required for any deferral: (i) before
   each deposit, under the lock, drop the job if any parent is missing, deleted or quarantined; (ii)
   `forget` and `erase_*` cancel queued jobs that name the ids and discard in-flight results; (iii) jobs
   carry ids only, never text, so `redact_event_payloads` and cache purge already cover them.
6. **Observability.** Today the sleep stats dict is returned and degradation is logged
   (`engine.py:11574-11581`). A queue needs: depth and oldest-job age per namespace, jobs by status
   (queued, running, done, failed, cancelled), LLM calls and tokens per job type (reuse `model_usage()`,
   `engine.py:11521`), and a loud failure after `max_attempts`. Without these a stuck queue is invisible
   (the Graphiti worker would just log).
7. **Duplicate work.** Two triggers for the same stage (debounce worker and `SleepScheduler`) would both
   call the LLM. The `stage_done` marker prevents duplicate *output* but only after the first call
   finished; concurrent runs could both pay for the call. The queue and the scheduler should share one
   in-process lock per (namespace, stage).
8. **Memory pressure and shutdown.** The queue holds ids only (small). `Engine.stop()` must drain or
   persist-by-log and cancel the worker tasks (the scheduler already does this, `engine.py:1408-1410`).

## 5. Evidence from other engines

| System | Mechanism | Take-away for MemSpine | Source |
|---|---|---|---|
| Graphiti MCP | one in-memory `asyncio.Queue` plus one worker task per `group_id`, sequential; errors logged and dropped | per-namespace ordering is simple and enough; add durability and retries | `mcp_server/src/services/queue_service.py:25-78` |
| LangMem | `ReflectionExecutor.submit(payload, after_seconds, thread_id)`: a new submit for the same thread cancels the pending one (debounce), run later in a worker | debounce by session key, cancel and replace | `src/langmem/reflection.py:54, 254-340` |
| Memobase | buffer table `BufferZone` with status `idle`; flush when token count or idle time passes a threshold | durable queue with size and idle triggers; the batch is the unit of one LLM call | `src/server/api/memobase_server/controllers/buffer.py:33-60` |
| Mem0 | extraction inline in `add` (one LLM call); async API variants but no queue | the inline baseline MemSpine avoids | survey `:139` |
| Letta | sleeptime agents run on a frequency, sharing memory blocks | background by cadence, not by event; MemSpine's sleep cycle is the equivalent | survey `:137` |
| Hindsight | retain can be submitted `async=true`, returns an `operation_id` that can be listed | caller-visible operation handle is a good API | `hindsight-api-slim/.../api/http.py:1376-1385` |
| EverOS, MemOS, Cognee, Mastra | scheduler or `cognify` pipelines and observer agents with idle buffering | same split: hot path stores, scheduler enriches | survey rows `:141-146` |

Common pattern: raw store on the hot path; an ordered, per-user (or per-group) worker; batching by size or
idle time; an operation handle for progress. Only Memobase persists the queue.

## 6. Recommended minimal design (no new dependencies)

### 6.1 Principle

Do not build a second pipeline system. MemSpine already has idempotent, replayable, ordered stages with
done-markers (`workers/pipelines.py`). The queue is a **trigger and a scope**, not new logic: it runs the
existing stages for one namespace (and one session) soon after a write, instead of waiting for `sleep()`.

### 6.2 Inline versus queued

| Stays inline (security, correctness of the next read) | Goes to the queue |
|---|---|
| validation, caller tags (1), speaker tag (2) | `mine_facts` + list cards + dating (28, 29) |
| perspective rules tags (3), implicit parents (5), trust cap (6) | anticipate cues (30), reflect profile (31) |
| redaction, PII tag, governance labels incl. sensitivity and visibility (8, 9) | extract graph and rule edges (32), entity summaries (33) |
| firewall, quarantine, corroboration (10, 11, 20) | link evolution (21), reply link (22) |
| dedup, keyed conflict ladder (12, 14) | graph write pipeline C3 (15) when the user opts in |
| event append, SQL, vector, lexical projection (16-19) | `decider`-mode refinements (4), as a tag-update job |
| session reopen, correction detector (23, 24) | LLM entity extraction (13) only for unkeyed free text, opt-in |

Rows 4 and 13 need a way to amend a record after the fact. There is no "update tags" event used for this
(I48's note in `GAP_REGISTER.md` already lists "needs a tag-update event" as open). The `DECAY_TRANSITION`
`set` payload (`engine.py:11764`) is used for status changes and may be reusable; unverified. Until that
exists, keep rows 4 and 13 inline.

### 6.3 Components

- `EnrichmentQueue` (one small module under `workers/`, no imports of runners, in keeping with the
  anti-lock-in rule at `pipelines.py:1`): `dict[namespace -> asyncio.Queue]` and one worker task per
  active namespace, created lazily and cancelled when idle; a global `asyncio.Semaphore` for LLM
  concurrency.
- Job = `(namespace, stage, scope_key, attempt)`; **idempotency key** = the existing
  `(namespace, stage, session_key, members_fp)` that `stage_marker` already writes. A job whose key already
  has `stage_done` is skipped, which makes at-least-once safe.
- Debounce: `enqueue` for the same (namespace, session) replaces the pending job and restarts the timer
  (LangMem pattern). Burst of 20 turns gives one job.
- Scope: a `scope` argument on the session stages so a job processes one session instead of scanning every
  session (the stages already iterate a `SessionIndex`, `pipelines.py:2863`; the change is a filter).
  Closure rule: mining today needs a *consolidated* (closed) session; a deferred job would need a rule for
  an *open* session (see question 7.2).
- Recovery: on `start()`, enqueue every namespace that has sessions without `stage_done` for an enabled
  stage. No queue file is needed; the log is the queue.
- Cancel: `forget`, `erase_subject`, `erase_namespace` call `queue.cancel(ns, ids)` (drop queued, flag
  in-flight so its result is discarded) while holding the namespace lock they already take
  (`engine.py:7991, 8018, 8040`); the deposit re-checks parent liveness under the lock (4.2 item 5).
- Barrier: `await engine.flush(namespace=None, timeout=None) -> dict` waits until the queue (one namespace
  or all) is empty and returns per-stage stats and LLM call deltas; `engine.pending(namespace) -> int`.
  `sleep()` keeps working unchanged and shares the per-(namespace, stage) lock.
- Observability: log events `enrich.queued|started|done|failed|cancelled`, `describe()` gains a
  `write_queue` block (mode, depth, oldest age, failed), failures after `max_attempts` stay as a MARKER
  `stage_failed` so they show in the log and in `sleep` stats.

### 6.4 Config keys (all under `write`, default = today's behaviour)

```yaml
write:
  mode: sync              # sync | deferred.  sync = exactly today.
  defer:
    stages: [mine_facts, anticipate, reflect_profile, extract_graph, link_evolution]
    debounce_seconds: 5   # per (namespace, session); 0 = run as soon as the worker is free
    max_queue_per_namespace: 1000
    when_full: run_inline # run_inline | block | reject
    llm_concurrency: 2    # global cap across namespaces
    max_attempts: 3
    backoff_seconds: [5, 30, 120]
    daily_llm_calls: null # per-namespace budget; null = unlimited
    flush_on_stop: true
```

Evals never set `defer`. MCP / tool writes are out of scope for I71 (user decision 2026-10-10).

## 7. Open questions for you

Each has a recommended default; none block the others unless noted.

1. **Is I71 worth doing now?**
   - Do it: gains freshness and multi-tenant fairness; costs an M-size module and new tests.
   - Hold (recommended): the default config has no inline LLM and the benchmark path gets nothing; do the
     two small prerequisites first (7.8, 7.9) and revisit when MCP tools exist and you can see real
     traffic.
2. **What counts as "ready" for mining an open session?** Today: closed session (>= 3 turns, 30-minute
   gap). Options: (a) keep that rule and only run it sooner/automatically after the gap (small change,
   fixes "needs a manual sleep" but not freshness); (b) mine a rolling window of the last N turns after the
   debounce (fresh within seconds, but the same facts may be re-mined when the session grows, so dedup and
   the ladder do more work and the `members_fp` marker no longer prevents repeats). Recommend (a) first.
3. **Should perspective tagging stay inline?** Rules mode is regex plus per-namespace state, est. well
   under 1 ms, and its order dependence (`last` speaker, `acked`) argues for inline. `decider` mode adds an
   encoder pass or two per turn (est. tens of ms on GPU, hundreds on CPU; not measured). Inline: tags are
   right for the next read, +X ms per turn where X is the unmeasured decider cost. Deferred: rule tags now,
   decider refinement later, so for Y seconds a turn carries only the rule stance (read legs that filter on
   `is_fact`-style tags could miss or mis-rank it) and a tag-update event must exist first. Recommend:
   rules inline, decider inline until timers show it is a real share of the 55-60 ms.
4. **Sensitivity, visibility, participant tags: inline always?** They gate who may read a record, so a
   late label is a leak window. Recommend inline always, even in `decider` mode (a failed decider already
   falls back to the lexicon, `engine.py:4155`). Alternative: defer the decider upgrade only, record starts
   at the lexicon grade, upgrades can only raise it (that is already the rule, `engine.py:4139-4146`).
5. **Semantic entity extraction and graph write pipeline (rows 13, 15).**
   - Keep inline (today): correct ladder behaviour immediately; a write costs one or more LLM calls.
   - Defer unkeyed free text: fast write; the fact is "unkeyed" for seconds, cannot supersede, and a
     newer user statement and a pending older job could finish in the wrong order unless the job re-reads
     the key at run time.
   - Recommend: callers that know the key pass `entity`/`attribute` (already supported) and are never
     deferred; defer row 15 (edges), keep row 13 inline until the ordering test passes.
6. **What happens when the queue is full?** `run_inline` (the write pays the cost, nothing is lost, latency
   spikes), `block` (back-pressure to the caller, can stall an agent), `reject` (error to the caller, safest
   for servers, bad for chat). Recommend `run_inline` for library use and `reject` configurable for the
   REST/MCP server.
7. **Durability.** (a) Log-derived recovery only (recommended: no new storage, at-least-once, idempotent);
   (b) an explicit `enrich_jobs` table (exact attempt counts and visibility of pending work, one more
   projection to keep consistent and to erase). Choose (a) unless you need per-job audit trails.
8. **Hard-forget semantics for in-flight jobs.** Wait for the running LLM call (erase takes seconds to
   tens of seconds) or cancel and discard its result (erase is immediate, one wasted call)? Recommend
   discard. Independent of I71, do you want the parent-liveness recheck in `_deposit_mined_fact` fixed now?
   It closes a real erase gap in the existing sleep path (section 4.2 item 5).
9. **Per-step write timers.** Approve adding timers (log-only, `write.step_timings`, default off) around
   firewall, embed, vector, lexical, evolution and tag steps. It is the data that decides 7.1 and 7.3, costs
   little, and changes no behaviour.
10. **MCP default.** Removed: MCP / tool writes are out of I71's scope (user decision 2026-10-10).
11. **Debounce window.** 0 s (as soon as free), 5 s (a burst of turns becomes one job), or session-idle
    (30 minutes, equals today's closure rule). Shorter is fresher and costs more LLM calls on a chatty
    session; recommend 5 s only if 7.2(b) is chosen, otherwise idle.
12. **Budget.** Do you want a per-namespace daily LLM call cap for background enrichment from the first
    version (protects an API bill from a looping agent), or only a depth cap?

## 8. Suggested order if you decide to go ahead

1. Step timers (7.9) and the parent-liveness recheck (7.8): small, useful alone.
2. `flush()` and `pending()` on the engine as thin wrappers around `sleep()` stages with a namespace and
   session scope. This alone gives evals the barrier API and the "micro-sleep" without any worker.
3. Per-namespace worker, debounce, `forget` cancel, recovery from the log, observability.
4. Sync-versus-deferred equivalence test (stubbed LLM) and the supersede-order test (4.2 item 2).
5. Only then wire the optional inline LLM rows (13, 15) to the queue.
