# Wiretap (WT-01)

A per-request trace of every pipeline stage so a developer can see each issue and gap of each request.
Debugging and development only. **Off by default**; when off, no event is built and no file is touched.

## Turn it on

```yaml
observability:
  tap:
    enabled: true
    sink: jsonl        # none | jsonl | sqlite | memory | otel | log
    dir: runs/_tap     # jsonl / sqlite
    content: false     # text only on explicit opt-in
    max_content_chars: 200
    sample_rate: 1.0
    kinds: [read, write, job]
```

`Engine.start()` calls `tap.configure(config.observability.tap)`; `Engine.stop()` shuts it down. Outside an
engine: `tap.configure(TapConfig(enabled=True, sink="memory"))`, then `tap.memory_traces()`.
`Engine.search/retrieve/assemble/read` open `read` requests, `write/write_messages/write_episode` open
`write`, `sleep` opens `job` (`@tap.traced(kind)`). Calls nested inside another request become children of it.

## Sinks

| sink | output |
|---|---|
| `jsonl` | one line per finished root request (children nested), one `tap-YYYY-MM-DD.jsonl` per day |
| `sqlite` | `tap.sqlite` with `requests` and `events` tables (children flattened, `parent_id` kept) |
| `memory` | ring buffer of the last 1000 root traces (tests, notebooks) |
| `otel` | one span per request, one span event per tap event; enabling without `opentelemetry` raises `TapConfigError` |
| `log` | one structured line per request on the `memspine.tap` logger |

Custom sinks: `tap.register_sink(name, factory)`; a sink has `write(trace_dict)` and `close()`. A sink failure
never reaches the engine (counted in `tap.sink_errors()`).

## What an event holds

Ids, numbers, booleans, enums and short identifier-shaped strings (the LIB-27 content guard), and lists or dicts
of those. Anything else (free text, objects) is dropped and its name is listed in the event's `field_rejected`
field, which the report flags. Text is recorded only under `content: true` and only when wrapped:
`tap.event("read.q", text=tap.content(query))`. It is truncated and redacted (secret and PII patterns, email and
phone masking). With content off the wrapped field is simply absent.

## Writing events (wiring code)

```python
from memspine.observability import tap

if tap.enabled():                       # skip building expensive payloads
    tap.event("read.gate.dropped", reason="visibility", n=len(dropped), ids=dropped_ids[:50])
with tap.span("read.rerank", model=model_id):    # emits read.rerank.start / read.rerank.end(duration_ms, error)
    ...
```

Naming: `<pipeline>.<stage>.<what>`, for example `read.leg.vector`, `read.gate.dropped`, `read.rerank.skipped`,
`write.firewall.verdict`, `ingest.extract.rejected`. Prefer these field names so the gap rules fire without
changes: `n`/`count` (size), `ids`/`id`, `reason`, `score`, `duration_ms`, `outcome` (`ok|error|timeout|retried`),
`error` (exception class name), `retries`, `skipped`, `abstained`, `partial`, `missing_capabilities`,
`unscored`, `covered`.

## Forensics bridge

While a root request is open, the tap installs the existing `trace_sink` forensics sinks (when the caller has
not installed its own). At the end each forensics entry becomes an event: `read.forensics.cut` (id, reason,
detail), `read.forensics.<list>` (n, ids, scores) and `write.forensics.<kind>`. `search_forensics()` and
`write_forensics()` behave as before and take precedence when a caller opened them.

## Gap report

```
python -m memspine.observability.tap_report runs/_tap [--request ID] [--html out.html] [--since 2h|1d|2026-10-13] [--json]
memspine tap report runs/_tap --html out.html
```

Per request it lists gaps and a summary table across requests (rule, requests affected, occurrences). The HTML
output is one self-contained file: summary, then each request's stage timeline with gaps highlighted.

Gap rules are data in `tap_report.RULES`: `Rule(id, severity, stage_globs, message, when=(Cond(field, op, value), ...))`
where ops are `eq ne gt ge lt le truthy falsy present absent in`. Add one `Rule` for a new event. Current rules:
`empty_leg`, `leg_skipped`, `dropped` (counts and ids by reason), `unscored`, `rerank_skipped`,
`missing_capability`, `partial_read`, `abstention`, `model_error`, `model_timeout`, `model_retry`, `quarantine`,
`extraction_rejected`, `coverage_hole`, `field_rejected`, `no_events`, and `slow_stage` (events carrying
`duration_ms` above the per-stage `SLO_MS` table).

## Write, derive and model events (WT-02)

All are `<pipeline>.<stage>.<what>`; ids, counts, enums and short codes only (firewall reasons are reduced to
their code, `size_anomaly(9>5)` becomes `size_anomaly`; free text such as a stage's skip reason rides as `*_text`
under `tap.content`).

