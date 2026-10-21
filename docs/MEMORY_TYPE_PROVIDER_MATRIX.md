# Memory type and provider matrix

Status reconciled 2026-10-20 against PROVIDER_TODO.md; existing local gates are not proof of the new BI enrichment pipeline.

This is the PA-074 map of the nine built-in memory types to the provider seams
they exercise. It distinguishes a type's own operations from services that
`Engine.start()` currently constructs for the application as a whole. The
memory-type registry and its dependency rules are in
[`memspine.memory.registry`](../src/memspine/memory/registry.py) and
[`memspine.core.registry`](../src/memspine/core/registry.py).

## Common Engine path

Every record type travels through the Engine write door and the selected SQL
storage/event-log projection. SQL therefore supplies the common persistence
and record lookup path, not a distinct database requirement of each memory
class. On startup, [`Engine.start()`](../src/memspine/api/engine.py) resolves
storage, cache, embedder and vector store, then builds the LLM router. Lexical
storage is only constructed when `read.hybrid` is enabled; the graph store is
only constructed when associative memory is enabled. The memory objects are
then created conditionally from the enabled-type set. This global boot shape
does **not** mean every memory intrinsically needs every service.

The Engine's vector and lexical projectors index ordinary records for the
shared search APIs; lexical indexing is the optional hybrid leg. The shared
KV cache fronts the embedder and, for configured LLM extraction, the extractor.
The runner is selected independently as `inline`, `temporal`, or `taskiq` by
[`build_runner()`](../src/memspine/runtime/container.py). Background execution
is not a per-type storage dependency. The default prospective watch API is
pull-based, and shared subscriptions likewise have no push delivery today.

## Type requirements and proposed coverage

