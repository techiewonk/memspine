# Tuning Studio: check, manage and tune every stage from the web (2026-10-14)

**Goal (owner):** check, test and tune every stage from a web page (an artifact), in controlled steps with verification. That covers every parameter, prompt and extraction, both before and around benchmark runs.

**Decisions (owner, 2026-10-14):**
- The Claude desktop app is used.
- A local `memspine-studio` MCP server is added to both the desktop app config and Claude Code (user scope).

## How it works

```
Claude desktop app
  └─ artifact "MemSpine Tuning Studio"  (claude.ai page, no dataset text inside)
        └─ claude.use("mcp") → host:memspine_studio  (tool input/output stay on this device)
              └─ memspine-studio MCP server (local, stdio, Python)
                    └─ StepSession (evals/memspine_evals/inspect/step.py) → in-process MemSpine engine
                          ├─ inspect hooks (TI-1) → per-stage events
                          ├─ snapshot / assemble / checks (TI-2/3/6)
                          ├─ stage → knob catalogue (TI-8, docs/v2/data/stage_params.json)
                          └─ GPU queue + ledger (evals/run_v2_batch.sh, v2_ledger.py)
```

- **In a browser tab**, `host:` calls fail. The page then falls back to a **demo mode** that uses the invented conversation embedded in the page. It is read-only for learning the controls.
- **LoCoMo text** reaches the page only at view time through the local server. It is never stored in the artifact, its database or its assets.
- **Writes are approved:** tools that change state are not annotated read-only, so the desktop app may ask you to confirm each one.

## Packages

### TS-1 · MCP server
Files: `evals/memspine_evals/studio/server.py` and `api.py`.

- **Protocol:** stdio JSON-RPC MCP, covering `initialize`, `tools/list` and `tools/call`. It is implemented in-house, with no new dependency (the shared venv has no `mcp` package and is not modified).
- **Session:** one `StepSession` per open session.
- **Results:** every tool returns structured JSON, never terminal text. Each result carries a `log_id`, and the call is appended to `tuning.jsonl`.
- **Read-only tools** carry the `readOnlyHint` annotation.

### TS-2 · Studio artifact
- **Panels:** a stage map (all 86 stages) and a knob panel per stage, built from `stage_params.json`. Other panels cover:
  - writing messages, interactions and sessions
  - asking one, several or any question
  - the window and envelope journey
  - the question funnel and waterfall
  - diffs from rerun and sweep
  - the verify panel
  - tuning history
  - export and screen
- **Shell behaviour:** it uses the `mcp` capability with `host:memspine_studio` only, plus a demo fallback.

### TS-3 · Verification loop
`studio.verify` runs three things:

- the TI-6 checks on the current bundle;
- the targeted question set (an id list, a failure slice name, or `cat:n`) under the current overrides;
- the same set under the baseline arm (cached).

It returns fixed / broken / unchanged counts, gold survival per stage, and the check failures. **Promote rule in the page:** "export arm → queue sample screen" is enabled only when verify shows 0 check errors and broken ≤ fixed.

### TS-4 · Registration and docs
- **Registration:** `scripts/install_studio_mcp.py` adds the server to `%APPDATA%/Claude/claude_desktop_config.json` (`mcpServers.memspine-studio`) and to Claude Code with `claude mcp add --scope user`. It backs up the original config, and is idempotent.
- **Docs:** `docs/v2/TUNING_STUDIO.md`.

## Tool contract (v1)

| Tool | Read-only | Input | Output |
|---|---|---|---|
| `studio_status` | yes | – | session, arm, data, item, mode, cursor, counts (turns written, sessions, windows open, records), overrides, prompt overrides, last verify, GPU queue state |
| `studio_open` | no | data (`demo` / path / `locomo`), item, arm, mode (`offline` / `replay` / `live`) | status |
| `studio_reset` | no | keep_overrides (bool) | status |
| `studio_load_conversation` | no | json (conversation in the demo shape) | status |
| `studio_stages` | yes | – | `[{stage_id, name, path (write/sleep/read), file_function, events[], gap, n_params}]` |
| `studio_params` | yes | stage | `{stage_id, keys[{key, value, default, type, choices / range, description, effect, rebuild, screens[]}], prompts[], models[]}` |
| `studio_config` | yes | prefix?, diff? | `[{key, value, default, source}]` |
| `studio_set` | no | key, value | `{ok, applied: live / needs_reset, old, new, error?}` |
| `studio_unset` | no | key | same |
| `studio_prompts` | yes | – | `[{prompt_id, role, version, overridden}]` |
| `studio_prompt_show` | yes | prompt_id, rendered? | `{prompt_id, text, rendered_example?}` |
| `studio_prompt_set` | no | prompt_id, text | `{ok, diff}` |
| `studio_prompt_reset` | no | prompt_id | `{ok}` |
| `studio_write` | no | `{mode: say, messages[{speaker, text, ts?, session?}]}` or `{mode: turns, ids[] / range}` or `{mode: interaction, n}` or `{mode: session, n / ids[]}` | `{turns[{turn_id, custom, stages{stamp, record, index, window, extract, envelopes[], deposits[], links[]}}], summary}` |
| `studio_sleep` | no | – | `{stages[{stage, items[], end}], model_calls}` |
| `studio_ask` | no (writes access stats) | questions[ qid / text / {text, gold, turns} ], detail (full / brief) | `{results[{qid, plan, legs{}, fused[], cuts[], pool[], rerank[], headers[], packed[], context, answer, verdict, gold_path[], first_losing_stage}], summary[]}` |
| `studio_rerun_window` | no | window_id, commit? | `{diff{items_added, items_removed, items_changed, stage_changes[], verdict_changes[]}}` |
| `studio_rerun_ask` | no | q | `{before, after, diff}` |
| `studio_sweep` | no | key, values[], questions | `{table[{value, gold_found, first_losing_stage, correct}]}` |
| `studio_show` | yes | id (rid / window / occ) | object |
| `studio_links` | yes | rid | links, parents, children, supersede chain |
| `studio_verify` | no | questions (ids / slice / cat), baseline? | `{checks{errors, warnings, failing[]}, counts{fixed, broken, unchanged}, funnel[], per_question[]}` |
| `studio_history` | yes | limit? | tuning.jsonl rows |
| `studio_export_arm` | no | name | `{path, diff_vs_base}` |
| `studio_screen` | no | arm, slice, mode (sample) | `{queued, eta_min}`. Refused while the GPU is reserved, or when verify has not passed. |
| `studio_screen_status` | yes | – | the queue log tail and ledger rows |

All text fields that can carry dataset text are returned only to the local caller. The artifact page never persists them.
