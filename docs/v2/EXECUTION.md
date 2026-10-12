# How the restructure is executed

Owner decisions of 2026-10-13. Applies to every work package in `PLAN.md`.

| Topic | Decision |
|---|---|
| Branching | One integration branch `restructure` off `feat/local-qwen-stack`. Each package = one agent = one git worktree = one PR into `restructure`. Merged only when both test suites and the CPU golden pass. `restructure` merges into the main branch at each phase exit (EX-1 ... EX-5). |
| Review | CI gates must pass (unit and eval tests, CPU golden, import-linter contracts, blocking gold-lint, single Alembic head, flag-evidence check). Claude reviews every PR against its package brief and the pipeline spec; Codex gives a second opinion on behaviour and foundation packages; the owner approves each phase exit. |
| Agents | Sonnet, one package each, at most 8 running at once. A serial lane (`S1 split`, `S2 foundations`, `S3 read`, `S4 extraction`) never has two agents at the same time; other lanes run in parallel once their dependencies are Done. |
| Python | 3.13 only (already the floor in `pyproject.toml`); new code may use 3.13 features (TaskGroup, `type` statements, PEP 695 generics). |
| GPU | Stopped by the owner. Behaviour packages end at "built, flag off, golden green" with status **Needs measurement**; screens are queued and run when the owner restarts the GPU. |
| Brief | Every agent gets the package's brief (X-01 template): goal and decision ids with `areas/*.md` anchors, pipeline spec sections, owned and forbidden files, tests to add or migrate, golden or gate, docs and ADR to update, done-definition, PR title. |
| Status | `data/packages.py` is the source of truth for rows; the live tracker page keeps status and notes; both are synced into `PLAN.md` on each commit. |

## Change log
- 2026-10-13: first version (owner answers on branching, review, Python, agent count).
