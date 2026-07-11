# memspine — Ecosystem Memory Taxonomy (Pass #5)

**Status:** Pass #5  
**Date:** 2026-07-10  
**Machine export:** [exports/ECOSYSTEM_MEMORY_TYPES.csv](exports/ECOSYSTEM_MEMORY_TYPES.csv)  
**Sources:** `docs/survey/_staging/*/TAXONOMY.json` (24 engines)

## 0. How to read this

The survey records cognitive memories, transient buffers, graph atoms, product cascade objects, policies, projectors, and even declared-but-unused kinds. Inclusion means that an object participates in a repository's memory model; it does not imply that every row is a first-class cognitive type.

- `type_or_kind`: the peer's own name.
- `category`: a normalized role/family label.
- `purpose`: what the object is for.
- `write_path`: how it is created or updated.
- `stored_shape`: its persisted or in-process representation.
- `read_path`: how it is searched, recalled, assembled, or consumed.
- `lifecycle`: update, consolidation, decay, invalidation, archive, and deletion behavior.
- `approx_memspine_type`: nearest of memspine's **working, episodic, semantic, resource, procedural, reflective, associative, prospective, shared**, or an explicit partial/none/infrastructure mapping.
- `evidence`: implementation, schema, prompt, or design artifact supporting the row.

Every source entry appears below with the identifying fields, purpose, mapping, and evidence. The machine export additionally preserves every full write/store/read/lifecycle value.

## 1. Coverage summary

| Engine/source | # | Notable categories | Notes |
|---|---:|---|---|
| memspine | 9 | facts, window, timeline, chunks, skills, reflection, graph, watches, grants | Nine first-class types; public writes are firewall-gated. |
| A-mem | 3 | zettel note, links, audit | Semantic payload and evolving links share one note object. |
| cognee | 12 | documents, graph, temporal, rules, skills, retrieval modes | DataPoint/schema kinds are not uniformly cognitive types. |
| EverMemOS | 10 | cascade kinds, MemCell, buffer | Markdown/SQLite/LanceDB product cascade. |
| graphiti | 7 | episodes, entities, bitemporal edges, communities, sagas | Temporal KG. |
| hindsight | 10 | facts, observations, models, directives, links, banks | Explicit consolidation and curated reflection. |
| honcho | 4 | document levels | Explicit/deductive/inductive/contradiction tree. |
| langmem | 7 | collections, schemas, prompts, summaries, namespaces | Developer-defined LangGraph-store schemas. |
| LightMem | 11 | facts, buffers, summary; FluxMem nodes/edges | Includes 4 FluxMem rows and one graph stub. |
| mem0 | 8 | facts, raw turns, procedure, entity index, audit, context | Two enum labels are unused. |
| MemMachine | 11 | episodic LTM, STM, semantic/domain profiles, indexes | Distinct episodic and semantic services. |
| memobase | 9 | profiles, events, buffers, assembly, roleplay | Product-shaped profile/event cascade. |
| Memori | 9 | messages, facts, provenance, triples, scopes, cloud | Local and hosted paths differ. |
| MemOS | 12 | tree scopes, preference/activation/parameter cubes, tools, dream | Includes runtime and parametric state. |
| memonto | 7 | RDF schema/triples, reification, projector, view | RDF-first; one dead classifier. |
| MemoryBear | 15 | dialogue KG, summaries, annotations, analytics, activation | Rich Neo4j/ACT-R model. |
| memory-opensource | 11 | vectors, conversations, graph schema, policy, OMO, ACL | Limited-confidence broken-tree survey. |
| memU | 10 | resources, typed items, category summaries, references | Profile/event defaults plus opt-in kinds. |
| OpenMemory | 5 | HSG sectors | Retrieval/decay sectors. |
| powermem | 12 | lifecycle tiers, facts, graph, skills, profiles, optimizer | Tiers coexist with content kinds. |
| ReMe | 9 | filesystem, dream buckets, proactive topics, index, wikilinks | Markdown source of truth. |
| Second-Me | 14 | L0/L1/L2 identity, graph, weights, role, shared space | Includes training/parametric artifacts. |
| SimpleMem | 8 | entries/dialogue, cross-session, EvolveMem, OmniSimpleMem | Nested research implementations included. |
| telemem | 7 | events, characters, video, graph, frames | Multimodal episodic pipeline. |
| **Total** | **220** |  | All entries from all 24 source JSON files. |

