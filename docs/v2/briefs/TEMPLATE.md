# Agent brief: <package id> <title>

Package X-01 template. One brief per work package; the agent (Sonnet) reads only this brief and the files it links.

## Goal
<one paragraph: what changes and why, in plain words>

## Decisions implemented
<decision ids with links to docs/v2/areas/<area>.md anchors and docs/v2/pipelines/<pipeline>.md sections>

## Inputs
- Branch: `restructure` at <commit>; your worktree: `../memspine-<package id>`
- Depends on (must be Done): <ids>
- Read first: <spec sections, ADRs, RESTRUCTURE.md rows>

## You own (may write)
<target files and their tests>

## You must not touch
<files owned by other packages in flight; pinned_run.sh interface; evals/runs; data/>

## Steps
1. ...

## Tests
- Add: <tests>
- Migrate: <tests>
- Must stay green: `tests/unit`, `evals/tests`, CPU golden, import-linter, gold-lint, single Alembic head

## Gate
<move: golden unchanged | behaviour: built flag-off, golden unchanged, screen queued (status Needs measurement)>

## Docs to update in the same PR
<pipeline spec step, ARCHITECTURE section, ADR, PLAN status>

## Done when
- [ ] all checks above pass
- [ ] PR titled `<package id>: <title>` opened into `restructure`, description lists decisions and checks
- [ ] no model call added on a default path; no layer turned on that the decisions keep off
- [ ] status in PLAN set to Done (move/infra) or Needs measurement (behaviour)
