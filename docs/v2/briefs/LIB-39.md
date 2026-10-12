# Agent brief: LIB-39 Library: profile card with guarded operations and reflection plans (opt-in)

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: profile card with guarded operations and reflection plans (opt-in). Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- O-18 How the profile is updated ([areas/offline.md](areas/offline.md#o-18-how-the-profile-is-updated-b-guarded-operations-against-a-card-note-a--b))
- O-19 Profile card schema, caps and inferences ([areas/offline.md](areas/offline.md#o-19-profile-card-schema-caps-and-inferences-b-typed-card-code-caps-stated-only-by-default))
- O-20 Reflection and insight generation ([areas/offline.md](areas/offline.md#o-20-reflection-and-insight-generation-b-per-session-cited-depth-capped))
- M-20 Profile and persona representation ([areas/model.md](areas/model.md#m-20-profile-and-persona-representation-b-capped-typed-card-citing-facts))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 5b594ba; your worktree: `../memspine-LIB-39`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/derive/profile.py`
- `tests/unit/lib/test_lib_39*.py`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `evals/memspine_evals/systems/memspine_system.py` (F-13a, F-13b, F-13c)
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/api/**` (F-13a, F-13b, F-13c)
- `src/memspine/compose/**` (F-11)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/derive/communities.py` (LIB-36)
- `src/memspine/derive/edges.py` (LIB-35)
- `src/memspine/derive/graph.py` (F-01, F-02a, F-02b, F-02c)
- `src/memspine/derive/patterns.py` (LIB-31)
- `src/memspine/derive/reconcile.py` (LIB-37)
- `src/memspine/derive/session_close.py` (LIB-40)
- `src/memspine/derive/stages/**` (F-15)
- `src/memspine/derive/standing.py` (LIB-32)
- `src/memspine/derive/write.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11, F-12, F-13a, F-13b, F-13c)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/ingest/commit.py` (F-04, RT-01, RT-02)
- `src/memspine/memory/**` (F-12)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/protocols/**` (F-13a, F-13b, F-13c)
- `src/memspine/read/**` (F-10a, F-10b)
- `src/memspine/runtime/**` (F-04, RT-01, RT-02)
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/services/graph/neo4j.py` (F-16a, F-16b, F-16c, F-16d, F-16e, F-16f)
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
- `src/memspine/trust/gates.py` (LIB-38)
- `src/memspine/workers/dbos_runner.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/inline.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/pipelines.py` (F-15)
- `src/memspine/workers/runner.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/schedule.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/scheduler.py` (F-04, RT-01, RT-02)
- `src/memspine/workers/taskiq_runner.py` (F-04, RT-01, RT-02)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_31*.py` (LIB-31)
- `tests/unit/lib/test_lib_32*.py` (LIB-32)
- `tests/unit/lib/test_lib_35*.py` (LIB-35)
- `tests/unit/lib/test_lib_36*.py` (LIB-36)
- `tests/unit/lib/test_lib_37*.py` (LIB-37)
- `tests/unit/lib/test_lib_38*.py` (LIB-38)
- `tests/unit/lib/test_lib_40*.py` (LIB-40)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in O4-07); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-39: Library: profile card with guarded operations and reflection plans (opt-in)` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