## 2. Cross-engine category map

- **Semantic facts/profiles:** nearly universal—flat facts (mem0, LightMem, SimpleMem), profile slots/features (memobase, MemMachine, memU), graph facts/entities (graphiti, cognee, memonto), or derived documents (honcho, hindsight).
- **Episodic/raw:** episodes, messages, dialogue chunks, task cases, daily notes, files, and multimodal atomic units occur across most engines.
- **Working/buffers:** memspine's bounded window parallels EverMemOS's tail, LightMem's two buffers, MemMachine STM, langmem running summaries, MemOS activation state, and several ingest/session caches.
- **Resource:** memspine, cognee, EverMemOS, ReMe, MemoryBear, memU, and memory-opensource distinguish document/resource objects; many peers instead collapse documents into episodic rows.
- **Associative:** links, entities, graph edges, communities, wikilinks, category relations, and provenance edges are widespread.
- **Procedural:** skills/rules/prompts appear in cognee, EverMemOS, FluxMem, langmem, mem0, MemOS, memU, powermem, ReMe, and Second-Me.
- **Reflective:** explicit higher-order or consolidated units occur in memspine, hindsight, LightMem, OpenMemory, MemoryBear, MemOS, and SimpleMem-Cross; several peers expose only a summarization process.
- **Prospective:** memspine watches are the clearest due/invalidation primitive. EverMemOS foresight and ReMe interest topics are partial; Second-Me roles and memobase plot state are looser.
- **Shared:** memspine grants are live no-copy views. Second-Me stores shared messages; langmem namespaces, hindsight banks, and Memori attribution are partitions/scopes.
- **Non-types:** retrieval modes, audit logs, policy/ACL/interchange objects, vector projectors, caches, and dead declarations remain visible so they are not mistaken for cognitive types.

## 3. Per-engine taxonomy

Order is memspine first, then source repositories alphabetically. Nested engines retain the `engine` value from their source JSON.

### memspine (9)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `semantic` | semantic_fact | Keyed facts with dedup and bi-temporal conflict ladder. | semantic | `docs/FEATURES.md §1`; `SemanticMemory.write`; registry |
| `working` | working_window | Bounded session buffer and pinned persona; overflow pages out. | working | FEATURES §2; engine write path; `memories/working/` |
| `episodic` | episodic_timeline | Chronological stream, sessions, and consolidation source. | episodic | FEATURES §3; consolidate pipeline; `memories/episodic/` |
| `resource` | document_chunks | Firewall-gated ingested document chunks. | resource | FEATURES §4; `Engine.ingest`; `memories/resource/` |
| `procedural` | skills_plans | Staged skills and embedding-backed plan cache. | procedural | FEATURES §5; procedural memories/API |
| `reflective` | higher_order | Source-linked derived memories, depth ≤2, trust capped by parents. | reflective | FEATURES §6; reflections; guards |
| `associative` | link_graph | Event-projected links with PPR/BFS/RRF retrieval. | associative | FEATURES §7; associative store; PPR |
| `prospective` | watches | Due-time/invalidation watches pending until acknowledgement. | prospective | FEATURES §8; watches; `Engine.watch` |
| `shared` | cross_namespace_grants | Revocable live cross-namespace views without copying rows. | shared | FEATURES §9; grants; `shared_search` |

### A-mem (3)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `MemoryNote` | zettel_note | Content, keywords, context, tags, links, and evolution history. | associative + semantic payload | `MemoryNote`; architecture flows; ADR-015 |
| `links` | associative_edge | Directed neighbors for strengthen and expanded retrieval. | associative | `process_memory`; `search_agentic` |
| `evolution_history` | audit_trail | Reserved audit list not appended on surveyed hot path. | reflective (partial) | field definition; process path |

