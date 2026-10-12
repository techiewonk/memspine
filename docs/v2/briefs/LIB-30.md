# Agent brief: LIB-30 Library: source envelope intake, receipt and readiness, write ids and re-ingest

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: other; kind: infra). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
<TODO goal: Library: source envelope intake, receipt and readiness, write ids and re-ingest. Registry note: Library-first split (owner 2026-10-13: parallel agents)>

## Decisions implemented
- W-01 Unit of a write call ([areas/write.md](areas/write.md#w-01-unit-of-a-write-call-d-typed-source-envelope-per-message-raw))
- W-02 What a write receipt guarantees ([areas/write.md](areas/write.md#w-02-what-a-write-receipt-guarantees-d-raw-searchable-plus-derivation-intent))
- W-03 Idempotency key for retried writes ([areas/write.md](areas/write.md#w-03-idempotency-key-for-retried-writes-d-caller-id-first-fingerprint-fallback-versioned))
- W-24 Re-ingesting an edited document ([areas/write.md](areas/write.md#w-24-re-ingesting-an-edited-document-c-chunk-diff-on-source-versions))

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: `restructure` at 18adee6; your worktree: `../memspine-LIB-30`
- Depends on (must be Done):
- MOD-01 memspine.schemas: versioned Pydantic input/output types for every pipeline step; JSON Schema export to docs/v2/schemas: **Done**
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
- `src/memspine/ingest/intake.py`
- `tests/unit/lib/test_lib_30*.py`
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
- `src/memspine/engine.py` (F-08)
- `src/memspine/ingest/stamp.py` (LIB-29)
- `src/memspine/memory/procedural.py` (LIB-28)
- `src/memspine/memory/prospective.py` (LIB-28)
- `src/memspine/models/**` (F-06)
- `src/memspine/observability/stage_ledger.py` (LIB-27)
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
- `src/memspine/runtime/container.py` (F-08)
- `src/memspine/runtime/freshness.py` (LIB-26)
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
- `tests/unit/lib/test_lib_16*.py` (LIB-16)
- `tests/unit/lib/test_lib_22*.py` (LIB-22)
- `tests/unit/lib/test_lib_23*.py` (LIB-23)
- `tests/unit/lib/test_lib_24*.py` (LIB-24)
- `tests/unit/lib/test_lib_25*.py` (LIB-25)
- `tests/unit/lib/test_lib_26*.py` (LIB-26)
- `tests/unit/lib/test_lib_27*.py` (LIB-27)
- `tests/unit/lib/test_lib_28*.py` (LIB-28)
- `tests/unit/lib/test_lib_29*.py` (LIB-29)
- `evals/runs`, `data/`, the `pinned_run.sh` interface (unless listed under You own)

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
Unit tests; pure, unwired (wiring in W2-01); depends only on memspine.schemas

Kind gate (infra): Tooling or docs: tests for the tooling, existing suites green; no change to defaults.

Review findings implemented: none

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `LIB-30: Library: source envelope intake, receipt and readiness, write ids and re-ingest` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
