# Agent brief: LIB-41 Library: retention classes and audit records per storage profile

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: retention classes and audit records per storage profile. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- G-14 Retention policy and event log lifetime ([areas/govern.md](areas/govern.md#g-14-retention-policy-and-event-log-lifetime-d-retention-set-per-profile))
- G-15 Audit log scope and default ([areas/govern.md](areas/govern.md#g-15-audit-log-scope-and-default-d-actions-always-reads-in-shared-and-regulated-profiles))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 5b57520; your worktree: `../memspine-LIB-41`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/trust/retention.py`
- `tests/unit/lib/test_lib_41*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/compose/**` (F-11)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/derive/communities.py` (LIB-36)
- `src/memspine/derive/edges.py` (LIB-35)
- `src/memspine/derive/patterns.py` (LIB-31)
- `src/memspine/derive/profile.py` (LIB-39)
- `src/memspine/derive/reconcile.py` (LIB-37)
- `src/memspine/derive/session_close.py` (LIB-40)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/trust/gates.py` (LIB-38)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_31*.py` (LIB-31)
- `tests/unit/lib/test_lib_35*.py` (LIB-35)
- `tests/unit/lib/test_lib_36*.py` (LIB-36)
- `tests/unit/lib/test_lib_37*.py` (LIB-37)
- `tests/unit/lib/test_lib_38*.py` (LIB-38)
- `tests/unit/lib/test_lib_39*.py` (LIB-39)
- `tests/unit/lib/test_lib_40*.py` (LIB-40)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in T5-05); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-41: Library: retention classes and audit records per storage profile` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
