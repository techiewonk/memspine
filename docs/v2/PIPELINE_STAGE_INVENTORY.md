# Pipeline stage inventory (2026-10-14)

This lists every phase that a turn and a question go through. For each phase it records:

- where the output is persisted;
- what is observable today;
- the ids that join it to other phases.

It is the source for [TRACE_INSPECTOR_PLAN.md](TRACE_INSPECTOR_PLAN.md). It comes from a read-only survey of the code on branch `restructure`; nothing was run.

Abbreviations:

- **tap** = `memspine.observability.tap` events.
- **FX** = the `search_forensics()` sink (`core.trace_sink.FORENSICS`).
- **WF** = `write_forensics()` notes (`firewall`, `conflict`).

Ids:

- `rid` = `record_id` (uuid4).
- `win` = `window_id` (12 hex of a hash over `{ns, member record_ids}`).
- `occ` = envelope `occurrence_id`.
- `wid` = intake write id.
- `seq` = `memory_events.seq`.

## 0. Observability mechanisms

### Tap
Files: `observability/tap.py`, `tap_sinks.py`, `tap_report.py`; documented in `WIRETAP.md`.

- **Switch and sinks:** off by default (`observability.tap`). Sinks are jsonl, sqlite, memory, otel and log.
- **Request kinds:** `read`, `write` and `job`.
  - Decorators sit on `Engine.write`, `write_messages`, `write_episode`, `search`, `retrieve`, `assemble`, `read` and `sleep`.
  - Window extraction and each sleep stage open their own `job` request.
- **Field guard:** only ids, numbers and booleans pass. Text needs `tap.content()` plus `content: true`.
- **Forensics bridge:** FX keys are re-emitted as `read.forensics.<key>` and `read.forensics.cut`. WF notes are re-emitted as `write.forensics.firewall` and `write.forensics.conflict`.
- **Report:** `memspine tap report` (gap rules, html, json).

### Other ledgers
- `ops/ledger.py` OpsLedger → `observability/stage_ledger.py` (counts and durations only).
- `economy/ledger.py` WriteCostLedger (calls and tokens per layer and stage).
- `observability/usage.py`.
- `observability.write_timers`.

### Eval harness logs
See §D.

## A. Write path (turn time)

