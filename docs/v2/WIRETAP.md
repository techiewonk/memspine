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
