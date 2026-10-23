# Session transfer to Codex (2026-10-23)

This note hands the memspine `restructure` work from a Claude Code session to Codex. It holds
context only:

- **Task status:** current status, evidence and next steps for every task live in
  [PROVIDER_TODO.md](PROVIDER_TODO.md), the one shared register (CLAUDE.md, RM-28).
- **What to pick up and how:** see [CODEX_TASK_TRANSFER_2026-10-23.md](CODEX_TASK_TRANSFER_2026-10-23.md).
- **Conflicts:** if this note and the register disagree, the register wins.

## 1. Repository state

| Item | Value |
|---|---|
| Working checkout | `C:\Users\hembad\Documents\GitHub\memspine-restructure`, a git worktree of `C:\Users\hembad\Documents\GitHub\memspine` |
| Branch | `restructure`, pushed to `origin/restructure` (github.com/techiewonk/memspine) |
| Head at hand-off | `6d75b5c5`, plus this note's commit. The working tree is clean. |
| Commits in this session | 125 since `7f15d26d`. The list of key commits per task is in section 5. |
| Work in flight | One background agent is upgrading `huggingface-hub` to 2.x in its own worktree (`worktree-agent-ae9c7e9f093e6056b` under `memspine\.claude\worktrees\`). Its result is not merged. If it was not merged when you start, check that branch, then cherry-pick it or redo it (task T-07). |

## 2. Rules that hold for every change

These are owner decisions and CLAUDE.md rules. Keep them exactly.

1. **No trailer lines in commits:** no `Co-Authored-By`, no `Claude-Session`, no other
   trailers. This also applies to any agent you run.
2. **Push only when the owner asks.** Push only to `origin/restructure`; there is no PR
   workflow. Force-push needs explicit owner consent.
3. **One tracker.** `docs/PROVIDER_TODO.md` is the only status source, so do not create
   another TASKS or roadmap file. Refresh it with `python scripts/refresh_tracker.py`, then
   `--check`. A literal `|` inside a cell breaks the parser, so write "or" instead.
4. **No aliases or compatibility shims** (ADR-068 / PA-099). A removed key or module is
   rejected with an error naming its replacement.
5. **No paid API runs.** Use local OpenAI-compatible backends only. The eval QA mode needs
   an explicit model-call cap.
6. **No generative ingestion in the selected profile.**
7. **No benchmark-specific names or rules** in the vocabulary.
8. **Never share results or caches across tenants.** Sharing model weights is allowed.
9. **Run tests in chunks.** This is a low-memory machine, and a single-process full run can
   die silently. Pass `-p no:deepeval` while an old `.venv` still has DeepEval installed.
10. **Parallel agents work in their own worktrees** and never write to the main checkout.
11. **One decision = one ADR plus a register row** in `docs/memspine-structure-plan.md`, and
    CLAUDE.md is updated in the same change.

## 3. Environment (Windows 11, PowerShell 5.1 + Git Bash)

| Need | How |
|---|---|
| Python | `.\.venv\Scripts\python.exe` (3.13), with `$env:PYTHONPATH="$PWD\src"` when running pytest or scripts directly |
| uv | 0.13.5 (winget): `C:\Users\hembad\AppData\Local\Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe`. Anaconda's uv 0.8.0 comes first on the machine PATH, so the owner's PowerShell profile now aliases `uv` and `uvx` to the winget build. `-NoProfile` shells still see 0.8.0, so call the full path there. |
| Lock check | `uv lock --check`; `python scripts/audit_dependencies.py --check`. The audit rewrites `docs/provider_verification/dependency-audit.json` on every run, so commit it only when refreshing. |
| Agent environments | In a separate worktree, run `uv sync --locked --extra dev --extra <needed>`, then `uv run --frozen python -m pytest -p no:deepeval -q <chunk>`. Plain `uv run pytest` misses the repo-root `scripts` package for 3 root test files. |
| Extras missing from the main `.venv` | `[cache]` (cashews): its absence causes the 7 integration failures that also predate this session. `[neo4j]` is also missing. |
| Live services | Use disposable Docker containers with unique names, on free high ports, removed afterwards: `pgvector/pgvector:pg16`, `mongo:7 --replSet rs0` (then `rs.initiate`), `neo4j:5`, `cgr.dev/chainguard/minio` (`minio/minio` is no longer pullable), `cr.weaviate.io/semitechnologies/weaviate`. Test variables are `MEMSPINE_TEST_*` (the conftest clears every other `MEMSPINE_*`), for example `MEMSPINE_TEST_MONGO_URL`, `MEMSPINE_TEST_WEAVIATE_URL`, `MEMSPINE_TEST_S3_URL` and `MEMSPINE_TEST_MONGOD_BINARY`. |
| Import contracts | `python scripts/ci_importlinter.py` must print `18 kept, 0 broken`. The shrink-only baselines are in `tests/unit/baselines/`. |
| Generated files | Regenerate, never hand-merge: `scripts/dump_config_baseline.py` (writes stdout; save to `tests/unit/config/data/config_dump_baseline.json` with LF), `scripts/build_stage_params.py`, `scripts/dump_provider_defaults.py` (`--check`), `scripts/refresh_tracker.py`. |

### Regression procedure used for every merge in this session

1. Run the unit tests in chunks: `wiring`, `lib`, the remaining directories in two halves,
   and the root files. Then run `combinations`, `golden`, `e2e`, `integration`, the evals and
   the import linter. The scripts are in this session's scratchpad. Writing your own takes
   about 30 lines of PowerShell.
2. **Evals:** compare failure *sets*, not counts, against the pre-session baseline commit
   `d9a87db6`. Run `evals/tests` on that commit in a short-path worktree such as
   `C:\Users\hembad\Documents\GitHub\mspre117`, because long paths break `git worktree add`
   on Windows. At hand-off it was 191 failing on the baseline and 187 now, with 0 new; most
   are PA-107 flat eval arms.
3. **Accepted known noise:** the 7 `cashews` integration failures (missing `[cache]`
   extra). `test_w2_01_intake::test_write_p95_is_under_250ms` also flakes under CPU load
   but passes when run alone.

## 4. Decisions made in this session

| Decision | Where | Essence |
|---|---|---|
| D-104 / ADR-080 | `docs/adr/ADR-080-...` | Compiled graph edges carry typed qualifiers. `graph_filter` exists on search, read, REST and MCP. Unknown never passes an allow-filter, and erased edges never pass. Sensitivity is a separate three-state layer. |
| D-105 / ADR-081 + 2026-10-23 amendment | `docs/adr/ADR-081-storage-slots-and-deployment.md` | `storage.<slot>` (records, vector, lexical, graph, cache, documents) plus a `deployment` block. Every durable SQL database uses Alembic. **`storage.graph_mirror` is removed: exactly one graph client runs (RM-120).** |
| D-106 / ADR-082 + RM-118 amendment | `docs/adr/ADR-082-model-slots.md` | `model.embedding`, `model.llm.<role>`, `model.rerank` (always a chain), `model.extraction.<consumer>`, `model.decider.{route,read}`, `model.vision` and `model.placement` (including `thinking`). There are no named profiles. Each slot is `{provider, typed common keys, settings}`. Every LLM slot states `timeout_s`. |
| D-107 / ADR-083 | `docs/adr/ADR-083-managed-local-mongod.md` | Opt-in `launch.mode: managed` for the MongoDB provider. It downloads only with consent and SHA-256 checks, runs as a one-member replica set on loopback, and is host-local. |
| Graph backends | RM-96 | Work on one graph backend at a time; a deployment runs one graph client. |
| Local MongoDB | RM-112 spike, `docs/provider_verification/rm112-local-mongo-options-2026-10-23.md` | Only the official `mongod` works; PoloDB, Mongita, MontyDB, pymongo_inmemory and FerretDB are rejected. The library-based local default stays SQLite records plus filesystem documents. |
| Dependencies | RM-02 | uv ≥ 0.13.1 (0.13.5 installed). The 15 bumps landed and the audit reports 0 outdated. The `[decider]` Hub `<2` cap is being upgraded (T-07). |

## 5. What landed (by register ID; details and evidence are in each row)

| ID | Commits |
|---|---|
| RM-117 model slots | `a256f0b6`, `cff4d5f6`, `80490893`, `f87fc750`, `826aac3a`, `2a89b6c1`..`27ca26d2`, `e0d5455c`, `df3ca0a6`, `778ccaca` |
| RM-118 follow-ups | `28236a18` |
| RM-02 uv, lock and dependency refresh | `c375ffa3`, `6cbeacb8`, `5d132e89` |
| RM-112 non-SQL stores | `e73c7356` (spike), `79394028` (S3, Weaviate), `473c3f48` (MongoDB records and GridFS) |
| RM-119 managed mongod | `bda02f6b` (ADR), `94fbc67d` |
| RM-120 graph mirror removed | `655ac7f9` |
| RM-113 item 18 and baselines | `d8540742`..`ba507023` |
| RM-96 graph walk pushdown | `2669499b` (LadybugDB), `a64958fd` (Neo4j) |
| Hygiene | `d8f3d3ef` (test isolation), `c70a6e64` (stray `uv` file), `4197214b` (stray tantivy index, now ignored) |

## 6. Pitfalls learned (save yourself the time)

- **Shallow test helpers:** an override such as `model={"llm": ...}` replaces the helper's
  whole `model` block. That drops the hash embedder, and the test then quietly loads real
  fastembed. Merge overrides per slot. A probe that records real fastembed loads found two of
  these (`778ccaca`, `e0d5455c`).
- **Shared process registry:** weights are shared across Engines in a process (ADR-079). A
  test that checks load counts can be polluted by an earlier test in the same chunk. When a
  test fails only in a chunk, bisect by directory.
- **PowerShell:** inline `python -c` with quotes, `<` or backticks breaks. Write a script file
  instead. A native command's stderr redirected with `2>&1` sets `$?` false even on success.
- **Windows paths:** long worktree paths break `git worktree add` and some unzip steps. Use
  short paths such as `C:\Users\hembad\Documents\GitHub\mspreNN`.
- **Merging agent work:** cherry-pick. For tables in the tracker and the status doc, resolve
  conflicts row by row. Regenerate generated files instead of merging them.
- **Stray artefacts:** test runs can leave `memspine.db.tantivy/` in the repo root. It is now
  git-ignored. Check `git status` before committing an agent's work.
- **Status table:** the live per-provider status is `docs/CLIENT_ADAPTER_FACTORY_STANDARD.md`
  section 2a. The owner asked for it to be kept current after every merge, with one row per
  slot and provider.
