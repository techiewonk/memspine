# Agent brief: LIB-01 Library: temporal anchor ladder (basis, precision, intervals, relations; never invents dates)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: temporal anchor ladder (basis, precision, intervals, relations; never invents dates). Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- W-04 Times stamped on every raw input ([areas/write.md](areas/write.md#w-04-times-stamped-on-every-raw-input-c-three-clocks-plus-declared-zone-note-b--c))
- M-10 Temporal fields on a record ([areas/model.md](areas/model.md#m-10-temporal-fields-on-a-record-b-tri-temporal-with-precision-note-b--c))
- R-07 Temporal leg and relative-time anchoring ([areas/read.md](areas/read.md#r-07-temporal-leg-and-relative-time-anchoring-d-anchored-window-plus-event-time-index))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 65607ea; your worktree: `../memspine-LIB-01`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/derive/temporal.py`
- `tests/unit/lib/test_lib_01*.py`
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
- `src/memspine/derive/write.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11, F-12, F-13a, F-13b, F-13c)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/commit.py` (F-04, RT-01, RT-02)
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
- `src/memspine/runtime/**` (F-04, RT-01, RT-02)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/runtime/jobs.py` (LIB-07)
- `src/memspine/runtime/quotas.py` (LIB-07)
- `src/memspine/services/cache*` (F-06)
- `src/memspine/services/cache/**` (F-06)
- `src/memspine/services/decision*` (F-06)
- `src/memspine/services/decision/**` (F-06)
- `src/memspine/services/embedding*` (F-06)
- `src/memspine/services/embedding/**` (F-06)
- `src/memspine/services/graph/neo4j.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/services/llm*` (F-06)
- `src/memspine/services/llm/**` (F-06)
- `src/memspine/services/query_encoder*` (F-06)
- `src/memspine/services/query_encoder/**` (F-06)
- `src/memspine/services/rerank*` (F-06)
- `src/memspine/services/rerank/**` (F-06)
- `src/memspine/store/backends/**/alembic/versions/*` (F-02a, F-02b, F-02c)
- `src/memspine/store/backends/neo4j/**` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/store/backends/postgres/**` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/store/envelope.py` (F-02a, F-02b, F-02c)
- `src/memspine/store/keys.py` (F-01)
- `src/memspine/store/layers.py` (F-02a, F-02b, F-02c)
- `src/memspine/store/lineage.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/store/migrate.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/store/profiles.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/store/records.py` (F-02a, F-02b, F-02c)
- `src/memspine/trust/**` (F-12)
- `src/memspine/trust/erasure_plan.py` (LIB-09)
- `src/memspine/trust/families.py` (LIB-08)
- `src/memspine/workers/inline.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/pipelines.py` (F-15)
- `src/memspine/workers/runner.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/schedule.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/scheduler.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/step_journal.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/taskiq_runner.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/temporal_runner.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/temporal_workflow.py` (F-04, RT-01, RT-02)
- `tests/golden/harness.py` (F-06)
- `tests/unit/lib/test_lib_02*.py` (LIB-02)
- `tests/unit/lib/test_lib_03*.py` (LIB-03)
- `tests/unit/lib/test_lib_04*.py` (LIB-04)
- `tests/unit/lib/test_lib_05*.py` (LIB-05)
- `tests/unit/lib/test_lib_06*.py` (LIB-06)
- `tests/unit/lib/test_lib_07*.py` (LIB-07)
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
Unit tests; pure, unwired (wiring in W2-14); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-01: Library: temporal anchor ladder (basis, precision, intervals, relations; never invents dates)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
