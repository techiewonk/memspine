# Tuning Studio: local MCP server (TS-1, TS-3, TS-4)

The studio lets you check, test and tune every pipeline stage from the Claude desktop app, in controlled steps with verification. A local stdio MCP server (`memspine-studio`) holds one step session (the Trace Inspector's `StepSession`) and exposes it as 26 tools. The design and the tool contract are in [TUNING_STUDIO_PLAN.md](TUNING_STUDIO_PLAN.md).

## Install

Register the server once, from the repo root, with the shared venv's python:

```
python scripts/install_studio_mcp.py --dry-run            # print the entry, the diff and the `claude mcp add` command; change nothing
python scripts/install_studio_mcp.py --apply-desktop      # edit %APPDATA%/Claude/claude_desktop_config.json (backup .bak-<timestamp> first)
python scripts/install_studio_mcp.py --apply-claude-code  # claude mcp add --scope user memspine-studio ...
```

- **Idempotent:** an identical entry is left alone (no write, no new backup). All other keys of the config are kept.
- **Paths:** the entry runs from the MAIN restructure checkout (`C:/Users/hembad/Documents/GitHub/memspine-restructure`, `--root` to change) with `C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe`. It is never pointed at a worktree.
- **Entry written:** `command` = the venv python, `args` = `["-m", "memspine_evals.studio.server"]`, `env.PYTHONPATH` = `<root>/src;<root>/evals`.
- **GPU screens:** add `--allow-gpu` to the installer only if you want `studio_screen` to be able to launch the queue (see Safety).
- **After installing:** restart the desktop app. Nothing is installed into the venv; the server uses no package beyond the evals harness.

Run it by hand to check: `python -m memspine_evals.studio.server [--data demo|PATH|locomo] [--out DIR] [--allow-gpu] [--arms-dir DIR] [--logs-dir DIR]`. It starts in about 0.2 s; the engine is built on the first `studio_open`. Stdout carries protocol messages only; logs go to stderr.

## Using it from the desktop app

1. Ask Claude to open a session: `studio_open` with `data: "demo"`, `mode: "offline"` (hash embedder, stub model, no network, no GPU). For LoCoMo use `data: "locomo"` (or a path) with `mode: "replay"` and a recorded `cache_dir`, or `live`.
2. Write: `studio_write` with `say` (your own messages), `turns` (ids, a range or n), `interaction` (n exchanges, 2 turns each) or `session` (n = the next n sessions from the cursor, or ids). Each turn comes back grouped by stage: stamp, record, index, window, extract, envelopes, deposits, links.
3. Ask: `studio_ask`. The result shows each leg with gold marked, fusion, cuts, pool, rerank, headers, the packed context, and `first_losing_stage`: the first stage where a gold turn drops out.
4. Change a knob: `studio_params` (stage) lists the keys with type, choices, range, rebuild scope and the screens that test them. `studio_set` applies it; `studio_rerun_ask` shows before, after and the diff. `studio_sweep` tries several values on a question set.
5. Verify: `studio_verify` (see below), then `studio_export_arm`, then `studio_screen` for a sample screen.
6. Every call is appended to `<out>/tuning.jsonl`; `studio_history` reads it back.

Writes are not annotated read-only, so the desktop app may ask you to confirm each one. The artifact page (TS-2) calls the same tools through `host:memspine_studio`.

### How changes apply

The server uses the step session's own tuning (`step_tune.py`, package TI-5b): a set is applied to the running engine without a rebuild when the catalogue says no index rebuild is needed. `studio_set` never claims more than it has confirmed. `applied` is one of:

- **`live_verified`:** the engine's live config object shows the new value AND the key is one the session rebinds on every set (`read.assembly.*`, `read.scoring.*`, `read.compression.*`). With `probe: true`, a change in the last asked question's answer also verifies the key.
- **`live_unverified`:** accepted, and the engine config may show it, but the key may be bound when the engine starts (TI-5b saw `rrf_k`, fusion, replay-window keys and `candidate_pool` do nothing on the demo). Confirm with `studio_rerun_ask` (compare before and after) or `studio_set` with `probe: true`; if nothing changes, `studio_reset`. `studio_sweep` notes when every value gave the same packed context.
- **`needs_reset`:** the catalogue marks the key as needing a rebuild (write and index scope). Call `studio_reset` with `keep_overrides: true` and `replay: true` to rewrite the same turns under the new setting.

Prompts (registry prompts, the code-constant prompts and `reader.qa`) apply to the next model call of that role, so `studio_prompt_set` returns `live_verified`. Records already written keep their old extraction: use `studio_rerun_window` to see what the new prompt would extract. A rejected key returns `ok: false` with the schema error and changes nothing. `offline` swaps the embedder for the hash embedder and turns the reranker off, whatever the arm says.

### Verify (TS-3)

`studio_verify(questions, baseline)` runs, in this order:

1. If the baseline for these questions is not cached, it is computed and cached (memory plus `<out>/baseline_cache/`). When every override is a live read-path key, the overrides are reverted in the running engine, the questions asked, and the overrides put back. Otherwise (a write-path key or a prompt edit) a separate session is built with no overrides, the writes are replayed, and your session is rebuilt afterwards. `baseline: "<arm>"` compares with that arm instead.
2. It asks the same questions under the current overrides.
3. It saves a bundle of what the session wrote and runs the `inspect.checks` invariants over it.

It returns `checks {errors, warnings, failing[]}`, `counts {fixed, broken, unchanged}`, a `funnel` (per stage, how many questions keep all their gold turns, with the baseline beside it) and `per_question`. A question is correct when the judge scored it, else when all its gold turns are in the packed context. `passed` means 0 check errors and `broken <= fixed`. Any later write, `set`, `unset` or prompt change makes the verify stale.

`questions` (also for `studio_ask` and `studio_sweep`) takes a list of ids, texts or `{text, gold, turns}`; or one string: a qid, `all`, `cat:<n>` (category n), `<category>[:count]`, or a slice name (a json in `evals/analysis/` or a path; its query ids for the open item). Default for verify: all questions of the item.

## Tool reference

Read-only tools are annotated `readOnlyHint`. Results are structured JSON; `structuredContent` also carries `log_id`. The three list tools return a bare list as text and `{result, log_id}` as structured content.

| Tool | Read-only | Input | Output |
|---|---|---|---|
| `studio_status` | yes | – | questions[{qid, category, question}], open, session, arm, data, item, mode, cursor, counts, overrides, prompt_overrides, stale, last_verify, gpu |
| `studio_open` | no | data, item, arm, mode, cache_dir? | status |
| `studio_reset` | no | keep_overrides, replay? | status |
| `studio_load_conversation` | no | json | status |
| `studio_stages` | yes | – | `[{stage_id, name, path, file_function, events[], gap, n_params}]` (86 stages; path write/sleep/read) |
| `studio_params` | yes | stage | `{stage_id, keys[{key, value, default, type, choices/range, description, effect, rebuild, rebuild_scope, screens[]}], prompts[], models[]}` from `docs/v2/data/stage_params.json` |
| `studio_config` | yes | prefix?, diff? | `[{key, value, default, source}]` |
| `studio_set` / `studio_unset` | no | key, value, probe? / key | `{ok, applied: live_verified/live_unverified/needs_reset, old, new, note, error?}` |
| `studio_prompts` | yes | – | `[{prompt_id, role, version, overridden}]` |
| `studio_prompt_show` | yes | prompt_id, rendered? | `{prompt_id, text, system, rendered_example?}` |
| `studio_prompt_set` / `studio_prompt_reset` | no | prompt_id, text / prompt_id | `{ok, diff}` / `{ok}` |
| `studio_write` | no | mode + messages / ids / range / n / session | `{turns[{turn_id, custom, stages{...}}], summary}` |
| `studio_sleep` | no | – | `{stages[{stage, items[], end}], pre, model_calls}` |
| `studio_ask` | no | questions, detail | `{results[{qid, plan, legs, fused, cuts, pool, rerank, headers, packed, context, answer, verdict, gold_path[], first_losing_stage}], summary[]}` |
| `studio_rerun_window` | no | window_id, commit? | `{diff{items_added, items_removed, items_changed, stage_changes[], verdict_changes[]}, extract_io}`; scratch unless `commit` |
| `studio_rerun_ask` | no | q | `{before, after, diff}` |
| `studio_sweep` | no | key, values[], questions | `{table[{value, gold_found, first_losing_stage, correct}]}` (live keys) |
| `studio_show` | yes | id | a record, window or envelope occurrence |
| `studio_links` | yes | rid | parents, children, supersede chain, graph edges |
| `studio_verify` | no | questions, baseline? | see above |
| `studio_history` | yes | limit? | rows of `tuning.jsonl` |
| `studio_export_arm` | no | name | `{path, diff_vs_base}`; writes `evals/arms/studio-<name>.json` |
| `studio_screen` | no | arm, slice?, mode, screen | `{queued, eta_min}` or a refusal with `reason` |
| `studio_screen_status` | yes | – | the latest `v2_*.log` tail and the newest ledger rows |

`studio_screen` runs `bash evals/run_v2_batch.sh <screen> <mode>`. The queue runs rows of `evals/v2_queue.tsv`, not bare arms, so the call also needs `screen` (a row id or batch such as `S01` or `B1`); add a row for the exported arm first. Only `sample` and `focus` are accepted; a final run is never launched from here.

## Safety

- **GPU gate:** `studio_screen` launches only if all hold: the server was started with `--allow-gpu`; `evals/runs/_logs/GPU_RESERVED` does not exist; the last `studio_verify` passed (0 check errors, broken <= fixed) and is not stale (no config or write change since). Otherwise it returns `{queued: false, refused: true, reason}` and starts nothing. The default install has no `--allow-gpu`.
- **No GPU or model calls otherwise:** `offline` uses the hash embedder and a stub LLM; `replay` serves recorded calls and stubs misses; only `live` reaches real endpoints.
- **Licence:** no LoCoMo or OP-Bench text is stored in committed files. Dataset text reaches the page only at view time through this local server; run folders, bundles and `tuning.jsonl` live under the gitignored `evals/runs/`. Tests and the demo use the invented conversation (Bram, Ilse, Noor, Tomasz).
- **Writes are approvals:** state-changing tools are not read-only, so the desktop app can ask before each one.
- **One session at a time:** the engine patches process-wide state, so the server keeps a single session; verify's baseline is run sequentially.

## Known gaps

- `studio_sweep` covers keys that apply without a rebuild; sweep a write-path key with `studio_set`, `studio_reset(replay)` and `studio_ask` per value.
- The REPL's `skip`, `goto` and `asks` table have no tool of their own: `studio_ask` with `all`, `cat:<n>` or a list covers the table, and `studio_write` with `ids` or `range` covers skip and goto.
- A rerun of one window is integrated against the live store, which already holds that window's first run, so verdict changes can include coexist noise.
