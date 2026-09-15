# Ecosystem Memory Taxonomy (Pass #6)

> Per-engine memory-type inventory with crosswalk to memspine's 9 types. Full CSV: [`exports/ECOSYSTEM_MEMORY_TYPES.csv`](exports/ECOSYSTEM_MEMORY_TYPES.csv).

memspine types: working · semantic · episodic · procedural · reflective · associative(graph) · prospective · shared · firewall/trust.

---

## memspine (13 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| working | short-term / hot window | working | hot window; overflow -> episodic; persona superseded in plac | memories/working/manager.py:WorkingMemory; memories/working/ |
| semantic (fact) | long-term declarative | semantic | ACTIVATED -> ARCHIVED on supersession (evolve_to) / DELETED  | memories/semantic/store.py:SemanticMemory; core/records.py:M |
| semantic:consolidation (summary view) | derived long-term | semantic | superseded (ARCHIVED, evolve_to) when session membership dri | workers/pipelines.py:_consolidate_session |
| semantic:reorganize (community parent) | derived long-term / graph rollup | semantic + associative(graph) | superseded with weight-0 community-edge tombstones when the  | workers/pipelines.py:_reorganize_community; memories/associa |
| semantic:extract_graph (edge fact) | derived long-term / KG edge | semantic + associative(graph) | same M4 lifecycle as any keyed fact; opt-in feature | workers/pipelines.py:extract_graph; memories/semantic/write_ |
| episodic | long-term event / transcript | episodic | decay tiers; consolidated into semantic summaries; DELETED o | memories/episodic/store.py:EpisodicMemory; memories/episodic |
| resource (ingest chunk) | long-term document | semantic (resource sub-type; ingested docs) | decay + cold-tier compression like episodes; forget cascade | memories/resource/store.py:ResourceMemory; memories/resource |
| procedural (skill / plan / prompt) | long-term how-to | procedural | M13.4 ladder; verified->active requires dry_run_passed; held | memories/procedural/skills.py; memories/procedural/lifecycle |
| reflective (insight) | long-term meta | reflective | no reflecting on reflections beyond depth cap; parents outsi | memories/reflective/reflections.py:ReflectiveMemory; memorie |
| associative (link / graph edge) | graph / relational overlay | associative(graph) | LINK_BUDGET per node; prune_weakest tombstones; reserved rel | memories/associative/store.py:AssociativeMemory; memories/as |
| prospective (watch) | future-oriented trigger | prospective | fires -> stays in due() until acknowledged (archived); inval | memories/prospective/watches.py; memories/prospective/trigge |
| shared (grant / subscription) | cross-namespace access control | shared | grant archived on revoke via delta; foreign records never co | memories/shared/grants.py:SharedMemory; memories/shared/subs |
| firewall / quarantine / trust (cross-cutting) | security governance overlay | firewall/trust | QUARANTINED -> ACTIVATED via independent trusted corroborati | core/firewall.py:Firewall; core/policies/trust.py:TrustPolic |

---