| event | where | fields |
|---|---|---|
| `write.intake.received` / `write.intake.rejected` | `ingest/intake.py` | kind, source_id, segments, version, replayed, noop, new_events, intent_id / reason=quota |
| `write.intake.committed` / `write.intake.applied` / `write.intake.resume` / `write.intake.recovered` | `ingest/gateway.py` (W2-01, `write.intake_v2`) | intent_id, fresh, retried, version / records, resumed, raw_search / landed, fresh / intents |
| `write.stamp.turn` / `write.stamp.assent` | `ingest/write.py:_stamp_turn` (W2-02, `write.stamping_v2`) | turn, role, src_class, spk, addr, participants (count), source_tz, tz_unknown, precision, dated, flags / turn, kind, binds, matched, reasons, proposal |
| `write.intake.readiness` / `write.intake.retire` | `ingest/gateway.py` | intent_id, capability=ready / retracted, archived, repointed, edges_removed |
| `write.received` | `ingest/write.py` | memory_type, actor, chars, has_entity, reply |
| `write.messages.received` | `ingest/write.py` | turns, depositable, session, channel |
| `write.turn.skipped` | `ingest/write.py` | reason (role / injected_recall), turn, role |
| `write.firewall.verdict` | `ingest/write.py` | id, firewall, trust, quarantine, anomalous, instruction_flag, reasons |
| `write.quarantine` | `ingest/write.py` | id, reasons |
| `write.route` | `ingest/write.py` | id, memory_type, route (cue / semantic_pipeline / plain) |
| `write.deposit` | `ingest/write.py` (every door, also derived deposits) | id, memory_type, action (added / merged / updated / superseded / rejected / quarantined), trust, parents |
| `write.rejected` | `ingest/write.py` | id, reason |
| `write.time.parsed` / `write.time.unparsed` | `ingest/write.py`, `ingest/timeparse.py` | anchored, unknown_tz, assumed, precision / reason |
| `write.assets.attached` / `write.assets.registered` | `ingest/assets.py` | n, turn / id |
| `write.window.cut` | `workers/windows.py` | reason, turns, tokens, session, queued |
| `write.window.planned` | `workers/windows.py` | id, reason, turns, excluded, tokens, groups |
| `write.segment.cut` / `write.segment.gap` | `workers/windows.py`, `derive/segmenter.py` (`write.segmentation_v2`) | reason, turns, tokens, grown / reason (time_gap, calendar_day), gap_s, open_turns |
| `write.batch.planned` / `.window` / `.held` / `.failed` / `.cross_window` | `workers/windows.py` (`write.segmentation_v2`) | id, windows, turns, tokens, reason, sections / id, batch, first_line, turns / windows, tokens, wait_s / id / section, dropped |
| `write.window.skipped` / `write.window.job_failed` | `workers/windows.py` | reason, turns / error |
| `write.extract.call.start` / `.end` | `workers/windows.py` (span), or one `write.extract.call` (cached=true) | sections, cached, duration_ms, error |
| `write.extract.failed` / `write.extract.rejected` | `workers/windows.py` | reason (call_errors / parse_failures / section_malformed / parent_gone / *_deposit_error), sections, error, id |
| `write.extract.facts` / `.edges` / `.episode` | `workers/windows.py` | returned, accepted, invalid, ungrounded, uncited (truncated, present) |
| `write.extract.deposit` | `workers/windows.py` | id, layer, parents |
| `job` request, `stage=<name>` | `workers/schedule.py`: one child trace per sleep stage; `workers/windows.py`: one per window job | stage |
| `job.stage.start` / `job.stage.end` | `workers/schedule.py` | stage, status, every numeric or boolean stat of the stage (in/out/skipped counts, budget_full ...), skipped, failed, reason_text / error_text (content) |
| `model.call` | `models/providers/llm/base.py` (the one counting wrapper) | role, model, outcome (ok / error / timeout), prompt_tokens, completion_tokens, tokens_estimated, duration_ms, error |

Limits: window jobs run by the async worker (`write.window_async`) outlive the request that created them, so their
events are only recorded when a sleep or flush drives them (batch mode). Provider-internal retries are not visible
(`model.call` is one event per call as the stages see it).

New gap rules: `extraction_rejected` (also on `write.extract.facts/edges/episode` with invalid, ungrounded or
uncited above 0), `extraction_failed`, `stage_failed`, `stage_partial`, `stage_skipped` (also `write.*.skipped`),
`firewall_flag`, `write_replayed`, `time_unanchored`, `time_unparsed`, `window_anomaly`; `model_error` and
`model_timeout` fire on `model.call`. SLOs: `model.call` 60 s, `job.stage.*` 120 s.