### cognee (12)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `Document (+ Pdf/Text/Image/…)` | resource | Classified source document. | resource | document tasks/types |
| `DocumentChunk` | chunk | Semantic segment for extraction/retrieval. | resource chunks / episodic units | `DocumentChunk.py` |
| `Entity` | graph_entity | Named KG entity with type link. | semantic | `Entity.py` |
| `EntityType` | graph_type | Entity class/ontology type. | semantic (type) | `EntityType.py` |
| `TextSummary` | summary | Per-chunk retrieval summary. | semantic consolidate output | summarize task |
| `Event / Timestamp / Interval` | temporal | Optional temporal-cognify facts. | episodic + partial validity | temporal tasks; `Event.py` |
| `Triplet` | edge_text | Embeddable relation text. | associative | `Triplet.py`; memify tasks |
| `Rule / RuleSet` | procedural | Coding-agent rules from memify. | procedural (partial) | coding-rule associations |
| `Feedback / session lessons` | reflective | QA feedback weights and distilled lessons. | reflective (partial) | feedback/session distillation |
| `Skill / Tool / SkillRun` | agentic | Agentic playbooks and tools. | procedural / none | `Skill.py`; agentic prompts |
| `TableRow / ColumnValue / DltRow` | structured_ingest | Deterministic SQL/DLT graph rows and FK edges. | resource | DLT FK task |
| `SearchType strategy (not a memory type)` | retrieval_mode | Seventeen retrieval modes. | n/a | `SearchType.py` |

### EverMemOS (10)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `episode` | cascade_kind | Conversation narrative per user sender. | episodic | cascade registry; prior subset export |
| `atomic_fact` | cascade_kind | Atomic sentence extracted from episode. | semantic | atomic-fact strategy; hierarchy |
| `foresight` | cascade_kind | Forward-looking user inference; no wired recaller. | prospective (partial) | foresight strategy |
| `agent_case` | cascade_kind | Agent trajectory case feeding skill clustering. | episodic (agent) | agent pipeline; kind registry |
| `agent_skill` | cascade_kind | Skill synthesized from clustered cases. | procedural | skill strategy |
| `user_profile` | cascade_kind | Synthesized throttled profile document. | working persona + semantic | profile strategy/recaller |
| `knowledge_document` | cascade_kind | Knowledge metadata/summary index. | resource (metadata) | kind registry; knowledge service |
| `knowledge_topic` | cascade_kind | Searchable topic under knowledge document. | resource/semantic | kind registry; knowledge service |
| `MemCell` | boundary_unit | Algorithm-cut conversation slice. | working buffer / episodic raw | boundary service |
| `unprocessed_buffer` | session_buffer | Tail awaiting boundary conditions. | working | boundary/search manager |

### graphiti (7)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `EpisodicNode` | episode | Source utterance/document anchoring graph extraction. | episodic | nodes; `add_episode` |
| `EntityNode` | entity | Deduplicated entity/concept with summary. | semantic | node extraction/dedup |
| `EntityEdge` | fact_edge | Primary bitemporal fact triple. | semantic + associative edge | edge model/resolution |
| `EpisodicEdge MENTIONS` | structural_edge | Episode-to-entity mention. | associative (structural) | episodic edge/process |
| `CommunityNode` | community | Rebuildable cluster summary. | associative / reorganize (partial) | community build |
| `SagaNode` | thread | Ordered multi-episode thread and summary. | episodic session / working (partial) | saga API/prompt |
| `IS_DUPLICATE_OF` | dedupe_edge | Duplicate-entity resolution edge. | semantic merge adjunct | duplicate filtering |