| # | Phase | file:function | In → out | Persisted | Observable today | Join ids |
|---|---|---|---|---|---|---|
| A1 | API entry | `api/engine.py write / write_messages / write_episode` → `ingest/write.py write, write_messages` | messages → records | – | tap request `write`; `write.received`, `write.messages.received`; ops ledger | session |
| A2 | Intake gateway (`write.intake_v2`) | `ingest/gateway.py write_messages` → `IntakeGateway.submit`, `ingest/intake.py validate_envelope`, `IntakeBook.accept`, `write_id` | turns → envelope/segments → write ids, receipt, readiness | `intake_source`, `intake_write` (wid→rid), `intake_intent` | `write.intake.{received,rejected,committed,applied,resume,recovered,readiness,retire}` | intent, wid, `idem:<wid>` tag |
| A3 | Apply to direct write | `gateway._apply_messages` → `write.write_messages_direct` | fresh turns → batch | – | `write.messages.received` | wid |
| A4 | Prewarm embeddings, prefetch neighbours | `write._prewarm_embeddings`, `_prefetch_neighbours` | texts → embedding cache | embedding cache | none | – |
| A5 | Turn skip filters | `write._write_turns` | skipped role or injected recall → skipped | MARKER `recall_skipped` | `write.turn.skipped` | message index |
| A6 | Role policy and echo guard (`write.role_policy_v2`) | `ingest/role_wiring.decide`, `ingest/roles.detect_echo, route` | role and content → RoleDecision (derive_use, src_class, trust cap, tags) | tags and trust cap on the record | `write.role.turn`, `write.echo.{detected,last_read}` | session (no rid yet) |
| A7 | Stamping: 3 clocks, axes (`write.stamping_v2`) | `write._stamp_turn` → `ingest/stamping.ConversationStamper.turn` → `ingest/stamp.stamp`, `parse_source_time`, `ingest/timeparse` | timestamp, tz, speaker, addressee, participants → `source_at`, tz, precision, spk/addr, src_class, assent check | record columns `source_at`, `source_tz`, `participants`, `spk`, `addr`, `provenance_class` | `write.stamp.{turn,assent}`, `write.time.{parsed,unparsed}` | turn index (stamp write_id not copied to the record) |
| A8 | Other turn tags and links | `write._write_turns` | perspective tags, assistant_claim, recommendation, forget request, correction, assets, reply link (`reply_to`, `antecedent_kind`) | tags; LINK event if associative | `write.assets.*`, `write.asset.*` | `reply_to:<rid>` |
| A9 | Index view (`write.index_view`) | `ingest/index_view.plan_prefixes`, `write._index_view_prefixes` | turn and date → embed/BM25 prefix | `index_text` in the WRITE event payload | `write.index_view.{plan,text}` (counts) | rid |
| A10 | Record build | `write.write` | → `MemoryRecord` (rid, valid_from, trust cap, implicit read-ledger parents) | – | `write.received` | rid |
| A11 | Firewall, redaction, governance, quarantine | `write._write_locked_ex` → `_screen_write`, `_redact_fields`, `_govern_write`, `_assess_write` (`trust/firewall.py`), consent | record → trust, quarantined, instruction_flag | `memory_records` columns; `firewall.reasons` in the payload | `write.firewall.verdict`, `write.quarantine`, `trust.consent`; WF firewall | rid |
| A12 | Door routing and event append | `write._write_screened` → `engine._append_and_project`; `store/events.MemoryEvent` | record → WRITE event (plain / semantic pipeline / cue) | `memory_events` (seq, event_id, kind, payload, fingerprint) | `write.route`, `write.deposit` (action), `write.rejected` | rid, seq |
| A13 | Projectors | `store/projections/projector.py`; `RecordProjector`, `VectorProjector`, `LexicalProjector`, `GraphProjector`, `Neo4jOutboxProjector` | WRITE event → rows, vectors, index docs, graph nodes | `memory_records`; LanceDB (by rid); Tantivy (indexes `index_text` if present); `graph_nodes` (+ `mentions` edges); `projector_offsets` | none per record (only `graph.neo4j.*`, `store.vector.space`) | rid, seq |
| A14 | Post-write hooks | `_corroborate`, `_evolve_links`, `_reopen_session` | corroboration, LINK, SESSION events | event log | none | rid |
| A15 | Window observe and segmentation | `workers/windows.observe` → `derive/segmenter`, `_enqueue`, `derive/call_batcher` | turn joins the open window; cut by turns, tokens, topic, idle, gap, day, session end | memory queue or outbox (`outbox`, `outbox_item` payload `{kind, session, reason, record_ids}`) | `write.segment.cut`, `write.window.cut`, `write.batch.held`, `runtime.outbox.*` | member rids (`win` derived, not stored) |
| A16 | Live-turn filter | `windows._live_turns` | → live, unquarantined, role-allowed | – | `write.role.derive_skip`, `write.window.skipped` | win, rid |
| A17 | Pre-pass (`write.prepass`) | `windows._prepass_verdict` → `derive/prepass.py` (rules + GLiNER) | → skip / episode_only / full | learned aliases (events) | `write.prepass.window`, `write.window.skipped`, `write.learned.*` | win |
| A18 | Prompt context (`write.prompt_v2`) | `windows._with_prompt_context` → `derive/prompt_context.py` | previous-window tail → CONTEXT; KNOWN entities | – | `prompt.render` | – |
| A19 | **Extraction LLM call** | `windows._call`, `PromptWindowExtractor`, `_unified_call`, `_run_groups`, `_prepare_batch`; prompts `structured.window`, `structured.repair`, `window_extract.yaml` | numbered transcript (+ CONTEXT/KNOWN) → JSON `facts`/`edges`/`episode` | window cache (parsed JSON by hash); **prompt and reply not persisted** | `write.extract.call`, `write.prompt.v2`, `model.call` (tokens, ms), `economy.cost`, `write.batch.*`, `write.window.planned`, `write.extract.{rejected,failed}` | win, batch |
| A20 | Item validation and repair | `windows._compile_items` → `derive/items.process_items` → `envelope_compiler`, `derive/compile.compile_items` | items → `AssertionEnvelope` (wid, occ, key, scope, evidence = turn rids, trust); one repair call | dead letters (`<path>.deadletters.sqlite`) | `write.item.{valid,repaired,dead}` | win, occ, wid |
| A21 | Envelope stages | `derive/envelope_stages.run_envelope_stages` (list in `ingest/windows._envelope_stages`) | envelopes → envelopes | in `fingerprint.envelope` | `write.envelope.stage` (before/after counts) | win |
| A21a | 100 temporal | `derive/temporal_stage.py` | when/until, span, source_at → event time, precision, basis, relations | envelope `time` | `write.temporal` | win, occ |
| A21b | 200 grounding | `derive/grounding_stage.py` | claim vs cited lines → grounded/uncertain/tentative/dropped; assent | envelope `grounding`, `checks` | `write.ground.{checked,dropped,summary,assent}` | win, wid |
| A21c | 250 coverage | `derive/coverage_stage.py` | uncovered lines → ≤1 re-extraction call | envelopes | `write.coverage.window` | win |
| A21d | 300 resolver | `derive/resolve_stage.py` | mentions → entity or `pending_merge` | envelope mentions; alias events | `write.resolve.{mention,window}` | win, entity |
| A21e | 400 integration | `derive/integration_stage.py` | live facts + cardinality → append/supersede/merge/coexist/contest/noop/defer | `env.verdict`; tags `integ:*` | `write.integrate.{decision,stage}` | occ, target rid |
| A21f | 500 pre-pass cross-check | `derive/prepass.prepass_stage` | unseen names → `extras.prepass_unseen` | envelope | `write.prepass.unseen` | win |
| A22 | Section validation | `windows._deposit_sections`, `validate_facts/edges/episode` | sections → deposits; edges into `store.pending` (memory, capped) | edges in memory only | `write.extract.{facts,edges,episode,rejected}` | win |
| A23 | Fact deposit | `windows._store_envelopes` → `Engine._deposit_mined_fact` (`derive/pipeline/integrate.py`) | envelope → semantic record (channel `mining`, parents = cited turn rids, tags `window:<win>`, `envelope_v2`, `integ:*`; `fingerprint.envelope`; trust ≤ min parents) | through the door (A24) | `write.extract.deposit` (rid, parents count), `write.integrate.peer_disputed` | rid, parent rids, win; occ only inside the envelope (`envelope.record_id` unset) |
| A24 | Derived-write door and lineage | `derive/write.write_derived` → `_write_locked` (A11–A13), `add_lineage` | gates (gate-3, consent) → held or open | `lineage` (child, parent, role, span; with `write.lineage_table`) | `derive.gate.hold`, `trust.gate`, `derive.lineage.{write,refused}` | child ↔ parent rid |
| A25 | Semantic dedup and conflict ladder | `memories/semantic/store.write`, `_merge`, conflict resolution, `write_pipeline.py` | fact → added / merged / superseded / conflicting | WRITE, MERGE, CONFLICT events; `status`, `evolve_to`, `valid_to` | outcome only as `write.deposit.action`; rule and incumbent only as WF conflict | rid, incumbent rid |
| A26 | Episode fragment deposit | `windows._store_episode` → `ingest/windows._deposit_window_episode` | episode → semantic record (`episode_fragment`, `window:<win>`) | door; lineage | `write.extract.deposit` (layer = episode) | rid, win |
| A27 | Typed routing | `gateway._route_typed`, `role_wiring.TypeRouter` | keyed turn → typed record (semantic/skill/plan), parent = raw rid | records | `write.route.{decision,typed,skipped,failed}` | raw ↔ typed rid, wid |
| A28 | Graph mirror | `GraphProjector`, `Neo4jOutboxProjector`, `services/graph/*` | WRITE/LINK/FORGET → nodes, edges | `graph_nodes`, `graph_edges`; Neo4j outbox | `graph.neo4j.*` | rid = node id; `ent:<ns>:<canon>` |

