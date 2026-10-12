# Agent brief: LIB-38 Library: firewall at three gates (insert, derive, read) with base and extended signals

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: firewall at three gates (insert, derive, read) with base and extended signals. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- W-07 Firewall placement on the derivation path ([areas/write.md](areas/write.md#w-07-firewall-placement-on-the-derivation-path-c-three-gates-cap-always-on))
- G-06 Memory firewall default posture ([areas/govern.md](areas/govern.md#g-06-memory-firewall-default-posture-c-with-note-a--c))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at b034f79; your worktree: `../memspine-LIB-38`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/trust/gates.py`
- `tests/unit/lib/test_lib_38*.py`
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
- `src/memspine/derive/reconcile.py` (LIB-37)
- `src/memspine/derive/standing.py` (LIB-32)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/intake.py` (LIB-30)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/read/entity_leg.py` (LIB-34)
- `src/memspine/runtime/container.py` (F-08)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_30*.py` (LIB-30)
- `tests/unit/lib/test_lib_31*.py` (LIB-31)
- `tests/unit/lib/test_lib_32*.py` (LIB-32)
- `tests/unit/lib/test_lib_34*.py` (LIB-34)
- `tests/unit/lib/test_lib_35*.py` (LIB-35)
- `tests/unit/lib/test_lib_36*.py` (LIB-36)
- `tests/unit/lib/test_lib_37*.py` (LIB-37)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in W2-04); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-38: Library: firewall at three gates (insert, derive, read) with base and extended signals` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