### hindsight (10)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `world` | fact | External/user-life fact. | semantic (+ episodic event) | recall types; extraction enum |
| `experience` | fact | Assistant/agent interaction fact. | episodic / reflective raw | extraction prompts/recall types |
| `observation` | consolidated | Durable synthesis with proofs and sources. | semantic (sleep consolidate) | consolidator; constraints |
| `opinion` | legacy | Deprecated fact filtered from recall. | reflective (partial) | recall filter; constraint |
| `mental_model` | curated | Refreshable user-defined topic model. | reflective | reflect prompts/MCP tools |
| `directive` | policy | Tag-scoped hard rule for reflection. | procedural / policy | reflect prompt/engine |
| `entity` | graph_node | Named entity linking facts. | associative (entity node) | link utils/entity prompts |
| `chunk` | source | Raw segment for re-grounding. | resource / episodic source | chunk/recall paths |
| `bank` | scope | Mission-scoped namespace. | shared/tenant scope | bank API/utils |
| `memory_link` | edge | Temporal/semantic/entity/causal edge. | associative LINK | link creation/MPFP |

### honcho (4)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `explicit` | document_level | Atomic fact directly extracted from messages. | semantic / episodic fragment | document levels; representation/deriver |
| `deductive` | document_level | Necessity/update linked to premises. | semantic inferred / reflective edge | deduction specialist/tool |
| `inductive` | document_level | Pattern/preference generalized from ≥2 sources. | procedural/preference / reflective | induction specialist |
| `contradiction` | document_level | Conflict requiring clarification. | conflict / firewall-adjacent | validation; document level |

### langmem (7)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `Memory (default schema)` | semantic_collection | Generic fact/episode/note/preference string. | semantic | extraction model/instructions |
| `Custom Pydantic schema` | semantic_profile_or_typed | Developer-defined structured document. | semantic | store manager/tools |
| `Episodic (doc convention)` | episodic | Experience/few-shot via generic collection. | episodic | conceptual guide |
| `Procedural (prompt)` | procedural_prompt | Optimized system behavior text. | procedural | prompt optimizer/stateful graph |
| `Thread summary (SummarizeThread)` | episodic_summary | One-shot title/summary. | episodic | extractor/schema |
| `RunningSummary (short-term)` | working_memory | Token-budget active-history summary. | working | short-term summarization |
| `Namespace-partitioned store item` | storage_partition | Templated tenant/topic partition. | shared partition / semantic | namespace template/manager |

### LightMem (11 source entries)

| Engine/type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| LightMem `MemoryEntry / flat fact` | semantic | Extracted speaker fact. | semantic | memory utils; `lightmem.py` |
| LightMem `event factual binding` | episodic-ish / semantic | Time/image-enriched event fact. | episodic content / semantic storage | factual-binding prompt |
| LightMem `event relational binding` | associative / social | Speaker relational/emotional dynamics. | associative | relational-binding prompt |
| LightMem `sensory buffer` | working | Token-capped pre-segmentation messages. | working | sensory buffer |
| LightMem `short-term segment buffer` | working | Segments awaiting extraction. | working | short-term buffer |
| LightMem `cross-event summary` | reflective / consolidated | Window narrative over facts/interactions. | reflective | summarize/store summary |
| FluxMem `SemanticNode` | semantic | Embedded factual chunk. | semantic | FluxMem node/agent |
| FluxMem `EpisodicNode` | episodic | Task observation/action trajectory. | episodic | FluxMem node/run task |
| FluxMem `ProceduralNode` | procedural | PEMS-refined skill from episodes. | procedural | stage 3/PEMS |
| FluxMem `GroundEdge / DistillEdge / StepLinkEdge` | associative / graph | Fact→episode→skill/activation connectivity. | associative | edges/stages 1–2 |
| LightMem `GraphMem (core)` | graph | Empty intended graph backend. | associative (intended) | `memory/graph.py` |

### mem0 (8)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `flat_factual_memory` | semantic_flat | Extracted personal/assistant fact. | semantic | vector-add path |
| `raw_message_memory` | episodic_raw | Raw message when `infer=False`. | episodic | infer-false branch |
| `procedural_memory` | procedural | Agent execution summary. | procedural | enum/procedural create |
| `entity_record` | associative_index | Entity vectors linking memory IDs. | associative | entity store/extractor |
| `history_event` | audit_log | ADD/UPDATE/DELETE audit, not rebuild SoT. | none | SQLite manager |
| `session_messages` | working_context | Last-k extraction context. | working | messages table/add path |
| `semantic_memory_enum` | declared_unused | Rejected enum label. | semantic (declared) | enum/validation |
| `episodic_memory_enum` | declared_unused | Rejected enum label. | episodic (declared) | enum |