The typed edge compiler (assertion → graph) runs only in sleep (B12).

## B. Sleep and offline path

| # | Phase | file:function | In → out | Persisted | Observable today | Join ids |
|---|---|---|---|---|---|---|
| B0 | Trigger | `derive/scheduler.DerivationScheduler`, `Engine.sleep` → `derive/pipeline/scheduler.sleep`, `build_sleep_v2` | deposits, idle, pressure → job | – | `job.trigger` | ns |
| B1 | Namespace job | `derive/job.py LeaseStore, NamespaceJob._drive, SleepHandle` | per-stage loop | `<path>.sleep`: `sleep_lease`, `sleep_cycle`, `sleep_watermark`, `sleep_state`, `sleep_deferred`, `sleep_fence_log` | `job.lease`, `job.sleep.handle`, `job.watermark`, `job.deferred`, `job.budget` | ns, cycle, stage, epoch |
| B2 | Stage runner | `workers/schedule.run_sleep_stage`, `sleep_stage_order`, `run_sleep_cycle` | stage → stats | – | ops ledger; cost ledger; `job.stage.{start,end}` | stage |
| B3 | Window flush | `ingest/windows.flush_windows` | closes windows | A15–A26 | `write.*` | win |
| B4 | Session close (call 2) | `workers/windows.close_session`, `stages/close.session_close` → `close_v2.run_close_v2`; `derive/session_close.py` | session turns + episode stubs → summary, stitched episodes, profile ops | records (channel `session_close`); MARKER `stage_done` (`session_key`, `members_fp`) or `session_closed` | `job.close.{call2,idem,dedup}` | session, members_fp, parent rids |
| B5 | Retention and erasure re-derive | `stages/retention.py`, `trust/pipeline/retention_v2.py`, `stages/erasure_rederive.py` | TTL → hard forget | FORGET events, audit chain | `job.retention.*`, `trust.erasure.*`, `trust.audit.*` | rids |
| B6 | consolidate | `stages/consolidate.py`, `memories/episodic/sessions.detect_sessions` | sessions → summary (channel `consolidation`) | CONSOLIDATE event + record; lineage | stage-level only | session key, member rids |
| B7 | mine_facts, project_facts, predict_calibrate, anticipate | `stages/mining.py`, `predict.py`, `anticipate.py` via `session_stage._run_session_stage` | per session → facts (`mined:<session>`), cues | records; MARKER `stage_done` | stage-level only | session, members_fp |
| B8 | reflect_profile, profile card | `stages/reflect.py`, `stages/profile_card.py` | summaries/insights; guarded profile ops | records; MARKER `profile_card_v2` | `job.profile.*` | session, evidence rids |
| B9 | Graph extraction | `stages/extract_graph.py` (prompts `extract_edges`, `resolve_entity`, `invalidate_edge`) | session transcript (+ window edges) → edge facts, entity resolution, retraction, LINK | records; LINK → `graph_edges`; MARKERs `graph_extracted`, `entity_resolved` | `derive.graph.key` only | source rids, entity nodes |
| B10 | rule_edges, reconcile, histories | `stages/rule_edges.py`, `reconcile.py`, `histories.py` | rule edges; contradiction sweep; near-duplicate merge | DECAY_TRANSITION deltas; `lineage` merge_member | `job.reconcile.sweep`, `job.histories` | rids |
| B11 | patterns, standing answers | `stages/patterns.py`, `stages/standing.py` | observations, precomputed answers | records | `job.patterns`, `job.standing` | rids |
| B12 | compile_edges (v2) | `stages/edge_compiler_v2.py`, `derive/edges.compile_edges` | facts → typed assertion, role, value and time edges | LINK events with props → `graph_edges`; marker | `graph.compile.{start,delta,project,retract}` | rid on edge props; assertion node ids |
| B13 | summarize_entities | `stages/entity_summaries(_v2).py` | entity facts → summary (channel `entity_summary`) | record; MARKER `entity_summarized` | v2 `job.communities.entities`; v1 stage-level | `ent:` ids, member rids |
| B14 | reorganize (communities) | `stages/communities(_v2).py` | graph → Leiden → community summaries | records; MARKER `community_partition` | v2 `job.communities(.summary)`; v1 stage-level | community id, member rids |
| B15 | check_watches, session_lifecycle | `stages/watches.py`, `stages/maintenance.py` | watches; idle sessions → passive | SESSION events | stage-level, `job.prospective.*` | session |
| B16 | decay, compress, sleep_compute, log prune | `stages/maintenance.py`, `decay_v2.py`, `retention.event_log_prune` | tier deltas; cold tier; multi-resolution summaries | deltas; records; pruned events | `job.decay(.compress)`, `job.retention.prune` | rids |
| B17 | Cost and budget | `economy/ledger.py`, `derive/budget.py` | calls and tokens per stage | `LedgerStore` | `economy.{cost,budget}`, `job.budget` | ns hash, stage |

