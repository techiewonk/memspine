# Agent brief: MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas. Registry note: Owner 2026-10-13; new files only; sessions 1-2 answered (schemas/SESSIONS_01_02)>

## Decisions implemented
- G-16 Shape of the public Python API ([areas/govern.md](areas/govern.md#g-16-shape-of-the-public-python-api-d-loop-verb-core-on-scoped-handles))
- M-26 Schema versioning and migration ([areas/model.md](areas/model.md#m-26-schema-versioning-and-migration-b-event-upcasters--derivation-versions))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at aec2dfa; your worktree: `../memspine-MOD-01`
- Depends on (must be Done):
- X-00 RESTRUCTURE.md: S0-S16, target tree, owned file sets per package: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/schemas/**`
- `docs/v2/schemas/durable/**`
- `docs/v2/schemas/public/**`
- `docs/v2/schemas/internal/**`
- `tests/unit/schemas/**`
- the tests for the above

## You must not touch
Files owned by packages that can run at the same time as this one:
- `evals/README.md` (P0-02, P0-03)
- `evals/memspine_evals/paired_gate.py` (P0-02, P0-03)
- `evals/memspine_evals/provenance.py` (P0-02, P0-03)
- `evals/memspine_evals/systems/memspine_system.py` (F-13a, F-13b, F-13c)
- `evals/pinned_run.sh` (P0-02, P0-03)
- `src/memspine/*/registry.py` (MOD-02)
- `src/memspine/api/**` (F-13a, F-13b, F-13c)
- `src/memspine/compose/**` (F-06, F-11)
- `src/memspine/config/flags.py` (F-07)
- `src/memspine/config/schema.py` (F-07)
- `src/memspine/config/schema/**` (F-07)
- `src/memspine/core/answer*` (F-06)
- `src/memspine/core/audit.py` (F-05)
- `src/memspine/core/erasure.py` (F-05)
- `src/memspine/core/events.py` (F-05)
- `src/memspine/core/firewall.py` (F-05)
- `src/memspine/core/lead.py` (F-06)
- `src/memspine/core/namespace.py` (F-05)
- `src/memspine/core/perspective.py` (F-06)
- `src/memspine/core/policies/assembly.py` (F-06)
- `src/memspine/core/policies/compression.py` (F-06)
- `src/memspine/core/privacy.py` (F-05)
- `src/memspine/core/projector.py` (F-05)
- `src/memspine/core/query_*` (F-06)
- `src/memspine/core/records.py` (F-05)
- `src/memspine/core/redaction.py` (F-05)
- `src/memspine/core/replay.py` (F-05)
- `src/memspine/core/temporal_*` (F-06)
- `src/memspine/core/visibility.py` (F-05)
- `src/memspine/derive/graph.py` (F-01)
- `src/memspine/derive/stages/**` (F-15)
- `src/memspine/engine.py` (F-08, F-09, F-10a, F-10b, F-11, F-12, F-13a, F-13b, F-13c)
- `src/memspine/ingest/**` (F-09)
- `src/memspine/memory/**` (F-12)
- `src/memspine/models/**` (F-06)
- `src/memspine/prompts/lint.py` (P0-09)
- `src/memspine/protocols/**` (F-13a, F-13b, F-13c)
- `src/memspine/read/**` (F-06, F-10a, F-10b)
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
- `src/memspine/store/events.py` (F-05)
- `src/memspine/store/keys.py` (F-01)
- `src/memspine/store/lineage.py` (F-05)
- `src/memspine/store/log.py` (F-05)
- `src/memspine/store/records.py` (F-05)
- `src/memspine/store/scope.py` (F-05)
- `src/memspine/trust/**` (F-05, F-12)
- `src/memspine/workers/pipelines.py` (F-15)
- `tests/golden/**` (X-03)
- `tests/unit/test_prompt_gold_lint.py` (P0-09)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Every step in pipelines/*.md has a schema; export reproducible in CI

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: ER-32, ER-33

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `MOD-01: memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