## cognee (18 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Data (relational record) | raw source / ingest record | episodic (raw ingest event) / substrate SoT | persists until forget(dataset/data_id); survives forget(memo | cognee/tasks/ingestion/ingest_data.py:ingest_data; cognee/ap |
| Document (typed) | structural node | semantic (structural) | rebuildable projection; removed on forget | cognee/tasks/documents/classify_documents.py; cognee/modules |
| DocumentChunk | chunk / working unit | working / episodic | rebuildable; removed on forget | cognee/modules/chunking/models/DocumentChunk.py:DocumentChun |
| Entity | semantic graph node | semantic | idempotent merge on re-ingest (same name -> same id); remove | cognee/modules/engine/models/Entity.py:Entity |
| EntityType | semantic graph node (schema) | semantic (ontology) | idempotent merge; removed on forget | cognee/modules/engine/models/EntityType.py:EntityType |
| Edge / relationship (triplet) | associative graph edge | associative (graph) | dedup by src+rel+tgt; removed on forget | cognee/modules/graph/utils/expand_with_nodes_and_edges.py; c |
| TextSummary | semantic summary node | semantic (compressed) | rebuildable; removed on forget | cognee/tasks/summarization/models.py:TextSummary |
| GlobalContextSummary | community summary node (hierarchical) | reflective / semantic (community) | built by memify; removed on forget/rebuild | cognee/tasks/summarization/models.py:GlobalContextSummary; c |
| Event | temporal node | episodic (temporal) | rebuildable; removed on forget | cognee/modules/engine/models/Event.py:Event |
| Timestamp / Interval | temporal valid-time node | episodic (bitemporal valid-time; no separate txn-time axis) | rebuildable; removed on forget | cognee/modules/engine/models/Timestamp.py; cognee/modules/en |
| Skill | procedural playbook | procedural | versioned via content_hash; is_active flag; removed on forge | cognee/modules/engine/models/Skill.py:Skill; cognee/modules/ |
| SkillImprovementProposal / SkillRun | procedural evolution record | procedural / reflective | memify-managed | cognee/modules/engine/models/SkillImprovementProposal.py; co |
| Tool | procedural callable | procedural | registered at import or ingest; permission-checked at use | cognee/modules/engine/models/Tool.py:Tool |
| Session lesson (distilled) | reflective durable lesson | reflective / shared (session-derived) | write-gated (accept=false already_known/not_durable/unsuppor | cognee/modules/session_distillation/distill.py; cognee/memif |
| Agent-trace lesson / CodingRule | reflective learned rule | reflective / procedural | memify-managed; removed on forget | cognee/tasks/codingagents/coding_rule_associations.py; cogne |
| Session Q&A + session-context (goals/rules/preferences/lessons) | working session state | working | session-scoped; distilled to durable lessons via memify | cognee/infrastructure/session/*; cognee/tasks/memify/apply_f |
| feedback_weight / frequency_weight (reinforcement) | retrieval reinforcement signal | reflective (reinforcement, not decay) | continuously updated by memify; no time-decay | cognee/tasks/memify/apply_feedback_weights.py:stream_update_ |
| Permission / ACL (dataset-scoped) | access control (not content-trust) | firewall/trust (partial: permission-based, no content anomaly/quarantine) | per user/dataset | cognee/modules/users/*; cognee/modules/data/methods/get_auth |

---

## graphiti (6 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| EpisodicNode | episodic | episodic | Persisted; content optionally cleared post-extraction (store | nodes.py:EpisodicNode; graphiti.py:add_episode; graphiti.py: |
| EntityNode | semantic (entity) | semantic | Deduped/merged (label promotion, attribute replace); deleted | nodes.py:EntityNode; node_operations.py:resolve_extracted_no |
| EntityEdge (RELATES_TO fact) | semantic (relational fact) + bitemporal | semantic | Bitemporal: superseded facts invalidated (invalid_at/expired | edges.py:EntityEdge; edge_operations.py:resolve_extracted_ed |
| CommunityNode | associative (graph) / consolidated | associative(graph) | Fully rebuilt each build_communities (remove_communities DET | nodes.py:CommunityNode; community_operations.py:build_commun |
| SagaNode | episodic thread / consolidated summary | episodic | Persisted; summary advanced incrementally via dual watermark | nodes.py:SagaNode; graphiti.py:summarize_saga, _process_epis |
| EpisodicEdge (MENTIONS) | associative (provenance link) | associative(graph) | Created per episode; deleted with episode. | edges.py:EpisodicEdge; edge_operations.py:build_episodic_edg |

---

## mem0 (6 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| fact_memory (inferred, flat) | semantic/preference fact | semantic | Append-with-linking (no auto-update/merge in V3); explicit u | mem0/memory/main.py:_add_to_vector_store:837; mem0/configs/p |
| raw_message_memory (infer=False) | verbatim working note | working | Direct CRUD via update()/delete(); no LLM processing | mem0/memory/main.py:838-872 |
| procedural_memory | agent execution summary | procedural | Created explicitly on demand; no background refresh; CRUD as | mem0/memory/main.py:_create_procedural_memory:1935; mem0/con |
| entity_record | associative entity node | associative(graph) | Merged on write; ids stripped on memory update(text-changed) | mem0/memory/main.py:_add_to_vector_store:1044-1160, :_comput |
| change_history (audit) | audit/tombstone log | reflective | Immutable append; DELETE writes tombstone row (is_deleted=1) | mem0/memory/storage.py; mem0/memory/main.py:_delete_memory:2 |
| message_buffer (session) | raw conversational buffer | working | Rolling window (last N); context only, not retrievable memor | mem0/memory/main.py:878, :946, :1000 |

---

## MemOS (13 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| WorkingMemory | tree_textual | working | remove_oldest_memory(keep_latest=20) on sync add; scheduler  | tree_text_memory/organize/manager.py:_add_memories_batch,_cl |
| LongTermMemory | tree_textual | episodic+semantic | reorganizer cluster→summary parent nodes; conflict detect/re | memories/textual/tree.py:TreeTextMemory; organize/reorganize |
| UserMemory | tree_textual | semantic | reorganize scope=UserMemory; conflict resolution | templates/mem_reader_prompts.py memory_type enum; retrieve/s |
| OuterMemory | tree_textual | semantic | tree lifecycle (not merged into UserMemory) | memories/textual/item.py memory_type; retrieve/internet_retr |
| RawFileMemory | tree_textual | episodic | size cap 1500; DOC_REORGANIZE_PROMPT summarization | mem_os/core.py:add doc branch; organize/manager.py memory_si |
| PreferenceMemory (pref_mem cube) | preference_vector | semantic (preference facet) | LLM NAIVE_JUDGE_UPDATE_OR_ADD; datasketch MinHash dedup pres | memories/textual/preference.py; prefer_text_memory/utils.py: |
| ActivationMemory (act_mem) | activation_kv | working (runtime) | scheduler activation manager; not graph-backed | memories/activation/kv.py:KVCacheMemory; configs/mem_os.py e |
| ParametricMemory (para_mem) | parameter | procedural (loose) | cube dump/load; no distillation code path found | memories/parametric/lora.py:LoRAMemory; parametric/item.py:P |
| ToolSchemaMemory / ToolTrajectoryMemory | tool | procedural | tree lifecycle | templates/tool_mem_prompts.py; retrieve/searcher.py tool_mem |
| SkillMemory | skill | procedural | authoring/script/tool generation prompts | templates/skill_mem_prompt.py; pyproject [skill-mem] extra |
| Context | dream_index | associative(graph) | dream maintenance re-summarize | dream/prompts/context_binding_prompt.py, context_summary_pro |
| DreamMemory (dream_content insight) | dream_insight | reflective | persistence CREATE/UPDATE/MERGE/ARCHIVE dispatch | dream/pipeline/reasoning.py; dream/pipeline/persistence.py:_ |
| GeneralTextMemory / NaiveTextMemory (non-tree) | flat_textual | episodic | supports update/delete by id directly (unlike tree) | memories/textual/general.py, naive.py; mem_os/core.py:add no |

---

## honcho (7 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Message | raw conversation log | episodic | persists until session/workspace deletion (cascade); never d | src/models.py:206 (Message); src/utils/search.py:314 (search |
| Observation — level=explicit | atomic fact about a peer | semantic | dedup supersession (cosine>=0.95) soft-deletes weaker copies | src/deriver/prompts.py:40; src/models.py:379; src/utils/type |
| Observation — level=deductive | logical conclusion with premises | reflective | created during dream cycle; outdated ones soft-deleted by de | src/dreamer/specialists.py:451; src/utils/agent_tools.py:462 |
| Observation — level=inductive | behavioral pattern / generalization | reflective | created during dream cycle; not written to peer card; dedup/ | src/dreamer/specialists.py:639; src/utils/agent_tools.py:477 |
| Observation — level=contradiction | conflict flag | firewall/trust | created during dream cycle / dialectic; persists until resol | src/utils/types.py:257; src/dreamer/specialists.py:549 (syst |
| Peer card | stable identity markers | semantic | fully re-emitted on each update (omitted entries dropped, un | src/crud/peer_card.py:17,50; src/dreamer/specialists.py:487  |
| Session summary (short / long) | rolling conversation summary | working | supersedes previous summary (inclusive rewrite up to covered | src/utils/summarizer.py:40,101,133; SummaryType src/utils/su |

---

## OpenMemory (12 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| semantic sector memory | cognitive-sector | semantic | slowest decay (lambda 0.005); reinforced on retrieval; conso | core/constants.py:SECTOR_CONFIGS['semantic']; memory/hsg.py: |
| episodic sector memory | cognitive-sector | episodic | fast decay (0.015); recency-weighted retrieval | core/constants.py:SECTOR_CONFIGS['episodic']; memory/hsg.py: |
| procedural sector memory | cognitive-sector | procedural | slow decay (0.008); reinforced on use | core/constants.py:SECTOR_CONFIGS['procedural'] |
| reflective sector memory | cognitive-sector | reflective | slowest decay (0.001); consolidation output; roots salience= | core/constants.py:SECTOR_CONFIGS['reflective']; memory/refle |
| emotional sector memory | cognitive-sector | semantic | fastest decay (0.02); strong episodic/reflective resonance l | core/constants.py:SECTOR_CONFIGS['emotional']; ops/dynamics. |
| auto-reflection / consolidated cluster | derived-consolidation | reflective | background reflection loop (PY default-on, JS opt-in); no LL | memory/reflect.py:run_reflection,cluster,summ |
| root/child document memory | structural-ingest | episodic | root decay_lambda=0.1; children normal sector decay | ops/ingest.py:mk_root,mk_child,link,ingest_document |
| waypoint (associative link) | graph-edge | associative | reinforced +0.05 on traverse, +0.1 on re-observe; pruned whe | memory/hsg.py:create_*_waypoint,expand_via_waypoints,prune_w |
| temporal fact (bitemporal SVO) | structured-fact | semantic | supersession on conflict; apply_confidence_decay (floor 0.1, | temporal_graph/store.py:insert_fact; temporal_graph/query.py |
| temporal edge (fact-to-fact relation) | graph-edge | associative | invalidate_edge sets valid_to; no decay | temporal_graph/store.py:insert_edge,invalidate_edge; tempora |
| user summary / profile | derived-profile | reflective | regenerated periodically; overwritten each pass | memory/user_summary.py:gen_user_summary,update_user_summary, |
| LangGraph node memory (JS) | integration-tagged | working | same as underlying sector memory; reflective synthesis heuri | packages/openmemory-js/src/ai/graph.ts:store_node_mem,node_s |

---

## ReMe (9 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| session transcript (raw dialog) | raw conversation log / working buffer | working | append/rewrite by session_id; kept as durable provenance; de | reme/steps/evolve/auto_memory.py:AutoMemoryStep._save_sessio |
| daily note (conversation card) | episodic daily memory | episodic | created then merged in place (append-timeline / rewrite-curr | reme/steps/evolve/auto_memory.py:AutoMemoryStep; reme/steps/ |
| resource card | semantic memory from ingested files | semantic | created then reconciled (add/remove/modify sections) when th | reme/steps/evolve/auto_resource.py:AutoResourceStep; reme/co |
| digest node — procedure bucket | procedural long-term memory | procedural | consolidated nightly by dream; additive updates (never remov | reme/steps/evolve/dream/integrate.py:DreamIntegrateStep; rem |
| digest node — personal bucket | user/team preference & identity memory | semantic | consolidated by dream; one node per preference; CORRECT on m | reme/steps/evolve/dream/integrate.yaml:integrate_system_prom |
| digest node — wiki bucket | general-knowledge / precedent semantic memory | semantic | consolidated by dream; REFINE grows precision not volume; CO | reme/steps/evolve/dream/integrate.yaml:integrate_system_prom |
| interests.yaml topics | proactive / prospective interest surface | prospective | written nightly by dream; same-day preserved unless duplicat | reme/steps/evolve/dream/topics.py:DreamTopicsStep; reme/step |
| wikilink graph edges | associative graph over memory files | associative(graph) | rebuilt from files on boot (rebuild_links); real<->virtual p | reme/components/file_graph/local_file_graph.py:LocalFileGrap |
| day-index page + FileChunk projections | derived index / retrieval projection | associative(graph) | fully rebuildable projector (reindex job wipes + rebuilds);  | reme/steps/index/update_changes.py:UpdateIndexStep; reme/com |

---

## unimem (2 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| memory | flat generic memory item (single undifferentiated kind) | semantic (LLM-extracted facts) / working (raw chat turns via UnimemChatMessageHistory) — one physical kind spanning both | created on add; overwritten in place on update (last-write-w | unimem/memory.py:Memory.add; unimem/backends/in_memory.py:In |
| audit_event | optional append-only history/audit log (best-effort, secondary) | episodic / event log (but non-authoritative — unlike memspine's event-sourced core, projectors are never rebuilt from it) | append-only; never pruned, updated, or read back by the engi | unimem/audit.py:create_audit_log; unimem/audit.py:create_sql |

---

## LightMem (9 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| factual MemoryEntry | extracted fact | semantic | persistent; may be merged/deleted by offline_update_all_entr | src/lightmem/memory/utils.py:14 MemoryEntry; lightmem.py:418 |
| relational MemoryEntry (event mode) | interpersonal/emotional relation | associative(graph) | persistent; same offline update/summary lifecycle as factual | src/lightmem/memory/prompts.py:219; openai.py:192 _extract_w |
| consolidated summary | cross-event narrative summary | reflective | persistent; append-only summary collection; no dedicated evi | src/lightmem/memory/utils.py:547 store_summary; lightmem.py: |
| sensory buffer (SenMem) | transient pre-segmentation buffer | working | transient in-memory; drained on segmentation, not persisted | src/lightmem/factory/memory_buffer/sensory_memory.py; lightm |
| short-term buffer (ShortMem) | transient pre-extraction buffer | working | transient in-memory; flushed on extraction trigger | src/lightmem/factory/memory_buffer/short_term_memory.py:36;  |
| update_queue (per-entry candidate list) | conflict-resolution bookkeeping | procedural | recomputed offline; overwritten each construct pass | src/lightmem/memory/lightmem.py:510-525 |
| BAM / BoundMem tags | scoping/isolation tags | shared | persistent per-entry attribute | src/lightmem/memory/utils.py:798 resolve_tags, 905 filter_by |
| FluxMem SemanticNode / EpisodicNode / ProceduralNode | evolving connectivity graph nodes (prototype) | associative(graph) + procedural | evolving: nodes expanded/pruned/reshaped, skills consolidate | src/fluxmem/graph/nodes.py:SemanticNode,EpisodicNode,Procedu |
| em2mem episodic/semantic/visual memory (multimodal) | EgoLife multimodal memory cells | episodic + semantic + associative(graph) | build-time offline graphs; queried during eval | src/em2mem/llm/templates/{memory_reasoning,semantic_extracti |

---

## powermem (9 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| fact_memory (memory row) | core atomic fact | semantic | UPDATE overwrites in place (same id); hard DELETE; Ebbinghau | core/memory.py:_create_memory L1593; storage/oceanbase/ocean |
| verbatim memory row (infer=False) | raw utterance | working | hard delete; Ebbinghaus annotation if plugin on | core/memory.py:_simple_add L1185 |
| Ebbinghaus tier (working / short_term / long_term) | lifecycle classification (not a separate store) | reflective | promote working->short->long on access; forget marker when r | intelligence/plugin.py:on_add L95; intelligence/ebbinghaus_a |
| procedural skill | reusable operation guide | procedural | merged on similarity>=0.03; status updatable (update_status) | intelligence/skill_manager.py:distill L30; storage/skill_sto |
| source record (provenance) | raw input / fact source | episodic | retained even with zero links; not projected as a second SoT | core/memory.py:_maybe_create_source L1154; storage/source_st |
| graph relation (entity + edge) | knowledge graph triple | associative(graph) | edge update via UPDATE_GRAPH_PROMPT; delete via DELETE_RELAT | storage/oceanbase/oceanbase_graph.py:add L645, _create_or_up |
| user profile (persona text) | consolidated user description | semantic | LLM merge-overwrite when changed=true; delete_user_profile | user_memory/user_memory.py L395; storage/user_profile*.py |
| user profile topics (structured) | structured persona key-values | semantic | merge-update; strict/extend mode; empty {} when nothing | prompts/user_profile_prompts.py:get_user_profile_topics_extr |
| agent-scoped / shared memory | multi-agent isolation & sharing | shared | governed by AgentMemoryConfig (mode multi_agent/multi_user/h | agent/agent.py; configs.py:AgentMemoryConfig L194; core/memo |

---

## MemoryBear (15 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Statement | semantic/episodic fact | episodic/semantic | created on write; valid/invalid_at supersession; ACT-R activ | steps/statement_temporal_step.py:StatementTemporalExtraction |
| ExtractedEntity | semantic entity | semantic | exact-merge on write; reflection cosine+LLM dedup/alias merg | steps/triplet_step.py; deduplication/deduped_and_disamb.py:a |
| Triplet / EntityEntityEdge (13-predicate) | associative graph relation | associative(graph) | created on write; redirected/deduped to canonical endpoints; | enums.py:TripletPredicate; models/graph_models.py:EntityEnti |
| Chunk | raw text unit | episodic | created on write; provenance for statements | data_preprocessing/data_chunker.py; models/graph_models.py:C |
| Dialogue | conversation container | episodic | created on write | models/graph_models.py:DialogueNode; enums.py:Neo4jNodeType. |
| MemorySummary | consolidated / forgotten summary | reflective/semantic (consolidation) | created during forgetting cycle from decayed nodes; retains  | forgetting_engine/forgetting_strategy.py:ForgettingStrategy; |
| Community | graph community | associative(graph) / reflective | incremental LPA on write; full clustering + merge in backgro | clustering_engine/label_propagation.py; enums.py:Neo4jNodeTy |
| Perceptual | multimodal perceptual memory | episodic (multimodal) | created on file ingest | models/graph_models.py:PerceptualNode/PerceptualEdge; contro |
| memory_messages (raw turn buffer) | ingest buffer | working (short-term buffer) | written on ingest; consumed by write pipeline; flush fallbac | memory_service.py:ingest_agent_message; pipelines/dispatcher |
| memory_short_term | cached retrieval result | working | upserted after DEEP/NORMAL read; skipped if empty/insufficie | pipelines/memory_read.py:_save_short_term; repositories/memo |
| working memory | working memory | working | session-scoped | controllers/memory_working_controller.py |
| user metadata (L0) | semantic user profile | semantic (profile) | extracted on write; used every read as L0 | steps/metadata_step.py:MetadataExtractionStep; models/metada |
| implicit memory (preference/interest/habit/dimension) | derived behavioral profile | reflective | recomputed by background analytics jobs | analytics/implicit_memory/analyzers/*; controllers/implicit_ |
| emotion / affective memory | affective memory | reflective/affective (no memspine equivalent) | extracted on write (config-gated); implicit emotion storage  | steps/emotion_step.py; models/emotion_models.py; controllers |
| episodic memory (labelled) | episodic event | episodic | classified post-write | utils/prompt/prompt_utils.py:render_episodic_type_classifica |

---

## MemMachine (11 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| episodic | episodic | episodic | append; delete_episodes / delete_session; no decay | memmachine_common.api.MemoryType.Episodic; episodic_memory/e |
| short_term_memory | working | working | capacity eviction + summarize; clear on session delete | episodic_memory/short_term_memory/short_term_memory.py |
| semantic | semantic_profile | semantic | LLM add/delete; consolidation merge; delete_features | MemoryType.Semantic; semantic_memory/semantic_model.py Seman |
| semantic_category_profile | semantic_profile | semantic | same as semantic + consolidation | server/prompt/profile_prompt.py |
| semantic_category_coding_style | procedural_style | procedural | same as semantic | server/prompt/coding_style_prompt.py |
| semantic_category_writing_assistant | procedural_style | procedural | update + consolidation prompts | server/prompt/writing_assistant_prompt.py |
| semantic_category_financial | domain_profile | semantic | unified update + consolidation | server/prompt/financial_analyst_prompt.py |
| semantic_category_health | domain_profile | semantic | create/update/rewrite prompts | server/prompt/health_assistant_prompt.py |
| semantic_category_crm | domain_profile | semantic | update + consolidation | server/prompt/crm_prompt.py |
| ltm_derivative | episodic_index | episodic | created with episode; deleted with episode cascade | declarative_memory/declarative_memory.py |
| event_segment | episodic_index | episodic | tied to episode uid mapping uuid5 | episodic_memory/event_memory/event_memory.py; LongTermMemory |

---

## langmem (6 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| semantic memory (facts / preferences / user profile) | long-term, schema-driven | semantic | created/updated/deleted in place via LLM patches; no version | knowledge/tools.py:create_manage_memory_tool:298; knowledge/ |
| episodic memory (events / thread summaries) | long-term, schema-driven | episodic | same trustcall insert/update/delete; no time-decay | knowledge/extraction.py:_MEMORY_INSTRUCTIONS:185, create_thr |
| procedural memory (reasoning procedures) | long-term, schema-driven | procedural | trustcall insert/update/delete in place | knowledge/extraction.py:_MEMORY_INSTRUCTIONS:185 ('semantic, |
| prompt memory (optimized system prompts) | reflective / procedural, distinct subsystem | reflective | overwritten when update_prompt/warrants_adjustment True; min | prompts/optimization.py:create_prompt_optimizer:50; prompts/ |
| working / short-term memory (running summary) | in-thread, ephemeral (checkpointer state) | working | extended each time token threshold crossed; bounded by max_t | short_term/summarization.py:RunningSummary:52, summarize_mes |
| default / profile memory (single evolving document) | long-term, singleton | semantic | created once from default_factory, then trustcall-updated; n | knowledge/extraction.py:MemoryStoreManager.ainvoke:1040-1063 |

---

## A-mem (3 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| MemoryNote | atomic note (Zettelkasten unit) | semantic (episodic-flavoured: notes carry timestamp/last_accessed but no episode grouping) | created on add_note; mutated in place by update() and by evo | memory_system.py:MemoryNote (line 24), add_note (233), updat |
| link (evolution edge) | associative relation | associative (graph) | appended during evolution; never pruned; dangling on delete  | memory_system.py:process_memory strengthen (line 679-683), s |
| evolution_history | provenance/audit field | reflective (intended) — not realized | declared but never populated in code (no append site found)  | memory_system.py:MemoryNote line 81 (init only); no write si |

---

## EverMemOS (12 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Episode | episodic | episodic | append-only md; superseded via deprecated_by on reflection;  | memory/extract/pipeline/user_memory.py:UserMemoryPipeline.ru |
| AtomicFact | semantic | semantic | append-only md; deprecated on reflection re-extract; index r | memory/strategies/extract_atomic_facts.py; infra/persistence |
| Foresight | prospective | prospective | append-only md; index rebuildable | memory/strategies/extract_foresight.py; infra/persistence/la |
| UserProfile | semantic | semantic | read-merge-overwrite (in-place update, not append); supersed | memory/strategies/extract_user_profile.py:_persist_profile;  |
| AgentCase | procedural | procedural | append-only md; clustered by geometry into skills; index reb | memory/strategies/extract_agent_case.py; infra/persistence/l |
| AgentSkill | procedural | procedural | clustered-then-extracted; re-synthesised as clusters grow; i | memory/strategies/extract_agent_skill.py; memory/search/skil |
| KnowledgeTopic | semantic-shared | shared | created on doc ingest; removed on document delete via cascad | entrypoints/api/routes/knowledge.py:259; infra/persistence/l |
| KnowledgeDocument | shared | shared | DELETE /documents/{doc_id} removes dir + cascade drops rows | memory/cascade/handlers/knowledge_document.py; infra/persist |
| MemCell | working | working | durable SQLite archive keyed by memcell_id; referenced by ep | service/_boundary.py:_build_memcell_row; infra/persistence/s |
| UnprocessedBuffer | working | working | ephemeral; drained when a boundary cut consumes it; tail rol | service/_boundary.py:_replace_buffer / _row_to_canonical; in |
| Cluster | associative | associative(graph) | size-1 clusters merged in; superseded members marked reflect | memory/strategies/trigger_profile_clustering.py; infra/persi |
| ReflectionReport | reflective | reflective | written per cron reflection run; gates re-reflection | memory/reflection/orchestrator.py; infra/persistence/sqlite/ |

---

## hindsight (14 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| world | raw fact | semantic | mutable; hard-deletable; feeds consolidation into observatio | models.py:81 (memory_units), models.py:124 (CHECK fact_type) |
| experience | raw fact | episodic | mutable; hard-deletable; feeds consolidation | retain/fact_extraction.py:1088-1095; models.py:124 |
| observation | consolidated knowledge | reflective | created/updated/deleted by consolidation; re-embedded on upd | consolidation/consolidator.py:8-15, _execute_create_action:9 |
| opinion (deprecated) | raw fact | reflective | dead — migrations g2h3i4j5k6l7 / i4d5e6f7g8h9 removed opinio | models.py:124, alembic/versions/g2h3i4j5k6l7_remove_opinion_ |
| mental_model | pinned reflection | reflective | refreshed on demand / post-consolidation by re-running sourc | memory_engine.py:refresh_mental_model:6467, consolidation/co |
| directive | behavioral rule | procedural | persistent config; CRUD via API; hard-deletable | engine/directives/models.py, reflect/prompts.py:33/65, refle |
| entity | graph node | associative(graph) | resolved/merged on retain; stats updated post-txn; cascade-d | models.py:178/216/238, entity_resolver.py:170/307 |
| memory_link | graph edge | associative(graph) | created on retain; ON CONFLICT DO NOTHING idempotent; cascad | models.py:264, retain/link_utils.py:521/394, search/graph_re |
| chunk | source substrate | working | created on retain; FK cascade-deleted with document | retain/chunk_storage.py, migration b7c4d8e9f1a2_add_chunks_t |
| document | source substrate | episodic | created/updated on retain; hard-deletable (cascades units/ch | models.py:59, retain/fact_storage.py, memory_engine.py:delet |
| bank | scope / persona config | shared | persistent; hard-deletable (cascades all bank data) | models.py:307, retain/bank_utils.py:_llm_merge_mission:236,  |
| async_operation | durable task | prospective | pending->processing->completed/failed/cancelled; deleted on  | worker/poller.py:186-250, memory_engine.py:execute_task:987, |
| webhook | integration | shared | persistent config; deliveries retried by worker | webhooks/manager.py, orchestrator.py:508, migration e4f5a6b7 |
| audit_log / tenant guard | firewall / trust | firewall/trust | enforced per request; no quarantine/anomaly store | memory_engine.py:validate_sql_schema:103/_PROTECTED_TABLES:7 |

---

## SimpleMem (8 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| MemoryEntry (lossless_restatement fact) | core semantic fact | semantic | write-once; no update/dedup/decay/delete in core (only bulk  | models/memory_entry.py:MemoryEntry; database/vector_store.py |
| MemoryEntry with timestamp/location (temporal/episodic facet) | episodic facet of the same MemoryEntry | episodic | write-once | database/vector_store.py:structured_search; core/hybrid_retr |
| SessionEvent (Cross) | raw session event | working | buffered then persisted; source for observations | cross/types.py:SessionEvent (:80); cross/orchestrator.py:rec |
| CrossObservation (Cross) | distilled session observation | procedural | created at session stop; retained in SQLite | cross/types.py:CrossObservation (:92); cross/collectors.py:e |
| SessionSummary (Cross) | session-level summary | reflective | one per finalized session | cross/types.py:SessionSummary (:108); cross/session_manager. |
| CrossMemoryEntry (Cross, bitemporal) | cross-session long-term memory | semantic | consolidation worker decays importance, merges near-dupes (s | cross/types.py:CrossMemoryEntry (:133); cross/consolidation. |
| RetrievalMemoryEntry (memory-entry with entry_type retrieval reference) | context-bundle reference | working | ephemeral (per context build) | cross/types.py:(:126-129 memory_entry_id/source_kind/score); |
| MemoryUnit / MemoryType enum (EvolveMem) | typed memory (declared, classification unconfirmed) | semantic | has superseded_by + expires_at fields (lifecycle scaffolding | EvolveMem/evolvemem/models.py:MemoryType (:12); extractor.py |

---

## Memori (6 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| entity_fact | semantic fact about the entity/user | semantic | Upsert-reinforced (num_times++/recency); hard-deletable via  | memori/storage/drivers/sqlite/_driver.py:EntityFact.create;  |
| knowledge_graph_triple | associative graph (subject-predicate-object) | associative(graph) | Upsert-reinforced join row; hard-deletable via knowledge_gra | memori/storage/drivers/sqlite/_driver.py:KnowledgeGraph.crea |
| process_attribute | procedural / agent-capability | procedural | Upsert-reinforced; cascade-deleted with process; no decay | memori/storage/drivers/sqlite/_driver.py:ProcessAttribute.cr |
| conversation_summary | episodic / reflective summary | episodic | Overwritten per turn (mutable single field); cascade-deleted | memori/memory/augmentation/augmentations/memori/_augmentatio |
| conversation_message | working memory / raw history | working | Append-only within a session; cascade-deleted with conversat | memori/memory/_writer.py:Writer._execute_transaction; memori |
| entity_fact_mention | provenance / evidence link | associative(graph) | Insert-once per (entity,fact,conversation); cascade-deleted  | memori/storage/drivers/sqlite/_driver.py:EntityFact.create ( |

---

## memU (13 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Resource (Index layer) | raw source / provenance | episodic (raw event source) / working | hard-deleted on clear_memory / workspace cascade when source | database/models.py:Resource ; app/memorize.py:_create_resour |
| RecallEntry:profile | semantic (user facts) | semantic | reinforced on exact-hash re-ingest (opt-in); hard-deleted; p | prompts/memory_type/profile.py ; database/models.py:RecallEn |
| RecallEntry:event | episodic | episodic | same as profile; happened_at field exists but is never popul | prompts/memory_type/event.py ; DEFAULT_MEMORY_TYPES=['profil |
| RecallEntry:knowledge | semantic (world knowledge) | semantic | hard delete; reinforcement opt-in | prompts/memory_type/knowledge.py ; prompts/memory_type/__ini |
| RecallEntry:behavior | procedural (user routines) | procedural | hard delete | prompts/memory_type/behavior.py |
| RecallEntry:skill (entry-plane) | procedural (skill profile) | procedural / reflective | hard delete; dormant by default | prompts/memory_type/skill.py (entry plane, off by default) |
| RecallEntry:tool | procedural (tool-use memory) | procedural | hard delete; tool_calls dedup via md5 call_hash | prompts/memory_type/tool.py ; database/models.py:ToolCallRes |
| RecallFile track='memory' (category / topic file) | semantic aggregate (Memory layer) | semantic (consolidated) / reflective | content re-synthesized/patched on every touch; hard-deleted  | database/models.py:RecallFile ; memorize.py:_update_file_sum |
| RecallFile track='skill' (skill file) | procedural / self-evolving (Skill layer) | procedural (self-evolving skills) | updated/created per source; segments re-sliced; hard delete | memorize_workspace.py:_TRACK_TO_FILE_TRACK ; prompts/memory_ |
| RecallFileSegment | search index slice (L2 item) | semantic (retrieval projection) | reconciled on every file re-sync; deleted with owning file ( | database/models.py:RecallFileSegment ; memorize_workspace.py |
| RecallFileEntry (item↔category link) | associative edge | associative(graph) | unlinked before item delete; cleared first in clear_memory c | database/models.py:RecallFileEntry ; crud.py:_reconcile_upda |
| RecallFileResource (resource↔file provenance) | associative / provenance edge | associative(graph) / provenance | unlinked on cascade delete of a resource | database/models.py:RecallFileResource ; retrieve_workspace.p |
| Exported Markdown memory FS (INDEX.md / MEMORY.md / SKILL.md + memory/*.md + skill/*.md) | projection / artifact (not a SoT) | n/a (rebuildable projector) | rebuilt incrementally or fully on workspace sync (force_full | memory_fs/exporter.py:MemoryFileExporter ; memory_fs/synthes |

---

## Second-Me (14 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| raw_file_memory | episodic_raw | episodic | active\|deleted soft status; hard delete via API | models/memory.py:Memory |
| document_l0 | working_content | working | updated on reprocess; tied to Memory | L0/models.py:InsighterInput; document_service.py |
| chunk | lexical_unit | semantic | regenerated on chunk/embed steps | file_data/dto/chunk_dto.py; embedding_service.py |
| document_vector | projector | semantic | wipe on dimension mismatch | embedding_service.py:document_collection |
| l1_cluster | associative | associative | versioned per L1 generation | models/l1.py:L1Cluster; topics_generator.py |
| shade | semantic_identity | semantic | improve on new memories; merge similar shades | L1/bio.py:ShadeInfo; L1/prompt.py:SHADE_INITIAL_PROMPT |
| global_bio | profile | semantic | regenerated each L1 biography step | kernel/l1/l1_manager.py; L1/prompt.py:GLOBAL_BIO_SYSTEM_PROM |
| status_bio | episodic_status | episodic | regenerated from time-bucketed memories | L1/bio.py:TimeType; models/status_biography.py |
| chunk_topic | index | semantic | per L1 extract_dimensional_topics | L1/prompt.py:TOPICS_TEMPLATE_SYS |
| graphrag_entity_graph | graph | associative | rebuilt on MAP_ENTITY_NETWORK train step | L2/data_pipeline/graphrag_indexing/prompts/extract_graph.txt |
| l2_training_corpus | procedural_synth | procedural | regenerated each train data prep | ProcessStep DECODE_PREFERENCE_PATTERNS..AUGMENT_CONTENT_RETE |
| l2_weights | parametric_memory | procedural | replaced on retrain; no selective forget | L2/README.md; training_prompt.py MEMORY_PROMPT |
| role_persona | prospective_persona | prospective | CRUD via role API | api/domains/kernel2/dto/role_dto.py |
| space_message | shared | shared | delete_space | api/domains/space/space_service.py |

---

## memobase (6 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| GeneralBlob (raw chat/doc blob) | raw ingest | working | hard-deleted after successful flush unless CONFIG.persistent | models/database.py:GeneralBlob; controllers/buffer.py:flush_ |
| BufferZone (pending-process pointer) | staging / working buffer | working | idle -> processing -> done/failed; deleted with blob (cascad | models/database.py:BufferZone; controllers/buffer.py:detect_ |
| UserProfile (topic/sub_topic memo slot) | semantic user profile | semantic | mutable; LLM APPEND/UPDATE/ABORT; consolidated (organize) an | models/database.py:UserProfile; controllers/modal/chat/merge |
| UserEvent (session event) | episodic timeline | episodic | append-only; delete_user_event; retention = created_at read- | models/database.py:UserEvent; controllers/event.py:append_us |
| UserEventGist (single-line event fact) | episodic / searchable fact | episodic | append-only; cascade-deleted with parent event/user; retenti | models/database.py:UserEventGist; controllers/event_gist.py: |
| UserStatus (roleplay plot status) | roleplay state / prospective plan | prospective | append-only status log; request-triggered (roleplay feature) | models/database.py:UserStatus; controllers/modal/roleplay/pr |

---

## telemem (7 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| events memory (shared conversation summaries) | episodic/shared | episodic (+ shared) | immutable ADD-only; deletion only via inherited mem0 delete/ | telemem/mem0.py:319-323, 489-490, 646-653; utils.py:get_rece |
| character/person memory (isolated per-user profile) | semantic/episodic per persona | semantic (persona-scoped) | immutable ADD-only in custom pipeline; mem0 update/delete vi | telemem/mem0.py:315-317, 491-492; utils.py:get_person_prompt |
| raw memory (infer=False verbatim) | working/raw | working | immutable ADD-only; mem0 update/delete | telemem/mem0.py:337-338, 344-360 |
| procedural memory | procedural | procedural | mem0-managed | telemem/mem0.py:333-335 |
| memory history / change log | audit/lifecycle | firewall/trust (closest: audit) - no true firewall exists | append-only audit; not a rebuildable projector | config/config.yaml:22 history_db_path; telemem/mcp/server.py |
| video clip caption memory | multimodal/episodic | episodic (multimodal) | built once per video; skip-if-exists; no update/decay | telemem/mm_utils/frame_caption.py:CAPTION_PROMPT; build_data |
| subject registry (video character index) | semantic/entity | associative (entity index; flat, not a graph) | built once per video during ingest | telemem/mm_utils/frame_caption.py:merge_subject_registries;  |

---

## memonto (4 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| ontology_schema_triples (T-Box) | schema / ontology | semantic (schema layer) | Persists until forget() drops the graph; mutable via auto_ex | memonto/memonto.py:Memonto.ontology; memonto/core/retain.py: |
| extracted_data_triples (A-Box) | semantic fact | semantic | Persists; updated via delete-then-recommit (auto_update); re | memonto/core/retain.py:save_memory; memonto/utils/rdf.py:hyd |
| triple_vector_index | projector / embedding index | semantic (index/associative-graph adjunct) | Rebuildable from triples; delete_collection on forget; delet | memonto/stores/vector/chroma.py:save/search/delete |
| ephemeral_in_memory_graph | working memory (mode-specific) | working | Lost on process exit; forget() clears with data.remove((None | memonto/memonto.py:Memonto.data; memonto/core/recall.py:get_ |

---

## memory-opensource (12 types)

| type_or_kind | category | ~memspine | lifecycle | evidence |
|--------------|----------|-----------|-----------|----------|
| Memory (canonical item) | core record | semantic | created on add; overwritten on update; hard-deleted across a | services/memory_management.py:store_memory_item(792); memory |
| Memory content type: text \| code_snippet \| document \| image | modality of core record | semantic | same as Memory | services/memory_management.py:2000 type=metadata.get('type', |
| Chunk embedding | vector projection | semantic | rebuildable projection of Memory; deleted with parent | memory/memory_graph.py:add_memory_item_without_relationships |
| Graph node (entity) + relationship | graph projection | associative(graph) | created in add-time background task; updated in place; delet | api_handlers/chat_gpt_completion.py:generate_node_ids(3981); |
| Memory category (metadata): preference \| task \| goal \| fact \| context \| skills \| learning | classification tag | episodic | set at ingest; immutable unless memory updated | services/message_analysis.py:68 (category definitions); serv |
| Goal / Use-case / Step | intent graph node | prospective | created/reused on add; referenced during retrieval classific | api_handlers/chat_gpt_completion.py:generate_usecase_memory_ |
| Conversation summary (short_term / medium_term / long_term) | consolidated summary | reflective | regenerated/updated each batch; medium synthesizes prev+shor | services/message_batch_analysis.py:578 (SUMMARY GENERATION), |
| Learning signal (user-preference / agent-performance / failed-approach) | meta-learning record | reflective | detected at batch analysis; persisted as memory-worthy conte | services/message_batch_analysis.py:USER_PREFERENCE_LEARNING_ |
| Reinforcement counters (cacheHitEma30d / citationHitEma30d + CW EMAs) | usage statistic | working | decayed lazily (half-life 30d) and incremented on each retri | services/memory_management.py:_apply_time_decay(3177), updat |
| OMO safety annotations (consent / risk / ACL / audit trail) | governance / firewall | firewall/trust | set at graph-write; consent=none prevents node creation enti | services/omo_safety.py:process_memory_with_omo(240), enforce |
| User graph schema (custom ontology) | schema / configuration | procedural | developer-managed; versioned per workspace/namespace | services/schema_service.py; services/llm_schema_selector.py: |
| Query / retrieval / prediction log | telemetry / feedback | episodic | appended per query/retrieval event | services/memory_management.py:store_memory_query_log_async(6 |

