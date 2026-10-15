# Trace Inspector plan: every phase, every turn, every question (2026-10-14)

**Goal (owner):** check and maintain every phase, process and function, and analyse every session and every message:

- how it is extracted, stored and linked;
- how a question finds it;
- what the reader saw.

We want to do this manually (step by step) or by script, with a visualisation for each.

**Decisions (owner, 2026-10-14):**

| Decision | Choice |
|---|---|
| Viewer | Local HTML inspector plus CLI. LoCoMo text stays on this machine (gitignored `evals/runs/_analysis/`); a published demo uses an invented conversation. |
| Mode | Both a post-hoc replay viewer and a live step mode (feed one turn or question, pause after each stage). |
| Configurations | Reference (verbatim plus graph in sleep, `scr-gc3-graph-jina`) **and** write-v2 (`scr-b8-write-v2`: window extraction, grounding, resolution, integration, links), compared side by side for the same turns. |
| Data | One LoCoMo conversation first, then all ten. |

**Stage catalogue (what exists today):** [`PIPELINE_STAGE_INVENTORY.md`](PIPELINE_STAGE_INVENTORY.md) lists every phase with its observability today and its join ids:

- 28 write phases (A1–A28)
- 17 sleep phases (B0–B17)
- 34 read and answer phases (C1–C34)

It also lists the 17 observability gaps (F1–F17) that this plan closes.

---

## 1. The trace model (one schema for every tool)

One **inspect bundle** per run is written to `evals/runs/_analysis/inspect/<run_id>/`:

| File | One row per | Key fields |
|---|---|---|
| `turns.jsonl` | dataset turn | `item, session, turn_id, speaker, ts, text, record_id, index_text, stamp{source_at, tz, spk, addr, participants, src_class}, role{derive_use, trust_cap, echo}, firewall{trust, quarantined, reasons}, route, deposit_action, indexed{vector, lexical, graph_node}` |
| `windows.jsonl` | extraction window | `window_id, item, session, member_turns[], member_record_ids[], cut_reason, prepass{verdict, hints}, prompt{id, hash, context_lines, known}, call{model, cached, tokens, ms, prompt_text?, reply_text?}, sections{facts, edges, episode}, items{valid, repaired, dead[]}` |
| `envelopes.jsonl` | assertion envelope **per stage** | `occ, write_id, window_id, stage (compile/100/200/250/300/400/500/deposit), status (kept/dropped/rewritten/added), reason, before_hash, after_hash, fields_changed[], time{event, precision, basis}, grounding{label, rules}, resolve{entity, verdict}, integrate{op, rung, target}, record_id (once deposited)` |
| `records.jsonl` | stored record, snapshot after ingest and after sleep | `record_id, phase (post_ingest/post_sleep), memory_type, layer, channel, content, tags, entity/attribute/value, status, valid_from/to, evolve_to, trust, parents[], parent_turns[], envelope?` |
| `links.jsonl` | lineage edge or graph edge | `kind (lineage/graph/reply/mentions/derived_from/typed), src, dst, rel, props, created_by (stage), phase` |
| `sleep.jsonl` | sleep stage item | `stage, cycle, session_key, input_ids[], output_ids[], marker, calls, tokens, status` |
| `questions.jsonl` | question | `query_id, item, category, question, gold, gold_turns[], plan{contract, tier, route, list_mode, decompose}, legs{name: [{rid, turn, rank, score}]}, fused[], date_prior, protected[], cuts[{rid, turn, reason, stage}], pool[], rerank[{rid, raw, composite{rel, rec, imp, trust}}], headers[{rid, kind, source_ids}], packed[{rid, turn, tokens, reason}], window_added[], gate{state, dropped}, deep/obligations, context_text, reader{prompt, raw, calls[]}, retry, answer, judge{score, raw, conventions}, gold_path[{turn, last_stage_reached}]` |
| `checks.jsonl` | invariant result | `check_id, scope (turn/window/envelope/record/question), id, ok, detail` |
| `manifest.json` | the run | config hash, arm, commit, models, dataset revision, counts, and the file list |

