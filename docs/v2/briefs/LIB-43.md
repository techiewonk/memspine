# Agent brief: LIB-43 Library: deep tier sufficiency check and one-rewrite plan (model call injected)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: deep tier sufficiency check and one-rewrite plan (model call injected). Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- R-22 Agentic multi-step read design ([areas/read.md](areas/read.md#r-22-agentic-multi-step-read-design-b-sufficiency-check-plus-one-rewrite-round-owner-chose-differently))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at d176408; your worktree: `../memspine-LIB-43`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/read/deep.py`
- `tests/unit/lib/test_lib_43*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/derive/feedback.py` (LIB-44)
- `src/memspine/engine.py` (F-08, F-09)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/models/tiers.py` (LIB-42)
- `src/memspine/prompts/layout.py` (LIB-46)
- `src/memspine/read/external.py` (LIB-45)
- `src/memspine/read/tiers.py` (LIB-47)
- `src/memspine/runtime/container.py` (F-08)
- `tests/unit/lib/test_lib_42*.py` (LIB-42)
- `tests/unit/lib/test_lib_44*.py` (LIB-44)
- `tests/unit/lib/test_lib_45*.py` (LIB-45)
- `tests/unit/lib/test_lib_46*.py` (LIB-46)
- `tests/unit/lib/test_lib_47*.py` (LIB-47)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in R3-12); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-43: Library: deep tier sufficiency check and one-rewrite plan (model call injected)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