### MemMachine (11)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `episodic` | episodic | Durable turns and graph/vector LTM. | episodic | API type; episodic service |
| `short_term_memory` | working | Recent turns plus rolling summary. | working | STM implementation |
| `semantic` | semantic_profile | Tag/feature/value profile facts. | semantic | semantic type/model |
| `semantic_category_profile` | semantic_profile | General user profile pack. | semantic | profile prompt |
| `semantic_category_coding_style` | procedural_style | Coding-style features. | procedural | coding prompt |
| `semantic_category_writing_assistant` | procedural_style | Writing-style features. | procedural | writing prompt |
| `semantic_category_financial` | domain_profile | Financial profile. | semantic | financial prompt |
| `semantic_category_health` | domain_profile | Health profile. | semantic | health prompt |
| `semantic_category_crm` | domain_profile | CRM profile. | semantic | CRM prompt |
| `ltm_derivative` | episodic_index | Embedded sentence linked to episode. | episodic | declarative memory |
| `event_segment` | episodic_index | Vector event segment hydrating episode. | episodic | event memory |

### memobase (9)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `user_profile_slot` | semantic_persona | Topic/subtopic user memo. | semantic + working persona | profile model/process |
| `user_event` | episodic_timeline | Session memo with tags/profile delta. | episodic | event controller/model |
| `event_gist` | episodic_search_unit | Searchable timeline bullet. | episodic | gist controller |
| `chat_blob` | ingest_buffer | Raw chat awaiting flush. | working transient | blob/buffer controllers |
| `summary_blob` | ingest_buffer | Pre-summarized ingest. | working transient | summary modal |
| `event_tag` | metadata | Emotion/goal event tags. | episodic metadata | tag prompt/path |
| `context_pack` | assembly | Prompt-ready per-request memory. | retrieve/assemble | context controller/prompt |
| `roleplay_plot_status` | procedural_optional | Proactive plot/topic continuity. | procedural / prospective loose | proactive topics |
| `voice_session_memory` | integration | Voice use of profile/event API. | working + semantic | LiveKit tutorial |

### memonto (7)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `ontology_schema` | schema | RDF/RDFS extraction constraints. | semantic schema | ontology/retain/Jena |
| `instance_triple` | knowledge_graph | Extracted RDF fact. | semantic | retain/retrieve/Jena |
| `reified_triple_id` | metadata | UUID reification for sync/delete. | associative edge identity | RDF/Jena hydration |
| `ephemeral_graph` | working | Storeless in-process RDF. | working | ephemeral/forget/recall |
| `vector_triple_doc` | projector | Triple string for contextual kNN. | semantic projector | Chroma/recall |
| `contextual_summary` | assembled_view | Ephemeral English RDF summary. | working assembled | recall prompt |
| `procedural_vs_factual_label` | dead_classification | Unwired type classifier. | procedural/semantic unused | bisect prompt/no calls |

### Memori (9)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `conversation_message` | episodic_raw | Captured chat turn. | working / episodic raw | writer/migration |
| `conversation_summary` | episodic_summary | Rolling summary joined through mentions. | episodic | augmentation/fact join |
| `entity_fact` | semantic_fact | Durable embedded entity fact. | semantic | entity/SQLite/search |
| `entity_fact_mention` | provenance_link | Fact-to-conversation occurrence. | associative bookkeeping | migration/insert |
| `semantic_triple` | knowledge_graph | Subject-predicate-object relation. | associative | parser/KG create |
| `process_attribute` | procedural_skill | Process/agent skill or preference. | procedural | process writes |
| `attribution_scope` | namespace | Entity/process/session scope. | namespace / shared partial | attribution/config |
| `agent_turn_trace` | agent_episodic | Hosted agent turn and execution trace. | episodic agent | capture turn |
| `cloud_managed_memory` | managed_bundle | Hosted facts/messages bundle. | semantic + episodic | cloud default/recall |

