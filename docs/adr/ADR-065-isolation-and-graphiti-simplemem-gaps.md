# ADR-065: Per-user isolation (I1–I9) and the Graphiti / SimpleMem gaps (G-*, C2/C6/C7)

- **Status:** accepted (every key off or unchanged by default unless stated)
- **Date:** 2026-10-08
- **Decision id:** D-89.
- **Sources** (research repo):
  - `paper_spine/evaluation/ISOLATION_REVIEW_2026-10-08.md`;
  - `sota/S8_GRAPHITI_SIMPLEMEM_FEATURE_LIST_2026-10-08.md`;
  - `GRAPH_ENGINE_PLAN_2026-10-08.md`.

## Decision

### Isolation (namespace → session → message)

| Item | What |
|---|---|
| I1 | `vector.namespace_index`: a LanceDB BITMAP index on `namespace` |
| I2 | A per-read listing snapshot (always on; cleared by any write; same results) |
| I3 | An isolation sweep test |
| I4 | `vector.isolation: per_namespace`: one table per user; `erase_namespace` drops it |
| I5 | `Engine.namespace_stats` |
| I6 | Indexed `session_key` / `source_role` columns (migration 0005, backfilled) and `Engine.conversation()` |
| I7 / I8 | `sessions=` / `roles=` on search / assemble / read |
| I9 | `ledger_id`: the clearer name for the read-ledger `session_id` |

### Graphiti / SimpleMem gaps

| Item | Key or verb |
|---|---|
| G-1 | `extract_graph.close_ended` (edge `valid_to`) |
| G-2 | `extract_graph.contradictions` (cross-key, `invalidate_edge` role) |
| G-3 | `extract_graph.event_identity: dated` |
| G-4 | Entity rules in the edge prompts |
| G-5 / G-6 | `extract_graph.entity_types` / `relation_types` |
| G-7 | `entity_nodes.turn_mentions` |
| G-8 | `write(extraction_hint=)` |
| G-10 | `read.mmr_lambda` |
| G-11 | `focal_entity=` |
| G-12 | `memory_types=` / `tags_any=` |
| G-16 | `read.view_tag_leg` |
| G-17 | `read.completeness_rounds` |
| G-19 | `summarize@structured` |
| G-20 | `Engine.brief` |
| G-22 | `read.gist_after` |
| GR-14 | The `graph` template |
| GR-15 | `read.rerank_balanced` |

### Field context practices

| Item | Key |
|---|---|
| C2 | `read.recent_exchanges` |
| C6 | `read.leg_min_scores` |
| C7 | `read.section_captions` |

C1 (a follow-up rewrite) was built and then removed at the owner's request.

### Covered by existing mechanisms

| Plan item | Covered by |
|---|---|
| GR-2 typed entity table | `EntityVec` (ADR-064) |
| GR-4 / GR-7 / GR-8 fact embeddings and time | Facts are records: vector search, `valid_from` / `valid_to`, as-of reads |
| GR-10 embedding-ranked resolution | `resolution.py`, GP-7 |
| GR-11 / GR-12 searchable summaries | Summaries are records |

### G-23: guarded self-tuning (added 2026-10-09 at the owner's request)

`memspine_evals.spinetune` (SpineTune) is in the harness, not the engine. It differs from EvolveMem in five ways:
- it searches only existing keys;
- it splits by conversation into dev and held-out;
- it accepts a change only on a Bonferroni-corrected sign test plus a minimum gain;
- it can check a guard dataset for non-inferiority;
- no LLM proposer and no gold answers are involved.

Results are labelled "auto-tuned" and never replace the hand-built baseline. A default changes only through an ADR after rule U5.

### Deferred (low value for retrieval accuracy, or a new dependency)

| Item | Reason |
|---|---|
| G-13 server graph adapters | Operations, not accuracy |
| G-14 OpenTelemetry spans | Diagnosis only |
| G-15 MCP adapter | Integration; a new dependency needs its own decision |
| G-18 session value score | Low value |
| G-21 multimodal units | Out of the text-memory scope |
| Directed BFS | memspine's walk is undirected by design (GP-3) |

## Consequences

- With every new key at its default, reads are byte-identical. I2 changes speed only; the full unit suite passed with it.
- Every key is screened before any default changes (rule U5); QA claims need paid runs.
- Several items need an LLM role to have an effect: G-1, G-2, G-4, G-5 / G-6, G-8, G-17, G-19. Without the role they are no-ops.