## C. Read and answer path

| # | Phase | file:function | In → out | Persisted | Observable today | Join ids |
|---|---|---|---|---|---|---|
| C1 | API entry | `engine.read` → `read/pipeline/router.read` | query → ReadResult | – | tap `read`; ops; `scope.*` | ns, viewer |
| C2 | Request, contract, tier, route | `request.plan_read` → `read/tiers`, `read/contract`, `contract_rules`, `router_rules`, `layer_state` | → typed contract, tier, legs run and skipped | contextvar | `read.{request.normalised,contract.built,tier.chosen,router.plan}`; FX `read_plan` | – |
| C3 | Scopes, cache, decompose | `router._read_planned`, `request._cache_get/_put`, `read/pipeline/decompose.decomposed_read` | cached result or per-entity sub-reads | memory cache | `read.cache`, `read.decompose` | – |
| C4 | Headers | `compose/pipeline/pack._read_headers`, `sections.py`, `_count_section`, `_duration_section` | standing, timelines, cards, slots, profile, entity summary → headers | – | `compose.standing` only | header rids |
| C5 | Mode routing, planner, list trigger | `router._read_routed`, `request._plan_read_mode`, `_llm_read_plan`, `_list_fires_v2` | full / compose / replay / retrieve; list mode | contextvar | `read.list_trigger_v2`; FX `decisions`, `list_trigger_v2`; `model.call` | – |
| C6 | Assemble loop | `compose/pipeline/windows._assemble_core` | query (+ probes) → searches | – | FX `bridge_gate`, `bridge_phrases` | – |
| C7 | Query embedding | `router._search`, `models/encoders/query` | → vectors | embedding cache | `economy.cost` | – |
| C8 | Vector leg | `engine._vector_leg` → `legs.py`; LanceDB | → (rid, cos) | LanceDB | `read.leg`; FX `vector` | rid |
| C9 | BM25 leg | `legs._lexical_leg`, `router._lexical_guarded`, `read/legs/lexical.py` | → (rid, bm25) | Tantivy | `read.leg`; FX `lexical` | rid |
| C10 | Temporal leg | `read/legs/temporal.py` | time window → rids | – | `read.leg`; FX `extra_legs` | rid |
| C11 | Perspective | `read/pipeline/lift._perspective_leg/_apply_perspective` | speaker/subject tags → leg and multiplier | – | FX `perspective` (only under trace_full) | rid |
| C12 | Entity leg | `legs._entity_leg_v2`, `read/entity_leg.py` | entities → records | – | `read.leg.entity`; FX `extra_legs` | ent, rid |
| C13 | Graph leg | `legs._graph_leg`, `_graph_hop_leg`, `read/legs/graph.py` | seeds → walk → records | graph tables | `read.graph_hop`; FX `extra_legs` | rid, node |
| C14 | Episode/session leg and lift | `read/legs/episode.py`, `legs._session_leg`, `lift.lift_episode_hits` | episode hits → source turns | – | `read.lift`; FX `extra_legs` | episode rid → turn rids |
| C15 | Other legs | maxsim, speaker vote, person-time, probe, encoder, freshness | ranked lists | – | `read.freshness`; FX | rid |
| C16 | Fusion | `router._search`; `read/pipeline/fusion.fuse_families`; `rrf_fuse/minmax_fuse` | legs → fused pool | – | `read.fusion.pool` (family path); FX `fused`; cut `pool_cut` | rid |
| C17 | Date prior, pool protection | `router._date_prior` → `date_prior.py`; `_protected_ids`, `_family_slots` | boost; protect each leg's top-N | – | `read.date_prior`; FX `protected`, `pool_family` (not in default fz) | rid |
| C18 | Gates | `read/pipeline/gate._gate_hits` + header hide, scope, perspective filter | → admitted candidates | – | FX `cuts` with reason codes (trace_full) | rid |
| C19 | Post-pool refinements | graph rerank, causal walk, balanced pool, relevance filter, `lift_stage` | pool reshaped | – | `read.lift.stage`; FX `lift` | rid |
| C20 | Rerank, composite score | `read/pipeline/rerank.rerank_v2_stage`, `chain_score` (jina), `composite_score` | raw scores → blend → composite; keep cuts | – | `read.rerank.scored`, `models.rerank_chain`, `read.rerank_v2_search_pool`; FX `pool`, `reranker`, `rerank_scores`, `final` (no composite parts) | rid |
| C21 | Coverage select, MMR | `router._coverage_reorder`, `_mmr_order` | reorder | – | `read.coverage_select` | rid |
| C22 | Access write-back, read ledger | end of `_search` | RETRIEVE event; `_record_reads` | `memory_events`; access stats; memory ledger | none | rids |
| C23 | Assembly and pack | `compose/pack.AssemblyPolicy.assemble`, `_dedupe_candidates`, `bundles`, `budget` | → AssembledContext (records, boundary, abstained, tokens) | – | `compose.{pack.dropped,bundles,budget}`; FX `dedupe_dropped`, cuts | rid |
| C24 | Replay window, neighbours | `compose/pipeline/windows._window_turns`, `_expand_neighbours_v2`, `_attach_antecedents` | ±N session neighbours | – | `compose.neighbours`; FX `window` (trace_full), `reference_edges` | rid |
| C25 | Render | `compose/pipeline/render._render`, `_annotate_dates`, `render_trust.py` | → dated, wrapped text | – | `compose.render.bands` | rid |
| C26 | Compose mode | `windows._compose`, `_compose_checked` (prompts `sufficiency*`, `query_rewrite@compose`) | pooled probes, completeness re-search | – | `model.call` only | rid |
| C27 | Agentic, event ledger, deep_v2, obligations | `read/pipeline/deep.py`, `read/deep.py`, `read/obligations.py` | more queries, more records | – | `read.deep.step`, `read.obligations.report`; FX | rid |
| C28 | Relevance gate | `gate._calibrated_gate`, `_gate_v2`, `_gate_bypassed` | abstain or drop | calibration event | `read.gate.decision`; FX; cuts | rid |
| C29 | Overlays | `legs._read_overlay`, `_asset_overlay`, `_external_overlay(_v2)` | extra evidence | – | `read.external`; FX | – |
| C30 | Typed header, consent, audit | `request.add_typed_header`, `_consent_context`, `_audit_read` | contract line; filtered context; READ_AUDIT | event log | `read.contract.header`, `trust.consent`, `trust.audit.append` | rids |
| C31 | Harness query and truncation | `evals/…/systems/memspine_system.query`, `runner._answer` | → RetrievedContext within the budget | `results.jsonl`, `trace.jsonl` | see D | query_id, item, turn ids |
| C32 | Reader | `evals/memspine_evals/readers.py` (`QA_PROMPTS`, routed, gated) | context + question → answer | `results.jsonl` | `reads.jsonl` (trace_full) | query_id |
| C33 | Reader wrappers | `refusal.py`, `count_verify.py`, `date_repair.py`, `slot_verify.py`, `compose/answer_check.py` | retry or repair | row `meta` | `reads.jsonl reader_calls` | query_id |
| C34 | Judge | `judge.py`, `judge_prompts.py`, `judge_conventions.py` (v1–v3, strict list), `rejudge.py` | → score (+ convention columns) | row score, meta | `reads.jsonl judge_calls`, `verdict` | query_id |