Every row carries `run_id` and `item`. The **join keys** are:

- `turn_id` ↔ `record_id`;
- `window_id` → member record ids;
- `occ` → `record_id`;
- `record_id` → parents and links;
- `query_id` → candidate rids → turns.

Text fields are only written when `--inspect-content` is on (the default for local runs). The bundle directory is gitignored.

## 2. Packages

### TI-1 · Engine trace hooks
Flag `observability.inspect`, default off. Close the engine-side gaps F1–F17 with tap events that carry ids and an optional content payload. No behaviour change.

1. **Correlation (F1).** A contextvar `inspect_context(run_id, item, turn_id | query_id)` set by the harness. Every tap event in that scope carries those keys. Pre-record events (stamp, role, echo) are re-keyed to the rid once the record exists.
2. **Projectors (F2).** `store.index.record`: rid, vector yes/no, lexical yes/no, `index_text` hash and length (text when content is on), graph node and edges added.
3. **Extraction I/O (F3).** `write.extract.io`: window_id, prompt id and hash, rendered prompt, raw reply and parsed sections (content opt-in only).
4. **Window membership (F4).** `write.window.members`: window_id, member rids, cut reason, prepass verdict. Emitted also for windows that are skipped or fail.
5. **Envelope lifecycle (F5).** `write.envelope.trace`: one event per envelope per stage with status, reason, `fields_changed`, and `occ → rid` on deposit. The deposit sets `envelope.record_id`.
6. **Conflict and dedup ladder (F6).** `write.integrate.ladder`: rule, incumbent rid, outcome, as first-class events.
7. **Window edges (F7).** `write.edges.pending` and `.consumed`, with counts and ids.
8. **Sleep per-item (F8, F9).** `job.stage.item`: stage, session_key, input rids, output rid, channel, marker. Covers consolidate, mine, anticipate, reflect, extract_graph, rule_edges, summarize_entities, reorganize, decay and compress.
9. **Read keys (F10–F14).** In inspect mode the harness keeps all FX keys and `cuts`/`window`. Also add:
   - `compose.headers`: header kind, rid, source ids;
   - `read.rerank.components`: per-rid composite parts;
   - `read.planner.io` (content opt-in);
   - `read.retrieve.writeback`: the rids touched.
10. **Background tap scope (F17).** Wrap outbox prepare and commit in a `job` request.

Tests use the hash embedder and a stub LLM. With the flag off, behaviour is byte-identical (golden check).

### TI-2 · Store snapshot dumper
`evals/memspine_evals/inspect/snapshot.py`. After ingest and again after sleep, for a namespace, it writes `records.jsonl`, `links.jsonl` and `sleep.jsonl`:

- all records, including archived and superseded ones;
- `get_envelope(rid)`;
- lineage rows;
- `graph_nodes` and `graph_edges`;
- markers, read through the event log.

This is the ground truth for "how it is stored and linked", independent of events. It is CPU only.

### TI-3 · Bundle assembler
`evals/memspine_evals/inspect/assemble.py`. It joins harness rows (`ingest.jsonl`, `forensics.jsonl` with `trace_full`, `reads.jsonl`, `results.jsonl`), the TI-1 tap JSONL and the TI-2 snapshots into the schema in §1.

It also computes `gold_path`: for every gold turn, the last stage it reached. It reuses the `forensics_report.py` and `locomo_problem_classes.py` logic.

**Compare mode:** `assemble.py --compare runA runB` aligns by `turn_id` and `query_id`.

### TI-4 · Local HTML inspector
`evals/build_inspector.py`, output `evals/runs/_analysis/inspect/<run_id>/index.html`. It is self-contained, uses no network, and keeps all text local. Six views:

