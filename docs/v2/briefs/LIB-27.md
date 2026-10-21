# Agent brief: LIB-27 Library: per-stage cost ledger, write targets and budget reporting (cost classes, no content)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: per-stage cost ledger, write targets and budget reporting (cost classes, no content). Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- W-26 Write-side cost ledger and budget ([areas/write.md](areas/write.md#w-26-write-side-cost-ledger-and-budget-d-manifest-plus-enforced-budget))
- P-14 Write-side call and token targets ([areas/models.md](areas/models.md#p-14-write-side-call-and-token-targets-d-targets-as-gates-optional-hard-budget))
- P-22 Cost ledger scope and spend limits ([areas/models.md](areas/models.md#p-22-cost-ledger-scope-and-spend-limits-c-persisted-ledger-plus-optional-budgets))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 18adee6; your worktree: `../memspine-LIB-27`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/observability/stage_ledger.py`
- `tests/unit/lib/test_lib_27*.py`
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
- `src/memspine/compose/compression.py` (F-06)
- `src/memspine/compose/pack.py` (F-06)
- `src/memspine/compose/render_trust.py` (LIB-25)
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
- `src/memspine/derive/graph.py` (F-01, F-02a, F-02b, F-02c)
- `src/memspine/derive/stages/**` (F-15)
- `src/memspine/derive/write.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11, F-12, F-13a, F-13b, F-13c)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/commit.py` (F-04, RT-01, RT-02)
- `src/memspine/ingest/intake.py` (LIB-30)
- `src/memspine/ingest/stamp.py` (LIB-29)
- `src/memspine/memory/**` (F-12)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/models/**` (F-06)
- `src/memspine/protocols/**` (F-13a, F-13b, F-13c)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/read/__init__.py` (F-06)
- `src/memspine/read/contract.py` (F-06)
- `src/memspine/read/contract_rules.py` (LIB-16)
- `src/memspine/read/lead.py` (F-06)
- `src/memspine/read/neighbours.py` (LIB-24)
- `src/memspine/read/perspective.py` (F-06)
- `src/memspine/read/router_rules.py` (LIB-23)
- `src/memspine/read/shape.py` (F-06)
- `src/memspine/read/temporal_query.py` (F-06)
- `src/memspine/read/temporal_resolve.py` (F-06)
- `src/memspine/read/timeview.py` (LIB-22)
- `src/memspine/runtime/**` (F-04, RT-01, RT-02)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/runtime/freshness.py` (LIB-26)
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
- `tests/unit/lib/test_lib_16*.py` (LIB-16)
- `tests/unit/lib/test_lib_22*.py` (LIB-22)
- `tests/unit/lib/test_lib_23*.py` (LIB-23)
- `tests/unit/lib/test_lib_24*.py` (LIB-24)
- `tests/unit/lib/test_lib_25*.py` (LIB-25)
- `tests/unit/lib/test_lib_26*.py` (LIB-26)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_29*.py` (LIB-29)
- `tests/unit/lib/test_lib_30*.py` (LIB-30)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in W2-12); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-27: Library: per-stage cost ledger, write targets and budget reporting (cost classes, no content)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
