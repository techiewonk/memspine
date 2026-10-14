# Trace Inspector: runbook (draft)

How to record, view, step and check a run, and the rule for adding a stage. Plan: [`TRACE_INSPECTOR_PLAN.md`](TRACE_INSPECTOR_PLAN.md);
stage list: [`PIPELINE_STAGE_INVENTORY.md`](PIPELINE_STAGE_INVENTORY.md).

This page contains no dataset text. Bundles that hold real text live only under the gitignored
`evals/runs/_analysis/inspect/`.

## Record

A run is recorded into one **inspect bundle** directory, `evals/runs/_analysis/inspect/<run_id>/`.
The engine emits tap events when `observability.inspect` is on (TI-1); the dumper snapshots the
store after ingest and after sleep (TI-2); the assembler joins both with the harness rows (TI-3).

The bundle is a set of jsonl files, each optional for the tools that read it:

| File | One row per |
|---|---|
| `turns.jsonl` | dataset turn |
| `windows.jsonl` | extraction window |
| `envelopes.jsonl` | assertion envelope per stage |
| `records.jsonl` | stored record, per phase (`post_ingest`, `post_sleep`) |
| `links.jsonl` | lineage or graph edge |
| `sleep.jsonl` | sleep stage item |
| `questions.jsonl` | question |
| `checks.jsonl` | check result (written by the checks, below) |
| `manifest.json` | the run (config hash, arm, commit, counts, run time) |

Field names are fixed by section 1 of the plan; fields may be added, never renamed. Every row carries
`run_id` and `item`. Join keys: `turn_id` and `record_id`; `window_id` to member records; `occ` to
`record_id`; `record_id` to parents and links; `query_id` to candidate record ids to turns.

## View

Build the page from a bundle, or straight from a run folder:

```
python evals/build_inspector.py evals/runs/_analysis/inspect/<run>/                  # an assembled bundle
python evals/build_inspector.py --run <run> --data <locomo.json|demo> [--compare <run2>] [--out DIR]
python evals/build_inspector.py --demo                                              # the invented conversation
```

The output is one self-contained `index.html` in the bundle folder (inline JS and CSS, data embedded, no
network call). Six views; each one reads the bundle files named below and shows the stages of the catalogue
(`PIPELINE_STAGE_INVENTORY.md`, ids in brackets):

| View | Reads | Shows | Stages |
|---|---|---|---|
| Conversation | `turns`, `records`, `windows` | every turn with its stored record, flags (skipped, quarantined, not indexed, dropped) and the window it fell in | A1-A14, A15 |
| Window | `windows`, `envelopes` | member turns, rendered prompt, raw reply, parsed sections; each envelope's journey through stages 100-500 with the drops and reasons highlighted | A15-A23 |
| Record and links | `records`, `links`, `sleep` | one record with parents, children, graph neighbours, supersede chain and value history (inline SVG force layout) | A24-A28, B2-B14 |
| Question | `questions` | a waterfall: plan, each leg (gold turns marked), fusion, cuts, pool, rerank, headers and pack, window, context, reader, judge; a candidate-count funnel with the first losing stage named | C1-C34 |
| Compare | `compare.json` | the same turn or question under two runs, side by side, differences highlighted | all |
| Checks | `checks` | the invariant failures, filterable, each linked to its turn, window or question | all |

A stage whose events are still missing shows as empty in its view. The list of such stages is
`docs/v2/data/stage_trace_registry.json` (entries with `"gap": true`), so a blank panel can be told apart from a
bug.

## Step

Step mode runs one conversation through an engine in-process, one stage at a time, with the inspect hooks on.
It builds the arm's engine through the same harness code as an eval run (`MemspineSystem`: same config, models
and write path), taps the inspect events into memory, and prints what each step produced, grouped by stage.
Every step also appends to a run folder, so a stepped session can be saved, checked and opened in the viewer.

