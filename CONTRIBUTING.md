# Contributing to memspine

Thanks for helping build the memory spine.

## Setup

```bash
uv sync --all-extras
just check          # ruff + mypy + pytest must be green
```

Python **3.13+**. We use **uv** (env), **ruff** (lint+format), **mypy** (strict types), **pytest** (tests), **mkdocs-material** (docs).

## Before you write code

1. Read `CLAUDE.md` (golden rules) and the relevant part of `docs/memspine-structure-plan.md`. The **structure plan is authoritative**.
2. Confirm which **phase (P0–P7)** your change belongs to and which **decisions (D-xx)** it implements.
3. Don't add heavy dependencies to core — everything optional goes behind a `pip install memspine[extra]` (slim-core rule, D-03).

## The rules that block a merge

- Event-sourced core: the append-only log is the only source of truth; vector/graph/FTS/cache are rebuildable projectors.
- Anti-lock-in: background work is plain idempotent step functions; runners decorate them.
- Ports & adapters: talk only to `services/*`; `clients/*` own connections.
- `profile="simple"` stays green and backward-compatible.
- Tests mirror `src/` 1:1; new capabilities add a combination-matrix boot.

## Decisions & docs

- A new or changed architectural decision requires an **ADR** (`docs/adr/`, copy `ADR-000-template.md`) **and** a register row in the structure plan. Use the `/decision` Claude Code command to do both.
- Update docs in the same PR as the code that changes a contract.

## Commits & PRs

- Conventional-ish commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`).
- Keep PRs phase-scoped and small. CI runs lint + typecheck + unit + the combination matrix.
