# ADR-005 — Background execution: TaskRunner seam, pipelines stay plain functions

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-16 / D-17 / D-18
- **Phase:** P0 contract, P1/P3/P7 runners · **Tier:** DF
- **Amended:** 2026-07-10 (v0.2 A4, DEC-4) — see *Server-profile default* below.
- **Amended:** 2026-07-10 (v0.2 D1) — autonomous sleep scheduler; see *D1* below.

## Context

Consolidation/decay/compression must run in the background, but binding the engine to one worker framework is lock-in. The MemOS Redis-Streams scheduler (per-scope queues, claim recovery) validates the brokered design (D-42).

## Decision

Background pipelines are plain, idempotent step functions in `workers/pipelines.py`. Runners *decorate* them: `inline` (default), `dbos` (durable, SQLite→Postgres), `taskiq` (Valkey Streams with per-scope keys + priority labels). No runner imports inside pipeline code. Dead-letter severity: consolidation = warning, M7 hard-delete cascade = alert (D-18).

### Autonomous sleep scheduler (v0.2 D1)

The sleep cycle (consolidate → extract_graph → reorganize → decay → prune) ran
only on an explicit `Engine.sleep()` in v0.1, so learning dynamics were dormant
unless something drove them. D1 adds `workers/scheduler.py::SleepScheduler`: a
thin asyncio task that runs the full cycle every `workers.sleep_interval_seconds`
and is started/stopped by the engine. It decides *when*, never *how* — the work
still flows through the configured runner (inline/dbos/taskiq), so anti-lock-in
(D-17) holds. Runs cannot overlap (await-then-sleep in one task), a failing
cycle is logged and the loop continues, and stop() cancels-and-awaits before
tearing down the runner. **Off by default** (`sleep_interval_seconds = null`):
`profile="simple"` and existing deployments are unchanged. The optional
decay→FORGET auto-archival of dormant records is deferred — hard deletion is M7
-governed and needs its own decision; the scheduler advancing decay *tiers*
(reversible) is the safe core.

**Guard (v0.2):** the scheduler is **skipped on `:memory:`** storage. An
in-memory SQLite DB shares one connection and can't use WAL, so a background
sleep cycle collides with foreground writes ("table is locked"), and an
ephemeral DB has nothing durable to maintain anyway. File-backed DBs (WAL +
`busy_timeout`) run it safely; production autonomous scheduling should pair with
a durable runner (dbos/taskiq), which serializes work through its queue. A
coarse whole-cycle serialization lock was evaluated and rejected — it would add
write-path latency for no benefit over WAL, and a partial version gives false
safety (the pipeline writes through the same door it would need to lock).

### Server-profile default (v0.2 A4, DEC-4)

The **schema default stays `inline`** — a bare `pip install memspine` must boot with zero extras (slim-core D-03, profiles-stay-green). Durable execution is opted into per *server* deployment profile at the **template** layer, not the schema: the `multi_agent` and `regulated_financial` templates pin `workers.runner: dbos`. DBOS defaults to a colocated SQLite system database (`<storage.path>.dbos.sqlite`), so this adds no external infrastructure — only the `memspine[dbos]` extra, which a deployed server installs. Embedded profiles (`base`/simple, `coding`, `personal`, `voice`) remain `inline`. A schema-level `runner="dbos"` default was rejected: it would fail every zero-extra install at start. Template scope keeps the blast radius to profiles a server operator explicitly selects.

## Consequences

- Positive: `pip install memspine` works with zero infra; scaling is a config change, not a rewrite.
- Negative / cost: pipelines can't use runner-specific niceties directly; the seam must be tested per runner (crash-resume, flush-on-exit).
- Follow-up: P1 inline runner; P3 dbos; P7 taskiq with per-scope streams.

## Update — dbos runner is real (post-P3 hardening pass)

The dbos runner shipped through P3/C6 as a seam only: pipelines ran with
inline semantics plus dead-letter reporting, `DBOS.launch()` was never called
anywhere, and `DBOSRunner.__init__` took no arguments. That gap is closed:

- Every `DBOSRunner.run()` call executes the pipeline through a real
  `@DBOS.workflow`-decorated function, so DBOS's own system database records
  PENDING → SUCCESS/ERROR for each invocation and `DBOS.launch()` (now called
  from `Engine._build_runner`, guarded on `workers.runner == "dbos"`)
  auto-recovers anything left PENDING by a crash.
- The durable *unit* is the whole pipeline invocation, not its internal
  steps: `PipelineContext` holds live connections/callables that cannot be
  checkpointed, so the workflow body takes only a pipeline name (a plain
  `str`) and rebuilds a fresh context on every call — including on recovery —
  via a `context_factory` callable the engine wires in (`self._pipeline_ctx`).
  Re-execution from the top is always safe because pipelines are idempotent
  (D-17).
- Zero-infra confirmed for real: the installed DBOS SDK (2.26, verified
  against the actual package — `dbos>=0.20` was too permissive a floor and is
  now `dbos>=2.20,<3.0`) defaults its system database to **SQLite**, not
  Postgres. No external server is required to install, configure, or test
  `[dbos]`; `workers.dbos_system_database_url` exists only as an escape hatch
  for deployments wanting a shared Postgres system db across a fleet.
- Two DBOS SDK edges surfaced during hardening and are handled in
  `workers/dbos_runner.py` (see its module docstring for the full mechanism):
  DBOS is a process-wide singleton that silently reuses a stale instance
  across engine restarts unless explicitly destroyed, and calling any DBOS
  async workflow repoints the calling event loop's default executor at
  DBOS's own — `DBOSRunner.close()` repairs both so the stop()/start() engine
  cycles this codebase relies on everywhere stay safe.

See `tests/integration/test_dbos_crash_resume.py::test_dbos_durable_resume`
for the checkpointing proof (a genuine mid-pipeline process kill is not
simulated — the workflow has no internal step checkpoints by design, so only
"before start" and "after completion" are meaningful tear points, and the
former is exactly what the idempotent-resume property already covers).

## Alternatives rejected

- **celery / apscheduler** — heavyweight, wrong grain (anti-decision).
- **Runner-decorated pipeline code** — the exact lock-in D-17 forbids.
