# MemSpine restructure: target layout, steps and owned files

Package X-00 of `PLAN.md`. memspine is restructured in place (owner, 2026-10-12): no separate package, no
compatibility shims; replaced code is deleted. Pure moves are guarded by the CPU golden and both test suites; behaviour
changes are built flag-off and graduate through the paired gate. Runtime and modularity follow the owner's 2026-10-13
answers (`pipelines/README.md`): outbox + taskiq, async core with dedicated executors, four priority classes, dishka
dependency injection, typed registries with entry points, versioned schemas, import-linter contracts.

## 1. Target tree (`src/memspine/`)

```
__init__.py          open(), Memory (scoped handle), Engine (lifecycle + admin), __version__
errors.py            from exceptions.py
schemas/             versioned Pydantic I/O types of every pipeline step; JSON Schema export (MOD-01)
store/               truth and projections: records, envelope, scope, keys, events (+upcasters), log, lineage,
                     registry (cardinality), layers (LayerRegistry), projections/, backends/ (sqlite+alembic,
                     postgres+alembic, lancedb, tantivy, ladybug, sql_adjacency, neo4j), profiles, migrate, export
ingest/              D: envelope (SourceEnvelope, Receipt), stamp, roles, commit, write, segment, assets
derive/              online + offline: extract, ground, resolve, integrate, graph (edge compiler), temporal,
                     session (call 2), scheduler, job, stages/ (O-04 order)
read/                R: request, tiers, contract, router, legs/, fusion, lift, rerank, gate, timeview, deep
compose/             C: windows, pack, sections, render, answer
models/              tiers, providers/, structured, fingerprint, encoders/, planner, ledger
prompts/             registry, loader, env, schemas, layout, lint
trust/               firewall, horizon, privacy, consent, erasure, audit, quarantine
memory/              type mechanisms kept as features: procedural, prospective, shared, working
runtime/             outbox, transport (taskiq: in-memory / external broker), runner, executors, priorities,
                     locks, container (dishka providers: app / job / request scopes)
api/                 handle, engine, rest/, mcp, tools, cli
config/              schema/ (per area), flags (registry with evidence), loader, presets, templates
observability/       logging, timers, trace sink, optional OTel
```

Import contract (import-linter, CI): `api -> (ingest, derive, read, compose) -> (store, models, trust, runtime,
prompts) -> schemas`; `store` never imports `read`, `derive`, `ingest`, `api`; `read` never imports `derive`.

## 2. Steps (one PR each into the `restructure` branch)

| Step | Package | What | Kind | Check |
|---|---|---|---|---|
| S0 | P0-04, X-02, X-03 | CPU golden on the synthetic fixture, import-boundary contracts, method-to-module map of `engine.py` | infra | golden identical twice |
| S5 | F-05 | skeleton; move store and trust leaves; codemod rewrites imports in src, tests, evals | move | suites + golden |
| S6 | F-06 | move read, compose, models leaves | move | suites + golden |
| S6b | MOD-02 | typed registries + entry points | move | plug-in test + golden |
| S7 | F-07 | config split per area; flag registry | move | same YAML keys |
| S8 | F-08 | wiring out of the engine into a dishka container | move | golden |
| S9 | F-09 | write path out | move | golden |
| S10 | F-10a, F-10b | read path out | move | golden byte-identical hit order |
| S11 | F-11 | compose out | move | golden context hash |
| S12 | F-12 | trust, admin, memory-type mechanisms out | move | golden |
| S13 | F-13a, F-13b, F-13c | scoped handle; REST/MCP; harness adapter; replay NO CHANGE | move | GPU replay (one run) |
| S14 | F-14a, F-14b | retirement (measured-negative now; unscreened after owner) | move | retired flags raise |
| S15 | F-15 | split pipelines.py into derive stages | move | golden |
| S1 | F-01 | graph key identity | behaviour | A-B-A tests; GC screen |
| S2 | F-02a, F-02b, F-02c | envelope, tri-temporal, LayerRegistry, edge compiler | move / behaviour | replay byte-stable |
| S3 | F-03 | versioned vector spaces, re-derivation | behaviour | shadow switch test |
| S4 | F-04, RT-01, RT-02 | outbox + runner, taskiq transport, executors, priorities, backpressure | behaviour | fault injection, load test |
| S16 | F-16a..f | lineage door, upcasters, scope, profiles, Neo4j, isolation matrix | mixed | contract matrix |

