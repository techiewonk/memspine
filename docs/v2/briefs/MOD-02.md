# Agent brief: MOD-02 Typed registries + entry points (discovery is not enablement: pinned capability manifest)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: S1 split (serial); kind: api). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Typed registries + entry points (discovery is not enablement: pinned capability manifest). Registry note: Owner 2026-10-13>

## Decisions implemented
- G-16 Shape of the public Python API ([areas/govern.md](areas/govern.md#g-16-shape-of-the-public-python-api-d-loop-verb-core-on-scoped-handles))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 364397b; your worktree: `../memspine-MOD-02`
- Depends on (must be Done):
- F-06 Move read, compose and models leaves: **Done**
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/*/registry.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `src/memspine/ingest/intake.py` (LIB-30)
- `src/memspine/ingest/stamp.py` (LIB-29)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/observability/stage_ledger.py` (LIB-27)
- `tests/unit/lib/test_lib_27*.py` (LIB-27)
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
Built-ins register at import; entry-point plug-in test; golden unchanged

Kind gate (api): New contract or lifetime: contract tests added; golden unchanged; public surface documented.

Review findings implemented: ER-03, ER-33, ER-34

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `MOD-02: Typed registries + entry points (discovery is not enablement: pinned capability manifest)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
