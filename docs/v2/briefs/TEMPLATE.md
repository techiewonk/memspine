# Agent brief: {{id}} {{title}}

Package X-01 template, filled by `python docs/v2/data/build_plan.py --brief <ID>` (lane: {{lane}}; kind: {{kind}}). One brief per work package; the agent (Sonnet) reads only this brief and the files it links. Placeholders left as `<TODO ...>` are for the coordinator to complete before the brief is handed out.

## Goal
{{goal}}

## Decisions implemented
{{decisions}}

Pipeline specs: see `docs/v2/pipelines/` for the steps these decisions touch.

## Inputs
- Branch: {{base}}; your worktree: `../memspine-{{id}}`
- Depends on (must be Done):
{{depends}}
- Read first: the decision sections above, `docs/v2/RESTRUCTURE.md` section 3 row for this package, relevant ADRs

## You own (may write)
{{owned}}

## You must not touch
Files owned by packages that can run at the same time as this one:
{{forbidden}}

## Steps
1. <TODO steps>

## Tests
- Add: <TODO tests>
- Migrate: <TODO tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
{{gate}}

Kind gate ({{kind}}): {{kind_gate}}

Review findings implemented: {{review}}

## Docs to update in the same PR
<TODO pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `{{id}}: {{title}}` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
