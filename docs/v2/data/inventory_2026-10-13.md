# What exists today for each review finding (code inventory at 924a3ed, 2026-10-13)

Classification of the functionality behind ER-01..ER-40 in `src/memspine/` and `evals/` on `feat/local-qwen-stack`.
"Default" means `config/schema.py` defaults (templates and presets may switch flags on). Line numbers are at 924a3ed.

**Counts:** built and on 0 · built-off 5 (ER-19, 23, 25, 27, 28) · partial 26 · missing 3 (ER-07, 11, 12) · plan-only 6 (ER-01..05, 40).

| ER | Class | Evidence | Gap |
|---|---|---|---|
| 01-05 | plan-only | docs/v2 (PLAN, packages.py, RESTRUCTURE, EXECUTION, ROADMAP) | Canonical decision generation, DAG prerequisites, change classes, ownership tooling and milestones are plan and tooling work |
| 06 | partial | engine.py:13479-13503 (event appended, projectors applied inline, offsets per projector); core/replay.py:30-50 | No receipt, per-capability readiness or bounded wait; window extraction runs from an in-memory deque (workers/windows.py:326) discarded on stop (568-569) |
| 07 | missing | no lease, epoch or fencing in src/; closest is `require_live=member_ids` (windows.py:868) | No job lease, claim epoch or conditional commit |
| 08 | partial | REST token bucket off by default (schema.py:1722, 1674-1680); unbounded deque (windows.py:326) | No storage quotas, rejection before acceptance or disk-full handling |
| 09 | partial | advance-only max offset per projector (services/storage/sql_base.py:203-215); in-order replay (replay.py:40-49) | No holes, contiguous watermark, source fingerprints or captured barrier target |
| 10 | partial | WindowPipeline.stop: 5 s grace then cancel, queued jobs abandoned (windows.py:86-87, 554-569); engine stop order (engine.py:1637-1647) | Unfinished jobs not persisted; no logical vs physical completion; no per-tenant fairness |
| 11 | missing | window prompt has no CONTEXT section (prompts/defaults/window_extract.yaml:70-73; windows.py:164-183) | No context lines, binding evidence or citation |
| 12 | missing | no coverage or recovery logic | No omission diagnostics or targeted re-extraction |
| 13 | partial | entity id `ent:{namespace}:{canonical}` excludes session (memories/associative/entities.py:64-66); record ids uuid4 (core/records.py:69-71) | No separate retry, mention, assertion or occurrence ids |
| 14 | partial | one `confidence` float (prompts/models.py:218), dropped at window deposit (windows.py:855-869); record has `trust` only (records.py:231) | No separate binding, time or modality certainty |
| 15 | partial | json-repair on (services/llm/structured.py:107-129); retry off by default (schema.py:225, 229); auto split (windows.py:628-639) | No budgets by purpose; processed ranges not tracked |
| 16 | partial | per-prompt token ledger, chars/4 fallback (observability/usage.py:26-72) | No complete-request benchmark or information-per-token metric |
| 17 | partial | corroboration skips same source signature (engine.py:11393-11402); integrity roots off (schema.py:1484-1501); echo guard `firewall.skip_injected_recall` off (schema.py:1447) | No source-family independence; echo guard off |
| 18 | partial | undated window fact takes window start (windows.py:854); YYYY-MM becomes day one (workers/pipelines.py:2912-2935) | Precision and anchor provenance lost; weak anchors become unlabelled event dates |
| 19 | built-off | read.list_mode (schema.py:885), read.event_ledger (1207), agentic slot (1182) | No deterministic evidence-obligation model |
| 20 | partial | graph stores binary links only (services/graph/ladybug.py:82-96) | No assertion nodes or n-ary role edges |
| 21 | partial | rerank and keep-cut in `_search` (engine.py:5749, 5812-5818), lift afterwards (6209-6212, 8357-8386); Jina tail scored 0.0 (services/rerank/jina_rerank.py:65-75) | Lift after rerank; no post-lift cap; unscored looks like scored zero |
| 22 | partial | search gates (engine.py:5219-5308); `_context_eligible` for parents and neighbours (9494-9525) | A fact with no live lineage keeps its slot (8379-8380); no final admission before render |
| 23 | built-off | read.relevance_gate store_calibrated (schema.py:1231-1253) | Off; no missing vs conflicting evidence states |
| 24 | partial | `estimate_tokens = len//4+1` (core/policies/assembly.py:50-51); stop at first overflow (283-309) | No exact tokenizer, headers/reserves uncounted, no complete bundles |
| 25 | built-off | read.external_evidence (schema.py:1300-1309), asset evidence (1294, 1788) | Off; OCR uncertainty separation unverified |
| 26 | partial | Engine.sleep flushes and runs inline (engine.py:13314-13334); scheduler without overlap (workers/scheduler.py:37-81) | No immutable job snapshot or coalesced follow-up target |
| 27 | built-off | event_log.mode FULL/ROLLING/EPHEMERAL (schema.py:43-50); `RebuildUnavailableError` (core/replay.py:53-71) | Retained source/lineage/tombstone contract not stated |
| 28 | built-off | profile headers and standing instructions (schema.py:647-822); forget cascade (engine.py:9757) | No changed-group invalidation or usefulness metric |
| 29 | partial | optional `RecordScope.viewer` (core/read_filters.py:285, 318); unmarked visible without participants (core/visibility.py:81-93) | Viewer not required on multi-party reads; not bound to an authenticated principal |
| 30 | partial | hard-forget cascade and cache purge (engine.py:9933-9985; core/erasure.py); window cache purge via in-process map (windows.py:600-606) | No deletion manifest, erasure generation or purge progress; map lost on restart |
| 31 | partial | embedding cache key `emb:{embedder_id}` (services/cache/semantic.py:55); extraction cache key without model id (semantic.py:125); no result/context cache | No generation in keys; extraction key omits model (result cache absent, consistent with "off") |
| 32 | partial | separate output models (prompts/models.py:214, 811-870) and record (core/records.py:174-260) | No explicit compiler or test that the model cannot set scope or trust |
| 33 | partial | `MemoryEvent.schema_version = 1` (core/events.py:122), no upcasters | Compatibility policy and fixtures are plan-only |
| 34 | partial | static registries (core/registry.py); no entry points in pyproject | No discovery, pinned manifest or duplicate-provider failure |
| 35 | partial | RunManifest (evals/memspine_evals/provenance.py); paired gate INCOMPLETE (paired_gate.py:122) | Gate does not compare manifests (no INCOMPARABLE) |
| 36 | partial | frozen hashed blind slices, aggregate-only scoring (evals/freeze_blind_slices.py, eval_blind.py) | No exposure ledger or sealed final partition |
| 37 | partial | failure buckets and evidence normaliser (evals/failure_buckets.py, memspine_evals/evidence.py) | No standard stage metrics; margins not enforced by the gate |
| 38 | partial | lexical gold lint with seeded-leak test (tests/unit/test_prompt_gold_lint.py) | No reviewed-provenance record or synthetic-example policy |
| 39 | partial | generative token ledger; rerank call counter (engine.py:1263, 5749); eval timing | Embedding, rerank and tool costs not per request; SLO conditions undeclared |
| 40 | plan-only | slot loop and fact chain still present, off (core/slot_loop.py, core/fact_chain.py) | Nothing retired; no retirement record |

Concrete defects found while inventorying (to fix in their packages): lift after rerank with unscored turns (ER-21, R3-08);
undated window facts silently dated to the window start (ER-18, W2-14); window jobs discarded on stop (ER-06, F-04).
