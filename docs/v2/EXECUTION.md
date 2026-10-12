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

## Ownership checker and brief generator (X-01)

Both live in `data/build_plan.py`; ownership data is `OWNS` and `ALLOWED_SHARED` in `data/packages.py`.

- **`OWNS`**: package id to repo-relative glob patterns the package may write (`*` stays inside a directory, `**` crosses). It covers target files and the "Removes or edits" column of `RESTRUCTURE.md` section 3, because a moved file is written by its mover. It is not derived from decision text (the `files` field in `plan.json` is a hint only).
- **Checker** (runs on every `python docs/v2/data/build_plan.py`): two packages are *concurrently runnable* when neither depends on the other transitively and they are not in the same serial lane. For every such pair where neither is Done, their `OWNS` patterns must not overlap: identical patterns, one glob matching the other's literal path, both matching a common existing file, or (files not yet created) a `dir/**` glob containing the other pattern. Collisions are printed and the script exits non-zero, except for pairs in `ALLOWED_SHARED` (with a reason). Packages with no `OWNS` entry are listed as a warning ("no ownership yet, cannot be briefed"). Add the entry before launching the agent.
- **Generator**: `python docs/v2/data/build_plan.py --brief <ID>` writes `docs/v2/briefs/<ID>.md` from `briefs/TEMPLATE.md`: decisions with links into `areas/`, dependencies with status, owned files, must-not-touch (the `OWNS` of every concurrently runnable package that is not Done), gate with kind-specific text, review ids. It refuses (exit 1) if a dependency is not Done or `OWNS` has no entry; `--force` overrides. Goal, steps, tests and docs stay `<TODO ...>` for the coordinator.
- Tests: `python -m unittest docs/v2/data/test_build_plan.py`.

## Change log
- 2026-10-13: first version (owner answers on branching, review, Python, agent count).
- 2026-10-13: ownership checker and brief generator (X-01).