## D. Eval-harness logs

Writers are in `evals/memspine_evals/systems/memspine_system.py` unless noted.

| File | Writer | When | Fields |
|---|---|---|---|
| `ingest.jsonl` per turn | `_write_ingest_log` | `MEMSPINE_FORENSICS_DIR` | `run_id, item, turn, session, speaker, source_timestamp, source_text, written, record_id, stored_text, text_identical, valid_from, memory_type, group_id, session_id, batch_size, quarantined, trust, status`, `write_timers` |
| `ingest.jsonl` `window_stats` | `_write_window_log` | after `flush_windows` | cumulative window counters (calls, cache hits, parse failures, facts returned/accepted/invalid, items valid/repaired/dead, deposits, envelope-stage notes) |
| `forensics.jsonl` per question | `_write_forensics` | per query | `query, vector, lexical, extra_legs, fused, pool, reranker, decider, decisions, relevance_*, agentic*, rerank_scores, final, context_records{turn,text}, context_tokens`. `--trace-full` adds `trace_full{query_analysis, perspective, read_overlay, abstain_on_raw, dedupe_dropped, records, final_not_in_context, cuts, window, assembled}` |
| `write_trace.jsonl` | `_write_ingest_log`, `_write_derived_trace` | `--trace-full` | per turn: stored record (tags, entity, attribute, status, version, evolve_to, trust, fingerprint, source{parents}), firewall signals, engine_events; derived rows with `parent_turns`, before and after sleep |
| `results.jsonl` | `runner._answer` | always | question, gold, answer, score, tokens, latencies, retrieved_ids, recall, meta (judge raw, conventions, retry) |
| `trace.jsonl` | `memspine_evals/trace.py` | always | deposit rows (record ids, model calls), cycle rows (E_t, M_ctx_t sha and tokens, y_t) |
| `reads.jsonl` | `runner` → `trace_full.write_read` | `--trace-full` | context_text, answer, reader_raw, reader_calls, judge_calls, verdict |