### memory-opensource (11, limited)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `_survey_meta` | limitation | Broken-tree survey marker, not a memory. | n/a | repo sync/dirty HEAD |
| `TextMemory` | vector_text | Embedded text with metadata/ACL. | episodic + semantic | memory graph/shared types |
| `Message` | conversation | Session message and summary input. | episodic + working | staged/recovered evidence |
| `MessageSession` | conversation_summary | Reused summary after ~15 messages. | working + reflective | route docstring/inventory |
| `DocumentChunk` | document_ingest | Vector/graph document chunk. | episodic | env/pyproject/inventory |
| `AgentLearning` | structured_learning | Auto-registered learning schema. | procedural + reflective | auto-schema doc |
| `GraphNode` | neo4j_schema_node | Policy-supplied/extracted typed entity. | semantic | API/DX/chat artifacts |
| `GraphRelationship` | neo4j_edge | Typed relation under schema. | associative graph | API/chat artifacts |
| `MemoryPolicy` | processing_policy | Schema, constraints, consent, risk, ACL. | policy | shared types/resolver |
| `OpenMemoryObject` | interchange_object | OMO consent/risk/topic/source/ACL object. | interchange | OMO model |
| `ACLConfig` | access_control | Read/write ACL to vector filters. | security policy | shared types/auth |

### MemoryBear (15)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `Dialogue` | container | Root conversation node. | episodic | orchestrator/model |
| `Chunk` | span | Dialogue extraction segment. | episodic | chunker/model |
| `Statement` | fact | Temporal/affective activated fact atom. | semantic | extractor/ontology/forget |
| `ExtractedEntity` | entity | Typed alias-aware graph entity. | associative | triplet/dedup/forget |
| `EntityEntityEdge` | relation | Predicate-constrained relation. | associative | ontology/orchestrator |
| `MemorySummary` | summary | Dialogue or forget-merge compression. | reflective | summary/forget/search |
| `TemporalValidity` | temporal | Statement valid/invalid interval. | episodic | temporal prompt/service |
| `EmotionAnnotation` | affect | Statement emotion metadata. | episodic | emotion service/prompt |
| `ImplicitPreference` | analytics | Derived preference tag. | semantic | preference prompt |
| `ImplicitInterest` | analytics | Derived interest tag. | semantic | interest prompt |
| `BehaviorHabit` | analytics | Ranked habits. | procedural | habit detector/prompt |
| `PersonalityDimension` | analytics | Personality score. | reflective | dimension prompt |
| `OntologyClass` | schema | OWL/RDF entity constraint. | resource | ontology parser/validator |
| `RAGChunk` | resource | Alternate KB chunk. | resource | RAG write/read |
| `ActivationState` | dynamics | ACT-R state driving rank/forget/merge. | working | history/calculator/rerank |

### MemOS (12)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `WorkingMemory` | tree_textual | FIFO short-horizon tree scope. | working | tree manager/item |
| `LongTermMemory` | tree_textual | Durable factual/episodic tree. | episodic + semantic | tree/reorganizer |
| `UserMemory` | tree_textual | Stable profile facts. | semantic | search/reorganizer |
| `OuterMemory` | tree_textual | External/internet memory. | semantic | item/retriever |
| `RawFileMemory` | tree_textual | File-chunk tree memory. | episodic | manager/reader |
| `PreferenceMemory / pref_mem cube` | preference_vector | Personalization preference. | semantic facet | preference memory |
| `act_mem (ActivationMemory)` | activation_kv | Runtime KV cache. | working runtime | chat/scheduler |
| `para_mem (ParameterMemory)` | parameter | Parameter/LoRA slot. | procedural loose | cube/schema |
| `ToolSchemaMemory / ToolTrajectoryMemory` | tool | Tool schema/trajectory. | procedural | tool prompts/search |
| `SkillMemory` | skill | Reusable skill/script. | procedural | skill prompt/extra |
| `Context` | dream_index | Dream-bound key/summary. | reflective | dream prompts/item |
| `general_text (non-tree)` | flat_textual | Simple vector text. | episodic | config/core add |

