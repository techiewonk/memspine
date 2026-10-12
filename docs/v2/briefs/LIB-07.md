# Agent brief: LIB-07 Library: job leases, epochs, watermark and holes, handles, coalescing, quotas

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: job leases, epochs, watermark and holes, handles, coalescing, quotas. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- W-02 What a write receipt guarantees ([areas/write.md](areas/write.md#w-02-what-a-write-receipt-guarantees-d-raw-searchable-plus-derivation-intent))
- W-10 Event log to projection hand-off ([areas/write.md](areas/write.md#w-10-event-log-to-projection-hand-off-c-hot-inline-heavy-via-outbox-note-combine-b--c))
- W-13 Durable derivation queue and acknowledgement ([areas/write.md](areas/write.md#w-13-durable-derivation-queue-c-leased-outbox-at-least-once-idempotent-effects))
- W-25 Backpressure when derivation falls behind ([areas/write.md](areas/write.md#w-25-backpressure-when-derivation-falls-behind-b-coalesce-and-mark-underived))
- G-19 Deployment modes and background runners ([areas/govern.md](areas/govern.md#g-19-deployment-modes-and-background-runners-a-library-first-with-the-inline-runner-recommended-c))
- P-21 Latency budgets and time-to-searchable ([areas/models.md](areas/models.md#p-21-latency-budgets-and-time-to-searchable-c-hot-path-budgets-plus-time-to-searchable-target))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 65607ea; your worktree: `../memspine-LIB-07`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/runtime/jobs.py`
- `src/memspine/runtime/quotas.py`
- `tests/unit/lib/test_lib_07*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `evals/memspine_evals/systems/memspine_system.py` (F-13a, F-13b, F-13c)
- `scripts/codemod_move.py` (F-06)
- `scripts/moves/F-06.toml` (F-06)
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/api/**` (F-13a, F-13b, F-13c)
- `src/memspine/compose/**` (F-11)
- `src/memspine/compose/__init__.py` (F-06)
- `src/memspine/compose/answer.py` (F-06)
- `src/memspine/compose/answer_check.py` (F-06)
- `src/memspine/compose/budget.py` (LIB-06)
- `src/memspine/compose/bundles.py` (LIB-06)
- `src/memspine/compose/compression.py` (F-06)
- `src/memspine/compose/pack.py` (F-06)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/core/answer*` (F-06)
- `src/memspine/core/lead.py` (F-06)
- `src/memspine/core/perspective.py` (F-06)
- `src/memspine/core/policies/assembly.py` (F-06)
- `src/memspine/core/policies/compression.py` (F-06)
- `src/memspine/core/query_*` (F-06)
- `src/memspine/core/temporal_*` (F-06)
- `src/memspine/derive/compile.py` (LIB-05)
- `src/memspine/derive/graph.py` (F-01, F-02a, F-02b, F-02c)
- `src/memspine/derive/ground.py` (LIB-03)
- `src/memspine/derive/integrate.py` (LIB-02)
- `src/memspine/derive/resolve.py` (LIB-04)
- `src/memspine/derive/stages/**` (F-15)
- `src/memspine/derive/temporal.py` (LIB-01)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11, F-12, F-13a, F-13b, F-13c)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/memory/**` (F-12)
- `src/memspine/models/**` (F-06)
- `src/memspine/protocols/**` (F-13a, F-13b, F-13c)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/read/__init__.py` (F-06)
- `src/memspine/read/contract.py` (F-06)
- `src/memspine/read/lead.py` (F-06)
- `src/memspine/read/perspective.py` (F-06)
- `src/memspine/read/shape.py` (F-06)
- `src/memspine/read/temporal_query.py` (F-06)
- `src/memspine/read/temporal_resolve.py` (F-06)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/services/cache*` (F-06)
- `src/memspine/services/cache/**` (F-06)
- `src/memspine/services/decision*` (F-06)
- `src/memspine/services/decision/**` (F-06)
- `src/memspine/services/embedding*` (F-06)
- `src/memspine/services/embedding/**` (F-06)
- `src/memspine/services/llm*` (F-06)
- `src/memspine/services/llm/**` (F-06)
- `src/memspine/services/query_encoder*` (F-06)
- `src/memspine/services/query_encoder/**` (F-06)
- `src/memspine/services/rerank*` (F-06)
- `src/memspine/services/rerank/**` (F-06)
- `src/memspine/store/backends/**/alembic/versions/*` (F-02a, F-02b, F-02c)
- `src/memspine/store/envelope.py` (F-02a, F-02b, F-02c)
- `src/memspine/store/keys.py` (F-01)
- `src/memspine/store/layers.py` (F-02a, F-02b, F-02c)
- `src/memspine/store/records.py` (F-02a, F-02b, F-02c)
- `src/memspine/trust/**` (F-12)
- `src/memspine/trust/erasure_plan.py` (LIB-09)
- `src/memspine/trust/families.py` (LIB-08)
- `src/memspine/workers/pipelines.py` (F-15)
- `tests/golden/harness.py` (F-06)
- `tests/unit/lib/test_lib_01*.py` (LIB-01)
- `tests/unit/lib/test_lib_02*.py` (LIB-02)
- `tests/unit/lib/test_lib_03*.py` (LIB-03)
- `tests/unit/lib/test_lib_04*.py` (LIB-04)
- `tests/unit/lib/test_lib_05*.py` (LIB-05)
- `tests/unit/lib/test_lib_06*.py` (LIB-06)
- `tests/unit/lib/test_lib_08*.py` (LIB-08)
- `tests/unit/lib/test_lib_09*.py` (LIB-09)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in F-04); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-07: Library: job leases, epochs, watermark and holes, handles, coalescing, quotas` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
