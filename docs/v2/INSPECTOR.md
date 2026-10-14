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
| `session` | write the rest of the current session, then close its windows and the session (call 2) |
| `sleep` | write any remaining turns, then run the sleep cycle and print it stage by stage (`job.stage.start`, `job.stage.item` per output, `job.stage.end`) |
| `ask <qid\|text>` | one read: plan, each leg's top-k with gold turns marked `*`, fusion, cuts, pool, rerank components, headers, the packed context, then reader and judge when `--reader-model` is given; `ask` alone lists the questions |
| `show <rid\|window_id\|occ\|turn_id>` | one object: record fields and envelope, a window with its envelopes, or an envelope's journey |
| `links <rid\|turn_id>` | lineage parents and children, supersede chain, graph edges |
| `diff` | the store delta since the last diff: new records, status changes, edge count |
| `save` | write the inspect bundle to `<out>/<run_id>--bundle/`, run the checks and print their summary |
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