Notes:

- The default `forensics.jsonl` drops these FX keys: `read_plan`, `query_contract`, `rerank_v2`, `freshness`, `list_trigger_v2`, `lift`, `protected`, `pool_family`, `relevance_gate_v2`, `deep_v2`, `obligations`, `event_ledger`, `reference_edges`, `asset_evidence`, `external_evidence`.
- `evals/memspine_evals/call_cache.py` holds raw engine-role prompts and replies, keyed by a hash of the request, not by window.

## E. Join ids

| Id | Where |
|---|---|
| Dataset turn id (`D1:2`) | Harness only: `_origin[record_id]`, `ingest.jsonl.turn`, `forensics[].turn`, `trace.jsonl`. Derived records have none; `write_trace` gives `parent_turns`. |
| `record_id` | Every record, event, vector and lexical doc, graph node, lineage row, `source.parents`. The primary write ↔ read key. |
| `wid`, `intent_id` | `intake_write` (with rid); tag `idem:<wid>`. |
| `seq`, `event_id` | `memory_events`; tap events do not carry them. |
| `window_id` | Tags `window:<win>`, `mined:win:<win>`; tap fields; dead letters; outbox payload. The member list is not stored elsewhere. |
| Envelope ids (`write_id`, `occurrence_id`, `assertion_key`, evidence turn rids, `prompt_hash`, `verdict`) | Inside `fingerprint.envelope` (`Engine.get_envelope(rid)`). `envelope.record_id` is unset; no event links occ → rid. |
| Lineage | `lineage(child, parent, role, span, …)` with `write.lineage_table`; `source.parents`; `member_record_ids`; markers `members_fp`, `session_key`. |
| Graph | `graph_nodes.node_id` = rid or `ent:<ns>:<canon>`; assertion/value/time node ids; `graph_edges(src, dst, rel_type)` with `record_id` in props. |
| Sleep | `job_id`, `ns`, `cycle`, `stage`, `epoch`; `session_key`, `members_fp`. |
| Read | `query_id` (harness), `run_id`, `item`; candidate ids are rids or harness turn ids. The tap request id carries no question id. |