1. **Conversation.** Sessions, then turns. Each turn shows a stage strip: stored, indexed, window, extracted, envelopes per stage, deposited facts, links, sleep outputs. Every chip expands to that stage's input and output.
2. **Window.** Member turns, rendered prompt, raw reply, parsed items. Each envelope's journey through stages 100–500, with drops and reasons highlighted.
3. **Record and links graph.** A selected record with parents, children, graph neighbours, supersede chain and value history (an inline SVG force layout, no CDN).
4. **Question.** A waterfall: plan → each leg (gold turns marked) → fusion → cuts → pool → rerank → headers and pack → window → context → reader → judge. A candidate-count funnel per stage with gold survival, and the first losing stage named.
5. **Compare.** The same turn or question under reference vs write-v2, side by side, with differences highlighted.
6. **Checks.** The TI-6 invariant failures, filterable, each linked to its turn, window or question.

A demo page built from the invented conversation can be published as an artifact.

### TI-5 · CLI step mode
`python -m memspine_evals.inspect step --data <locomo10.json|demo> --item conv-26 --arm <ref|write-v2> [--replay-cache]`. It runs an engine in-process with the inspect hooks on. Commands:

- `turn` (or Enter): write the next turn and print each stage's output, pausing between stages;
- `session`: run the rest of the session, then session close;
- `sleep`: run sleep stage by stage;
- `ask <qid|text>`: run one read stage by stage;
- `show <rid|win|occ>`: print one object;
- `links <rid>`, `diff`, `save`, `quit`.

**`--replay-cache`** serves model calls from the recorded evals `call_cache.py` replies, so stepping through a recorded run needs no GPU. Calls missing from the cache are reported and skipped, or made live with `--live`.

Every step also appends to an inspect bundle, so the HTML viewer can open a step session.

### TI-6 · Invariants and health report
`evals/memspine_evals/inspect/checks.py` runs a fixed check set over a bundle:

- every turn is written exactly once, and every written turn is in the vector index (and in the lexical index when hybrid is on);
- every non-skipped window has members, a call outcome and counted sections;
- every envelope ends as deposited, or as dropped with a reason code;
- every derived record has at least one live parent and a channel;
- no parent cycles;
- every supersede has a target and a valid time order;
- every graph edge endpoint exists;
- sleep is idempotent: a second sleep adds nothing;
- every question has a plan, at least one leg, and a context within budget;
- gold-path stage counts match `forensics_report`;
- nothing derived uses the wall clock (R14-decon-2 / time-2).

Output goes to `checks.jsonl`, plus a one-page summary per conversation. Checks run in CI on the invented demo conversation.

### TI-7 · Docs and runbook
`docs/v2/INSPECTOR.md` covers:

- how to record, view and step;
- the stage catalogue, linked to each view;
- how to add a stage's trace when a new stage is written. **Rule: every new stage emits its trace event, or the PR is not done**, enforced by a test that matches the stage list against the trace event list.

## 3. Order and cost

| Step | Needs GPU | Depends on |
|---|---|---|
| TI-1 hooks, TI-2 dumper, TI-6 checks, TI-7 docs | no (hash embedder and stub LLM tests) | – |
| TI-3 assembler, TI-4 HTML | no | TI-1 and TI-2 formats (fixed in §1, so they run in parallel) |
| TI-5 step mode | no with `--replay-cache`; yes with `--live` | TI-1, TI-2 |
| **Record conv 1 under reference and write-v2** | yes, about 60–90 min for both (one conversation) | TI-1..3; slotted between GPU screens |
| Record all 10 | yes, about 6–10 h | after conv 1 is validated |

## 4. How we use it

1. **Manually.** Open the inspector for one conversation. Walk session → turn → stages, and question → stages. Use the CLI step mode to reproduce one turn or question live.
2. **By script.** Run the TI-6 checks and the gold-path funnel over all ten conversations after every full run. A regression in any check fails the run report.
3. **Maintenance.** Every new or changed stage ships with its trace event (TI-7 rule). The stage catalogue and inspector update with the code.