```
python -m memspine_evals.inspect step --data <locomo json | demo> --item <id> --arm <name | path.json>
    [--offline] [--replay-cache --cache-dir DIR [--live]] [--out DIR] [--script FILE]
    [--reader-model M [--judge-model M] [--base-url URL]] [--budget N] [--top-k N] [--pause] [--wide]
```

- `--arm` is an `evals/arms` name (`scr-gc3-graph-jina`, `scr-b8-write-v2`), a path to an arm json, or `demo`
  (a small CPU arm with the write-v2 stages on, for the invented conversation).
- `--offline` swaps the heavy parts: hash embedder, no reranker, a stub LLM (no network, no GPU).
- `--replay-cache --cache-dir DIR` serves engine-role completions and embeddings from a recorded `call_cache.py`
  store. A cache miss is listed and answered by the stub (or hash embedder) and is never stored; with `--live` it
  goes to the arm's real endpoint. An arm that embeds with a local model (`st`) cannot be replayed from the cache
  (those vectors are not cached); replay and offline use the hash embedder and print a note.
- `--out DIR` is the run-folder root (default `runs/_analysis/inspect/step`, gitignored: real runs hold benchmark
  text). `--script FILE` runs the commands of a file, one per line, and exits (for tests and CI).
- `--pause` waits for Enter between printed stages; `--wide` prints full prompts, replies, contexts and event
  lists instead of the first lines.

Commands (Enter alone is `turn`):