## F. Gaps (what the inspector must close)

| # | Gap |
|---|---|
| F1 | **No tap ↔ harness join key.** Tap requests have no query, run, item or turn id. The harness never enables the tap. Stamp, role and echo events carry no rid; intake gives no wid → rid pairs in events. |
| F2 | **Projectors emit nothing per record.** The embedding call is not tapped. `index_text` lives only in the payload. Index membership has to be queried from the stores. |
| F3 | **Extraction prompt, reply and parsed sections are not exposed** per window. They survive only in the eval call cache, keyed by hash. |
| F4 | **Window membership is not an event.** Windows that produce nothing leave only counters. `window_stats` is cumulative. |
| F5 | **Envelope lifecycle cannot be followed.** No occ → rid event exists. Dropped envelopes are not persisted. Stages log counts, not per-envelope diffs. Dead-letter content is kept only with content opt-in. |
| F6 | **The dedup and conflict ladder** (rule, incumbent) exists only as WF notes, not first-class events. |
| F7 | **Window edges are lost silently.** They sit in memory only and are dropped if the process dies before sleep (only the counter `edge_batches_dropped` records it). |
| F8 | **Many sleep stages have stage-level stats only:** consolidate, mine/project/predict, anticipate, reflect, extract_graph, rule_edges, summarize_entities v1, reorganize v1, watches, lifecycle, decay v1, compress v1. |
| F9 | **Sleep markers are persisted but have no reader or report.** |
| F10 | **The default forensics drops 15 FX keys.** `cuts` and `window` are kept only under `--trace-full`. |
| F11 | **Header blocks have no event.** Their records appear in the context unlabelled. `context_records` has no rid outside `trace_full`. |
| F12 | **Read-side planner, decider and contract calls** show only as `model.call`, with no prompt or reply and no link to their stage. |
| F13 | **Rerank composite parts and `rank_pairs` order are not logged.** |
| F14 | **The RETRIEVE write-back and the implicit-parent read ledger are invisible.** |
| F15 | **Reader and judge raw text are kept only under `--trace-full`.** Verifiers and date repair are not in forensics. |
| F16 | **Cost cannot be attributed to a turn, window or question** (ledgers carry no ids). |
| F17 | **Outbox prepare and commit are not wrapped in a tap request**, so their events may be orphaned. |

## G. Existing tooling

### Engine (`src/memspine`)
- `memspine tap report` (`observability/tap_report.py`, gap rules, html).
- `Engine.ops_report()`, `cost_report()`, `write_timers()`, `describe()`, `get_envelope()`.
- `search_forensics()`.
- Docs: `WIRETAP.md`, `pipelines/{insert,offline}.md`, `data/screen_internals_*.json`.

### Evals (`evals/`)
- `forensics_report.py` joins results, forensics, ingest and gold, attributes the first losing stage, and writes `per_question.jsonl`.
- `build_html_report.py` shows a 9-step per-question trace.
- `build_forensics_explorer.py` is a local text explorer.
- `full_run_forensics.py`.
- `build_locomo_problem_atlas.py`, with `locomo_atlas_data.py`, `locomo_atlas_html.py`, `locomo_problem_classes.py` and `build_locomo_solution_map.py`.
- `failure_buckets.py`, `error_analysis.py`, `judge_audit.py`, `judge_agreement.py`, `rejudge.py`, `r14_*`.

There is no turn-level write or sleep inspector and no step mode today. That is what TI-1..TI-7 add.