### memU (10)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `resource` | raw_mount | Source artifact plus caption embedding. | episodic raw / ingest blob | resource/memorize/architecture |
| `profile` | memory_item | Stable user trait/preference. | semantic profile | prompt/default/type |
| `event` | memory_item | Time-bound experience. | episodic | prompt/default |
| `knowledge` | memory_item | Objective fact/concept. | semantic | prompt/config |
| `behavior` | memory_item | Recurring pattern/routine. | procedural soft | prompt |
| `skill` | memory_item | Actionable skill profile. | procedural | prompt |
| `tool` | memory_item | Tool pattern and call history. | procedural / tool trace | prompt/model/utils/tests |
| `memory_category` | folder_summary | Topic summary plus embedding. | semantic cluster / working summary | defaults/update |
| `category_item` | relation_edge | Item↔category relation. | associative edge | model/architecture |
| `item_reference` | citation | Summary-to-item citation. | provenance link | reference utils/config |

### OpenMemory (5)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `episodic` | hsg_sector | Events/experiences/schedules. | episodic | constants/HSG config |
| `semantic` | hsg_sector | Facts/concepts/default sector. | semantic | constants/classifier |
| `procedural` | hsg_sector | How-to/tool instructions. | procedural | constants/HSG config |
| `emotional` | hsg_sector | Affect/preference intensity. | working partial / affect overlay | constants/scoring matrix |
| `reflective` | hsg_sector | Insights/auto-consolidated patterns. | reflective | constants/reflection |

### powermem (12)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `working` | lifecycle_tier | Low-importance fast-decay tier. | working | Ebbinghaus/plugin |
| `short_term` | lifecycle_tier | Mid-importance tier. | episodic | plugin/decay |
| `long_term` | lifecycle_tier | High-importance slow-decay tier. | semantic | plugin/config |
| `fact_memory` | extracted_fact | Extracted fact with conflict merge. | semantic | intelligent add/prompts |
| `raw_message_memory` | simple_store | Raw messages without inference. | episodic | simple add |
| `graph_entity` | graph_node | OceanBase entity. | associative | graph extraction |
| `graph_relation` | graph_edge | OceanBase relation triple. | associative | graph establish |
| `skill` | procedural | Distilled operation guide. | procedural | prompts/manager |
| `user_profile` | profile | Natural-language profile. | semantic | user memory/prompt |
| `user_profile_topics` | profile_structured | Hierarchical profile JSON. | semantic | topics prompt |
| `compressed_summary` | optimizer_artifact | Replacement for similar-memory cluster. | semantic | optimizer |
| `category_scope_tag` | annotation | Caller category/scope label. | opaque | simple-add handling |

### ReMe (9)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `daily_note` | filesystem_layer | Day/session episodic facts. | episodic | auto-memory/daily write |
| `digest_procedure` | dream_bucket | Provenanced how-to/runbook. | procedural | procedure bucket/prompt |
| `digest_personal` | dream_bucket | Preferences/identity/conventions. | semantic persona | personal prompt |
| `digest_wiki` | dream_bucket | General principles/knowledge. | semantic | wiki prompt |
| `resource_file` | filesystem_layer | Watched external asset. | resource | resource watcher |
| `interest_topic` | proactive_signal | Daily proactive interest. | prospective + reflective partial | topics/proactive job |
| `file_chunk` | index_projector | Rebuildable retrieval unit. | projector, not cognitive | local store/chunker |
| `wikilink_edge` | graph | Memory-file association. | associative | wikilink/link expansion |
| `session_transcript` | source_artifact | Sanitized provenance transcript. | working / episodic source | session path/sanitizer |

