# Agent brief: LIB-34 Library: entity leg scoring with anti-hub damping

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: entity leg scoring with anti-hub damping. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- R-08 Entity leg and hub damping ([areas/read.md](areas/read.md#r-08-entity-leg-and-hub-damping-b-inverse-degree-penalty))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 19aa125; your worktree: `../memspine-LIB-34`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/read/entity_leg.py`
- `tests/unit/lib/test_lib_34*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/derive/decay.py` (LIB-33)
- `src/memspine/derive/edges.py` (LIB-35)
- `src/memspine/derive/patterns.py` (LIB-31)
- `src/memspine/derive/standing.py` (LIB-32)
- `src/memspine/engine.py` (F-08, F-09)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/intake.py` (LIB-30)
- `src/memspine/ingest/stamp.py` (LIB-29)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/observability/stage_ledger.py` (LIB-27)
- `src/memspine/runtime/container.py` (F-08)
- `tests/unit/lib/test_lib_27*.py` (LIB-27)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_29*.py` (LIB-29)
- `tests/unit/lib/test_lib_30*.py` (LIB-30)
- `tests/unit/lib/test_lib_31*.py` (LIB-31)
- `tests/unit/lib/test_lib_32*.py` (LIB-32)
- `tests/unit/lib/test_lib_33*.py` (LIB-33)
- `tests/unit/lib/test_lib_35*.py` (LIB-35)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in R3-05a); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-34: Library: entity leg scoring with anti-hub damping` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
