# Agent brief: LIB-16 Library: typed query contract by rules (shape, time view, personal vs generic, list/count, render header)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: typed query contract by rules (shape, time view, personal vs generic, list/count, render header). Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- R-03 Typed query contract adoption ([areas/read.md](areas/read.md#r-03-typed-query-contract-adoption-b-heuristic-contract-on-by-default))
- R-20 Rendering format, order and dates ([areas/read.md](areas/read.md#r-20-rendering-format-order-and-dates-d-typed-sections-contract-header-integer-aliases))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 61d7987; your worktree: `../memspine-LIB-16`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/read/contract_rules.py`
- `tests/unit/lib/test_lib_16*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `scripts/codemod_move.py` (F-06)
- `scripts/moves/F-06.toml` (F-06)
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/compose/__init__.py` (F-06)
- `src/memspine/compose/answer.py` (F-06)
- `src/memspine/compose/answer_check.py` (F-06)
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
- `src/memspine/derive/coverage.py` (LIB-17)
- `src/memspine/derive/ground.py` (LIB-03)
- `src/memspine/engine.py` (F-08, F-09)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/roles.py` (LIB-13)
- `src/memspine/ingest/segment.py` (LIB-14)
- `src/memspine/models/**` (F-06)
- `src/memspine/read/__init__.py` (F-06)
- `src/memspine/read/contract.py` (F-06)
- `src/memspine/read/gate.py` (LIB-12)
- `src/memspine/read/lead.py` (F-06)
- `src/memspine/read/lift.py` (LIB-11)
- `src/memspine/read/obligations.py` (LIB-15)
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
- `tests/golden/harness.py` (F-06)
- `tests/unit/lib/test_lib_03*.py` (LIB-03)
- `tests/unit/lib/test_lib_05*.py` (LIB-05)
- `tests/unit/lib/test_lib_11*.py` (LIB-11)
- `tests/unit/lib/test_lib_12*.py` (LIB-12)
- `tests/unit/lib/test_lib_13*.py` (LIB-13)
- `tests/unit/lib/test_lib_14*.py` (LIB-14)
- `tests/unit/lib/test_lib_15*.py` (LIB-15)
- `tests/unit/lib/test_lib_17*.py` (LIB-17)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in R3-02); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-16: Library: typed query contract by rules (shape, time view, personal vs generic, list/count, render header)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
