# Trace Inspector: runbook (draft)

Draft written with TI-6. TI-7 completes it once the recorder (TI-1), dumper (TI-2), assembler (TI-3),
HTML viewer (TI-4) and step mode (TI-5) have landed. Plan: [`TRACE_INSPECTOR_PLAN.md`](TRACE_INSPECTOR_PLAN.md);
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

The local HTML viewer (TI-4) opens a bundle in six views: Conversation, Window, Record and links graph,
Question, Compare and Checks. It is self-contained and makes no network calls. A demo page can be built
from the invented demo conversation, `evals/memspine_evals/inspect/demo_conversation.json`.

To be completed by TI-7 with the exact build command and a per-view description tied to the stage
catalogue.

## Step

The step mode (TI-5) runs an engine in-process with the inspect hooks on and pauses after each stage,
for one turn, one session, one sleep or one question. It appends to a bundle as it goes, so a stepped
session opens in the viewer. `--replay-cache` serves model calls from the recorded call cache so no
model is needed.

To be completed by TI-7 with the command reference.

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

1. emit a tap event that carries the correlation keys (run, item, and turn or query) and ids for what
   it read and wrote, with free text only through the opt-in content channel;
2. be listed in the stage catalogue with its event name and join ids;
3. appear in the bundle (a new file or new fields on an existing row; never a renamed field);
4. have an invariant in the checks if it has one worth keeping.

A test will enforce this by matching the stage list against the registered trace events and failing
when a stage has no event. That test is not written yet; until it lands, reviewers check the rule by
hand.