| Command | Does |
|---|---|
| `turn [n]` | write the next turn(s) and print the stages it went through: stamp and role, record and index (`store.index.record`: vector, lexical, graph, edges), window membership and extraction (`write.window.members`, `write.extract.io`), envelope stages (`write.envelope.trace`, one line per envelope: `occ <id> compile:added temporal:kept grounding:dropped ...`), deposits and the conflict ladder, links |
| `turn <turn_id>`, `turns D1:1-D1:6`, `turns D1:3,D2:5` | write the selected dataset turns, out of order if you like (a warning says when the order differs from the dataset or from what is already written); a turn already written is skipped |
| `skip [n]`, `goto <turn_id>` | move the cursor without writing |
| `say <Speaker>: <text> [@YYYY-MM-DD[ HH:MM]] [#session]` | write a custom message, not from the dataset: id `U<n>`, session = the last written turn's (or `#id`), time = the last turn's time + 1 minute (never the wall clock). `say <<` then lines then a lone `.` writes several |
| `interaction [n]` | write the next n interactions: a turn plus the run of turns right after it by the next speaker (user message + assistant reply); stages per turn, then a combined summary |
| `session` | write the rest of the current session, then close its windows and the session (call 2) |
| `session <n>`, `session <id>[,<id>]`, `sessions D1-D3` | the next n sessions, or the named ones (all their unwritten turns), each followed by window flush and session close |
| `sleep` | write any remaining turns, then run the sleep cycle and print it stage by stage (`job.stage.start`, `job.stage.item` per output, `job.stage.end`) |
| `ask <qid\|text>` | one read: plan, each leg's top-k with gold turns marked `*`, fusion, cuts, pool, rerank components, headers, the packed context, then reader and judge when `--reader-model` is given; `ask` alone lists the questions |
| `ask q1,q3,q7`, `ask all`, `ask cat:<n>`, `asks <file>` | several reads (a file: one question per line, an id or free text), then a SUMMARY TABLE: qid, category, gold turns found/total, first losing stage, answer correct (or gold text in context when there is no reader) |
| `ask <text> \| gold: <answer> \| turns: D1:3,D2:5` | an ad hoc question with its own gold to check against; kept in the bundle as a custom question |
| `brief on\|off`, `ask ... --brief` | one line per question instead of the full detail |
| `status` | what has been written (turns, custom turns, sessions open or closed, windows, records), the cursor, tuning overrides |
| `reset`, `load <file.json>` | a fresh engine with the same arm (overrides kept), nothing written; or load a conversation in the `demo_conversation.json` shape in place of the dataset item |
| `show <rid\|window_id\|occ\|turn_id>` | one object: record fields and envelope, a window with its envelopes, or an envelope's journey |
| `links <rid\|turn_id>` | lineage parents and children, supersede chain, graph edges |
| `diff` | the store delta since the last diff: new records, status changes, edge count |
| `save` | write the inspect bundle to `<out>/<run_id>--bundle/` (custom turns and questions carry `custom: true`; `tuning.jsonl` when you tuned), run the checks and print their summary |
| `config`, `set`, `unset`, `params`, `prompt`, `rerun`, `sweep`, `export` | tuning; see [Tuning](#tuning) |
| `quit` | leave |

Example (demo data, offline), abridged:

```
$ python -m memspine_evals.inspect step --data demo --arm demo --offline --script cmds.txt
--- step: item demo-1 | arm demo | mode offline -----------------------------
step> turn 3
--- turn 1/30: D1:1  session D1  Bram  [2024-03-02] ------------------------
--- stamp / role -----------------------------------------------------------
  write.role.turn  role=user derive_use=stated src_class=user_stated trust_cap=0.7
  write.firewall.verdict  id=4fc13b4e-... trust=0.7 quarantine=false
--- record / index (store.index.record) ------------------------------------
  index  rid=4fc13b4e-... turn=D1:1 vector=yes lexical=yes graph_node=yes edges=0 text_len=43
step> session
--- window membership / extraction -----------------------------------------
  window 6ad363d78068 session=D1 cut=flush prepass=off
    members: turns ['D1:1', ... 'D1:10']  records 10
  extract.io window=6ad363d78068 model=stub/window tokens={'prompt': 855, 'completion': 704, ...}
--- envelope stages (write.envelope.trace) ---------------------------------
  occ oc_d784... [6ad363d78068] compile:added temporal:rewritten(time) grounding:rewritten(checks) integration:rewritten(verdict) deposit:kept
      -> record 2504e300-...
  occ oc_b516... [6ad363d78068] compile:added temporal:kept grounding:dropped
step> ask q1
--- legs (gold marked *) ---------------------------------------------------
  lexical top10/21: 1:D2:7(1.117) 2:D1:1*(1.007) ...
--- packed context ---------------------------------------------------------
  gold turns in context: ['D1:1'] of ['D1:1']
step> save
  checks: 597 rows, 0 error(s), 0 warning(s)
```

Step mode flushes closed windows after every turn (an eval run flushes before a question and at build), so
the windows of a stepped session are cut at the same places but their calls happen earlier.

## Manual testing recipes

The owner's four needs, as commands (demo data, `--offline`; type them at the `step>` prompt or put them in a `--script` file).
Everything lands in the run folder, so `save` plus `build_inspector.py` show custom messages and questions too.

| Need | Command(s) |
|---|---|
| 1. one or many messages, then check | `say Bram: I adopted a hedgehog called Spike. @2024-03-02 10:30`; several: `say <<` / lines / `.`; dataset turns: `turn D1:3`, `turns D1:1-D1:6`, `turns D1:3,D2:5`; then `status`, `show U1`, `diff` |
| 2. one or many interactions, then check | `interaction` (one), `interaction 3` (three); `status`, `diff` |
| 3. one or many sessions, then check | `session` (rest of this one), `session 2` (next two), `session D2`, `sessions D1-D3`; `status`, `diff` |
| 4. retrieve one / several / any | one: `ask q1` or `ask Where does Bram live?`; several: `ask q1,q3,q7`, `ask cat:list`, `asks questions.txt`; any: `ask all`, or free text with its own gold: `ask Who has a hedgehog? \| gold: Bram \| turns: U1` |

A custom message and a question about it:

```
step> say Bram: I adopted a hedgehog called Spike. @2024-03-02 10:30
--- say: U1  session D1  Bram  [2024-03-02 10:30]  (custom) ------------------
  text: I adopted a hedgehog called Spike.
  deposit: records=1 model_calls=0 ids=['5ecc60f4-...']
step> say <<
Ilse: Spike sounds adorable, where did you find him?
Bram: At the shelter in Utrecht near the station.
.
--- say: U2  session D1  Ilse  [2024-03-02 10:31]  (custom) -------------------
--- say: U3  session D1  Bram  [2024-03-02 10:32]  (custom) -------------------
--- summary: 2 custom messages -------------------------------------------------
step> ask Who has a hedgehog? | gold: Bram | turns: U1
--- ask adhoc-1 [adhoc]: Who has a hedgehog? -----------------------------------
  gold: Bram   gold turns: ['U1']
  lexical top6/6: 1:U1*(4.338) ...
  gold turns in context: ['U1'] of ['U1']
```

Selected turns, an interaction, then a summary table:

```
step> turns D1:6,D1:4
  warning: the selection is not in dataset order; writing it as given
step> interaction
--- summary: interaction D1:1 (Bram) -> D1:2 (Ilse) ----------------------------
  turns=2 records=2 model_calls=0
  events by stage: stamp / role=16, record / index=2
step> ask q1,q5,q7
--- SUMMARY TABLE (3 question(s)) ----------------------------------------------
  qid  category    gold found  first losing stage  answer correct
  ---  ----------  ----------  ------------------  ---------------------
  q1   single_hop  1/1         -                   gold text in ctx: yes
  q5   nickname    1/2         not written (1)     gold text in ctx: yes
  q7   supersede   1/2         not written (1)     gold text in ctx: yes
```

"First losing stage" is `not written (n)` when a gold turn was never written, else the first stage of the gold
path that dropped it (`recall`, `fusion`, `gate`, `rerank`, `assembly`), the same logic as the bundle's gold path.
`reset` starts over without leaving the session; `load <file.json>` swaps the conversation.

## Tuning

Step mode is also for tuning every step by hand before a benchmark run. All of it works offline on the demo.
Every `set`, `unset`, `prompt`, `rerun`, `sweep` and `export` appends a row to `tuning.jsonl`
(`ts, command, key, old, new, effect`), which `save` copies into the bundle for the viewer.

| Command | Does |
|---|---|
| `config [prefix]`, `config diff` | the effective engine config with each value's default and source (`default`, `arm`, `template`, `override`); `diff` shows only values that differ from the defaults |
| `set <key>=<value>`, `unset <key>` | a live override, validated by the config schema (pydantic); the value is JSON (`0.9`, `true`, `[1,2]`) or a string. Keys the engine reads per call are applied to the running engine (the scoring, assembly and compression policies are re-bound). A key a stage binds at start (`embedding`, `storage`, `llm`, `memories`, and the `write.*` keys `stage_params.json` marks `rebuild`) is recorded and the reply says so: `reset` rebuilds the engine with the override kept |
| `params <stage-id\|name\|prefix>` | the keys that affect a stage with current value, default, choices or range, description, plus the stage's prompts and model roles, from `docs/v2/data/stage_params.json`; without that file, a prefix match on the config schema |
| `prompt list`, `prompt show <id>` | the registry prompts plus the code-constant ones (`structured`, `structured.window`, `structured.repair`) and `reader.qa` |
| `prompt edit <id> <file>`, `prompt reset <id>` | override a prompt for this session. A registry prompt (`window_extract`, `extract_edges`, `summarize`, `plan*`, `sufficiency` ...) is changed in place: the file is the body, or the shipped format (`system:` front matter, `---`, body). A code constant is replaced in the model call (the file is the whole system text; `structured.repair` is the user message with `{n}` and `{problems}`); `reader.qa` is the reader's QA prompt (needs `--reader-model`) |
| `rerun window <window_id> [--commit]` | run extraction and the envelope stages again for one window with the current params and prompts, in a scratch copy (extraction cache off, events kept out of the bundle), and print the diff against the original run: items added, removed, changed, stage status changes, integration verdict changes. `--commit` deposits the result (a second copy: `reset` to clean up) |
| `rerun ask <qid\|text>` | a read again with the current params, diffed against that question's previous run: gold ranks per leg, fusion, pool, rerank, final, packed context lines, first losing stage, answer |
| `sweep <key> <v1,v2,...> ask <q1,q2\|all\|cat:n>` | the reads once per value, then a table of value x gold found / first losing stage / correct; read-side keys only (`read.*`, and not ones needing a rebuild); the key is restored afterwards. A write-side key is refused with a hint to use `rerun window` |
| `export arm <name>` | write the original arm plus the overrides to `evals/arms/<name>.json` (validated with `validate_engine_config`; registry prompt edits go in as `prompts.overrides`, all edits also as files in `evals/arms/<name>.prompts/`) and print the diff against the base arm, ready for the GPU queue |

Offline, the stub extractor reads one directive from the system prompt so a prompt edit has a visible effect: a line
`[stub: min_len=N]` sets the shortest line it turns into an item (default 24).

Recipes (demo, offline):

```
step> session D1
step> ask q1
step> set read.assembly.relative_floor=0.9
  read.assembly.relative_floor: 0.3 -> 0.9   applied to the running engine
step> rerun ask q1
  gold found                               1/1                   1/1
  tokens                                   109                    76   <== changed
  packed context: -1 line(s), +0 line(s)
    - [2024-03-02] Ilse: Congratulations! Are you still working at the bakery?
step> sweep read.assembly.relative_floor 0.1,0.5,0.9 ask q1,q7
  read.assembly.relative_floor  gold found  first losing stage  correct
  ----------------------------  ----------  ------------------  -------
  0.1                           2/3         not written (1):1   1/2
step> sweep write.window_extract off,facts ask q1
sweep refused: 'write.window_extract' is not a read-side key (read.*). ... use `set write.window_extract=<value>` then `rerun window <window_id>`
step> params fusion
--- params C16: Fusion ---------------------------------------------------------
  read.fusion = rrf  default rrf  type Literal['rrf', 'minmax']  choices ["rrf", "minmax"]
step> export arm my-screen
  diff vs the base arm (demo): 1 value(s)
    read.assembly.relative_floor: (absent) -> 0.9
```

Edit an extraction prompt and see what changes in one window:

```
step> prompt show structured.window            # copy the text to a file, change it
step> prompt edit structured.window my_window_prompt.txt
step> rerun window 17158af3c87c
--- rerun window 17158af3c87c: 10 turn(s)  (scratch: nothing deposited) -------
  items before 8, after 1: +0 added, -7 removed, ~0 changed, =1 same
  - Bram: I finally moved to Utrecht last week.
  ...
step> prompt reset structured.window
```

The window ids are in the `window <id>` lines that `session` and `turn` print. Integration verdicts in a rerun are
judged against the live store, which already holds the window's first run, so a verdict change on an unchanged item is
expected; compare the items and the stage statuses first.

## Registry

`docs/v2/data/stage_trace_registry.json` maps every stage of the catalogue (A1-A28, B0-B17, C1-C34) to the tap
events that trace it, lists the envelope stages (`ingest/windows.py:_envelope_stages`) and the sleep stages
(`workers/schedule.py`) against their catalogue ids, and marks stages that still lack events with
`"gap": true` and the inventory gap number (F1-F17). `harness: true` stages live in the evals logs, not the tap.
`tests/unit/observability/test_stage_trace_coverage.py` checks it against the source.

## Checks

`evals/memspine_evals/inspect/checks.py` runs a fixed set of invariants over a bundle:

```
python -m memspine_evals.inspect.checks <bundle_dir> [--fail-on error|warn|never]
```

It writes `checks.jsonl` (one row per inspected object: `check_id`, `scope`, `id`, `ok`, `detail`, and
the additive `item`, `level`, `status`) and `checks_summary.md` (per item: ok and fail counts per check
and the first 10 failing ids). The exit code is non-zero when a check at the requested level fails;
`--fail-on error` (the CI setting) ignores warnings. The default is `never`.

A check whose input files are missing, or that finds no row to test, reports `status: skipped` and
never fails. So a partial bundle (for example turns only) is still checkable.

| Check | Level | Invariant |
|---|---|---|
| `bundle_wellformed` | error | every line of every file is a JSON object |
| `turn_written_once` | error | each turn appears once, has a record unless quarantined or dropped, and no record is shared by two turns |
| `turn_in_vector_index` | error | every written turn is vector-indexed |
| `turn_in_lexical_index` | error | every written turn is lexically indexed, when hybrid is on |
| `window_has_members` | error | every non-skipped window has member turns |
| `window_call_outcome` | error | every non-skipped window records a call outcome |
| `window_sections_counted` | error | every non-skipped window has counts for facts, edges and episode |
| `window_single_session_at_session_end` | error | a window cut by session end does not span two sessions |
| `envelope_terminal` | error | every envelope ends deposited with a record, or dropped with a reason |
| `supersede_has_target` | error | an envelope that supersedes names a target |
| `derived_has_live_parent_and_channel` | error | every derived record has a channel and a live parent |
| `derived_parent_turns_in_item` | error | a derived record's parent turns belong to its own item |
| `no_parent_cycles` | error | the parent relation is acyclic |
| `supersede_target_and_time_order` | error | a superseded record points at a stored successor with a consistent valid time |
| `duplicate_record_content` | warn | one turn does not yield two derived records with the same content |
| `no_wall_clock_in_derived` | error | no derived record is stamped with the run date instead of its source date (needs a run time in the manifest) |
| `graph_edge_endpoints_exist` | error | every edge endpoint is a stored record, a turn or a declared node |
| `sleep_idempotent` | error | a later sleep cycle over the same stage and session adds nothing |
| `question_has_plan` | error | every question has a plan |
| `question_has_leg` | error | every question ran at least one leg |
| `question_context_within_budget` | error | packed context tokens do not exceed the budget |
| `gold_path_matches_stage_lists` | error | each gold turn's last stage matches the question's own stage lists, the attribution `forensics_report.py` uses |

To add a check, write a function that yields `(item, id, ok, detail)` per inspected object, give it a
docstring that starts with `Invariant:`, and register it with `@check(check_id, scope, needs=(...))`.
`evals/tests/test_inspect_checks.py` requires a passing and a failing hand-built bundle for every
registered check.

## Adding a stage

The stage catalogue (`PIPELINE_STAGE_INVENTORY.md`) is the list of phases the inspector can show.
**Rule: every new stage emits its trace event, or the change is not done.** A stage that is added to
the write, sleep or read path must:

1. emit a tap event that carries the correlation keys (run, item, and turn or query; use
   `inspect_scope` at the entry point and `observability.inspect` helpers) and ids for what it read and
   wrote, with free text only through the opt-in content channel (`inspect.ic(...)`);
2. be listed in the stage catalogue (`PIPELINE_STAGE_INVENTORY.md`) with its event name and join ids;
3. be added to `docs/v2/data/stage_trace_registry.json`: a row under `stages` with the event names that
   trace it. A new envelope stage also goes under `envelope_stages`, a new sleep stage under `sleep_stages`.
   A stage that cannot emit yet is registered with `"gap": true` and the F-number of its gap, so it shows
   up on the list;
4. appear in the bundle (a new file or new fields on an existing row; never a renamed field) and in the
   assembler (`assemble.py`) and, if it has an invariant worth keeping, in the checks.

The test `tests/unit/observability/test_stage_trace_coverage.py` enforces this and fails when:

- an inventory row has no registry entry (or the registry has a row the inventory lacks);
- a registered event name is not emitted by any `tap.event` / `tap.span` / `tap.request` call in `src`;
- an envelope stage built in `ingest/windows.py:_envelope_stages`, or a sleep stage in
  `workers/schedule.py` (`SLEEP_CYCLE_ORDER` and the `*_STAGE` constants), has no registry entry;
- a stage has neither events nor a listed gap.