## 3. Owned files per package (agents may only write these, plus their tests)

| Package | Owns (target) | Removes or edits (v1) |
|---|---|---|
| F-05 | `store/{records,events,log,scope,lineage}.py`, `trust/*` | `core/records.py`, `core/events.py`, `core/replay.py`, `core/projector.py`, `core/namespace.py`, `core/visibility.py`, `core/firewall.py`, `core/audit.py`, `core/erasure.py`, `core/privacy.py`, `core/redaction.py` (moves) |
| F-06 | `read/*`, `compose/*`, `models/*` leaves | `core/query_*`, `core/temporal_*`, `core/perspective.py`, `core/lead.py`, `core/policies/{assembly,compression}.py`, `core/answer*.py`, `services/{llm,embedding,rerank,query_encoder,decision,cache}` |
| MOD-02 | `*/registry.py` per kind | registration call sites only |
| F-07 | `config/schema/*`, `config/flags.py` | `config/schema.py` |
| F-08 | `runtime/container.py` | `engine.py` 13838-15179 and start/stop |
| F-09 | `ingest/*` | `engine.py` 1672-2000, 4270-4995, 13468-13838 |
| F-10a/b | `read/*` | `engine.py` 2331-3900, 4996-5835, 6576-6760, 7505-8062 |
| F-11 | `compose/*` | `engine.py` 3879-4200, 5836-6575, 8063-9610 |
| F-12 | `trust/*`, `memory/*` | `engine.py` 2013-2330, 9611-13207 |
| F-13a/b/c | `api/*` | the rest of `engine.py`; `protocols/*`; `evals/memspine_evals/systems/memspine_system.py` |
| F-15 | `derive/stages/*` | `workers/pipelines.py` |
| F-01 | `store/keys.py` | `_edge_key` users in `derive/` |
| F-02a/b/c | `store/{envelope,layers}.py`, `derive/graph.py`, alembic revision | `store/records.py` |
| F-04, RT-01, RT-02 | `runtime/*`, `ingest/commit.py` | `workers/{inline,runner,scheduler,schedule,temporal_runner,temporal_workflow,step_journal,taskiq_runner}.py` (DBOS runner removed, ADR-071) |
| F-16a..f | `store/{lineage,migrate,profiles}.py`, `store/backends/{postgres,neo4j}`, `derive/write.py` | `services/graph/neo4j.py` stub |

Line ranges refer to `engine.py` at commit c83e93a; the method-to-module map (P0-04) supersedes them before F-08.

## 4. Risks that every agent must respect

- `pinned_run.sh` reproduces old commits from a worktree; do not change its interface, `evals/runs`, `data/locomo10.json`
  or `evals/data/opbench_src`. Per-run venvs (P0-02) come before any extra is removed from `pyproject.toml`.
- No old import path survives a move PR; the codemod rewrites src, tests and `evals/` in the same commit.
- Model calls never run inside a namespace lock (the 2026-10-12 hang).
- The eval adapter must call the read barrier after F-04, or reads race derivation.

## 5. Moved engine code vs library modules (2026-10-13)

The library-first splits (LIB-01..47, `LIBRARIES.md`) placed new pure modules in the target packages first. Code moved
out of `engine.py` or `workers/pipelines.py` therefore never takes a library's file name: it goes into a `pipeline/`
subpackage of its target package (`read/pipeline/`, `compose/pipeline/`, `derive/pipeline/`, ...) or, where no clash
exists, the module named in the method map. The wiring package for each library then replaces the moved internals with
the library and deletes the replaced code in the same PR (ADR-067, no shims).

## Change log
- 2026-10-13: first version (X-00), from the layout analysis and the owner's runtime and modularity decisions.
- 2026-10-13: section 5 (moved engine code goes to `<pkg>/pipeline/` when a library owns the mapped name).