| Memory type | Implemented operations and intrinsic provider needs | Conditional paths | Proposed acceptance item |
|---|---|---|---|
| **Semantic** | [`SemanticMemory.write()`](../src/memspine/memories/semantic/store.py) uses SQL-backed records/events and an `EmbeddingService` for deduplication. Engine projection and normal semantic search use the selected vector store; the type itself does not own a separate lexical index. | Entity extraction is `off`, `llm`, or `gliner`: [`build_extractor()`](../src/memspine/runtime/container.py) selects the LLM role or [`GlinerEntityExtractor`](../src/memspine/memories/semantic/entities.py). GLiNER loads on extractor construction; shared lifecycle/configuration is recorded DONE under PA-118; see GLINER_SHARED_RUNTIME.md. The graph-style write pipeline is separately opt-in. Cache fronts the embedder and the LLM extractor. | **PA-075 — DONE (selected local gate):** preserve source grounding, deduplication, contradiction handling and historical validity across selected backend combinations. |
| **Working** | [`WorkingMemory.enforce()`](../src/memspine/memories/working/manager.py) emits page-out transition events through the write door; storage keeps the resulting record, transitioned to episodic. It has no intrinsic vector, lexical, graph or model operation. | The common Engine embedding/vector/search path still exists at boot; page-out itself is SQL/event work. | **PA-076 — DONE (selected local gate):** verify overflow boundaries and persona isolation. |
| **Episodic** | [`EpisodicMemory.timeline()` and `sessions()`](../src/memspine/memories/episodic/store.py) read records from storage and order/group them by event time. No model, graph or cache is intrinsic to these operations. | General `Engine.search()` may use vector and, when hybrid mode is on, lexical search; timeline and session reads do not. | **PA-077 — DONE (selected local gate):** verify chronology, historical ingestion and session/conversation links. |
| **Resource** | [`ResourceMemory.ingest()`](../src/memspine/memories/resource/store.py) extracts/chunks text, preserves document/chunk provenance, assesses chunks through the Engine gate and appends writes. SQL stores the canonical chunks; general retrieval uses vector and optionally lexical projection. | Plain text works in core; rich-file extraction uses optional `markitdown`, and chunking uses `chonkie` with a deterministic fallback ([`extraction.py`](../src/memspine/memories/resource/extraction.py)). No LLM or GLiNER is required to ingest. | **PA-078 — DONE (selected local gate):** verify chunk lineage, retrieval isolation and erasure through the source asset. |
| **Procedural** | [`ProceduralMemory`](../src/memspine/memories/procedural/skills.py) stores skills, plans, prompts, lessons and mappings as staged SQL records; promotion/deprecation are write-door events. [`knn_vote()`](../src/memspine/memories/procedural/knn.py) is local BM25 with an optional dense score. | `Engine.recall_plan()` compares task embeddings; lesson/advisory recall can use embeddings. Cache/model execution may support callers, but is not required to persist or promote a procedural record. | **PA-079 — DONE (selected local gate):** verify promotion gates and plan-cache scope/validity. |
| **Reflective** | [`ReflectiveMemory.reflect()`](../src/memspine/memories/reflective/reflections.py) loads source parents from SQL, checks namespace and depth, carries provenance, applies trust caps and appends the reflection. The class accepts caller-provided text; it has no intrinsic generation model. | The Engine may inject an assessment/firewall callback; model-produced reflection text is an upstream caller concern. Ordinary search can use vector and optional lexical legs. | **PA-080 — DONE (selected local gate):** verify provenance, depth, parent trust and tenant restrictions on all retrieval paths. |
| **Associative** | [`AssociativeMemory`](../src/memspine/memories/associative/store.py) requires SQL record lookup plus a `GraphStore` for links, traversal and PPR/BFS. The core dependency rule also requires semantic memory. | The `rrf` strategy optionally blends graph order with the selected vector store and embedder; without them it falls back to PPR. Entity resolution may enhance matching with embeddings or an LLM. | **PA-081 — DONE (selected local gate):** verify graph direction, time filters, namespace and entity ownership across graph providers. |
| **Prospective** | [`ProspectiveMemory.pending()` and `acknowledge()`](../src/memspine/memories/prospective/watches.py) use SQL records and conflict events; due-time checks receive an explicit `now`. The core dependency rule requires semantic memory, but watch evaluation itself makes no model call. | Current watch delivery is pull-based; a worker/broker is not intrinsic. Subscriptions and any external scheduling remain caller/runtime concerns. | **PA-082 — DEPLOYMENT_CHECK:** verify no duplicate watch work and no firing across forbidden scopes under worker execution. |
| **Shared** | [`SharedMemory.grant()` and `revoke()`](../src/memspine/memories/shared/grants.py) store grants as SQL records/events. The core dependency rule requires either semantic or episodic memory. | [`Engine.shared_search()`](../src/memspine/api/engine.py) searches own and granted namespaces through the ordinary retrieval path, so vector and optional lexical providers matter to search behavior; grant bookkeeping itself does not require them. Revocation must also be checked against cached/projected visibility. | **PA-083 — DONE (selected local gate):** verify grants, revocation and visibility across lexical, vector, graph and cache paths. |

## Current implementation versus proposed coverage

The source contains all nine memory implementations and the common provider
bindings described above. Existing live contracts cover selected end-to-end
Engine paths and provider-specific operations—for example
[`test_provider_live_contracts.py`](../tests/integration/providers/test_provider_live_contracts.py),
[`test_remote_search_live_contracts.py`](../tests/integration/providers/test_remote_search_live_contracts.py)
and [`test_pgvector_contract.py`](../tests/integration/providers/test_pgvector_contract.py).
Those cases verify particular configurations; they are not a Cartesian test
of every memory type against every storage, vector, lexical, graph, model,
cache and worker combination.

PA-075 through PA-081 and PA-083 are recorded DONE for selected local gates in
PROVIDER_TODO.md; PA-082 remains DEPLOYMENT_CHECK. Historical proposed wording is
superseded by those recorded outcomes. This does not establish every backend
combination or production deployment. New contextual enrichment and BI-01..12
still require their own source-grounding, isolation, recovery and erasure checks.
Provider availability or a successful Engine boot alone is not that proof.
