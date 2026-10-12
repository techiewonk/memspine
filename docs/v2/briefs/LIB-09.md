# Agent brief: LIB-09 Library: erasure planner (hide then re-derive over lineage) and manifest

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: erasure planner (hide then re-derive over lineage) and manifest. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- G-13 Right to erasure across derived state ([areas/govern.md](areas/govern.md#g-13-right-to-erasure-across-derived-state-c-reverse-lineage-plus-re-derivation))
- M-25 Forgetting and erasure of derived text ([areas/model.md](areas/model.md#m-25-forgetting-and-erasure-of-derived-text-b-cascade--deferred-re-derivation))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 65607ea; your worktree: `../memspine-LIB-09`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/trust/erasure_plan.py`
- `tests/unit/lib/test_lib_09*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `scripts/codemod_move.py` (F-06)
- `scripts/moves/F-06.toml` (F-06)
- `src/memspine/*/registry.py` (MOD-02)
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
- `src/memspine/derive/ground.py` (LIB-03)
- `src/memspine/derive/integrate.py` (LIB-02)
- `src/memspine/derive/resolve.py` (LIB-04)
- `src/memspine/derive/temporal.py` (LIB-01)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/models/**` (F-06)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/read/__init__.py` (F-06)
- `src/memspine/read/contract.py` (F-06)
- `src/memspine/read/lead.py` (F-06)
- `src/memspine/read/perspective.py` (F-06)
- `src/memspine/read/shape.py` (F-06)
- `src/memspine/read/temporal_query.py` (F-06)
- `src/memspine/read/temporal_resolve.py` (F-06)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/runtime/jobs.py` (LIB-07)
- `src/memspine/runtime/quotas.py` (LIB-07)
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
- `src/memspine/trust/families.py` (LIB-08)
- `tests/golden/harness.py` (F-06)
- `tests/unit/lib/test_lib_01*.py` (LIB-01)
- `tests/unit/lib/test_lib_02*.py` (LIB-02)
- `tests/unit/lib/test_lib_03*.py` (LIB-03)
- `tests/unit/lib/test_lib_04*.py` (LIB-04)
- `tests/unit/lib/test_lib_05*.py` (LIB-05)
- `tests/unit/lib/test_lib_06*.py` (LIB-06)
- `tests/unit/lib/test_lib_07*.py` (LIB-07)
- `tests/unit/lib/test_lib_08*.py` (LIB-08)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in T5-04); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-09: Library: erasure planner (hide then re-derive over lineage) and manifest` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