### Second-Me (14)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `raw_file_memory` | episodic_raw | Uploaded personal file. | episodic | memory model |
| `document_l0` | working_content | Extracted text/insight/summary. | working | L0/document service |
| `chunk` | lexical_unit | Embedded tagged segment. | semantic | DTO/embedding |
| `document_vector` | projector | ANN clustering input. | semantic projector | embedding service |
| `l1_cluster` | associative | Hierarchical document group. | associative | L1 cluster/topics |
| `shade` | semantic_identity | Interest-domain identity facet. | semantic | shade model/prompt |
| `global_bio` | profile | Profile synthesized from shades. | semantic | L1 manager/prompt |
| `status_bio` | episodic_status | Recent/earlier activity overview. | episodic | time/status models |
| `chunk_topic` | index | LLM topic/tags. | semantic | topics prompt |
| `graphrag_entity_graph` | graph | L2-prep entity graph. | associative | GraphRAG prompt |
| `l2_training_corpus` | procedural_synth | Alignment training examples. | procedural | L2 process steps |
| `l2_weights` | parametric_memory | Fine-tuned personal model. | procedural | L2 README/prompt |
| `role_persona` | prospective_persona | Named role system prompt. | prospective | role DTO/service |
| `space_message` | shared | Multi-agent space message. | shared | space service |

### SimpleMem (8 source entries)

| Engine/type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| SimpleMem `MemoryEntry` | semantic_fact | Lossless pronoun-free dialogue fact. | semantic | model/vector store/flows |
| SimpleMem `Dialogue` | episodic_raw | Pre-extraction raw-turn buffer. | episodic | model/builder |
| SimpleMem-Cross `SessionRecord` | session | Tenant/project session lifecycle. | working | cross types/SQLite |
| SimpleMem-Cross `SessionEvent` | event_log | Append-only session transcript. | episodic | cross schema/event kind |
| SimpleMem-Cross `CrossObservation` | observation | Typed session insight. | reflective | type/store |
| SimpleMem-Cross `CrossMemoryEntry` | long_term_vector | Scoped fact with validity/supersession. | semantic | type/consolidation/store |
| EvolveMem `MemoryUnit` | eval_memory | Unit for retrieval-policy evolution. | semantic | model/retriever/evolution |
| OmniSimpleMem `MAU` | multimodal_atomic | Multimodal atomic unit. | episodic | MAU/store |

### telemem (7)

| Type | Category | Purpose | Approx. type | Evidence |
|---|---|---|---|---|
| `events` | episodic_dialogue | Global-lane dialogue-turn summary. | episodic | extraction/config |
| `person_profile` | character_semantic | Character facts/relations/traits. | semantic | person prompt/batch |
| `video_clip_caption` | multimodal_episodic | Time-bounded video narration. | episodic | multimodal/caption/DB |
| `subject_registry` | multimodal_entity | Cross-clip cast/object identities. | associative | merge prompt/browse |
| `memory_graph_node_edge` | associative_graph | Offline event graph for path QA. | associative | graph build/query |
| `procedural_memory` | inherited_mem0 | Accepted inherited label, unused path. | procedural (unused) | signature/no branch |
| `raw_frame_jpeg` | multimodal_raw | VLM caption/inspect frame cache. | working | decode/inspect |

## 4. Implications

Most peers collapse memory into notes, entities, edges, conversation units, or product cascade kinds. memspine instead gives nine families distinct contracts and lifecycles over one event-sourced substrate.

- **Shared grants are unusual:** memspine provides revocable, trust-capped, no-copy live views; peers usually provide partitions, tenant scopes, ACLs, or directly shared messages.
- **Prospective watches are operational:** due/invalidation triggers and acknowledgement semantics go beyond peer foresight or proactive-topic outputs.
- **Firewall gating is type-wide:** writable cognitive families share trust, quarantine, and anomaly controls, while reflective writes also prevent trust laundering. Peer security is more often ACL/policy metadata around a flat store.

The breadth has a maintenance cost: type distinctions must remain behavioral rather than cosmetic. The peer alternative—note/entity/edge or cascade-kind simplicity—is often appropriate, but carries less explicit lifecycle and safety semantics.
