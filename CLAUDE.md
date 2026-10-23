# CLAUDE.md — working guide for `memspine`

This file is loaded into every Claude Code session. Keep it accurate and terse.

## What this is

`memspine` is an open-source **cognitive-memory engine** for AI agents: one clean API (`Engine`) over a real write pipeline, hybrid + graph retrieval, and background learning dynamics — with pluggable, composable stores. It is the *engine*, not a product.

**Status:** pre-alpha, under active construction. **P0–P7 are all implemented and review-passed** (substrate · working memory + retrieval · semantic · episodic + lifecycle · Memory Firewall · procedural + reflective · associative graph · prospective + shared + REST), plus the C6 combination matrix. Get the current test count with `uv run pytest --collect-only -q` (evals add `evals/tests`); `ruff` + `mypy --strict` are the gate. **83 ADRs** (ADR-001…ADR-085; 038 and 045 unassigned; ADR-029/030/031 *proposed*), decision register through D-109. All 9 memory types and E1–E9 are landed. User-facing docs: `docs/FEATURES.md` + `docs/USAGE.md`. Ecosystem comparison: `docs/ECOSYSTEM_COMPARISON.md` + `docs/ARCHITECTURE_FLOWS.md` (pass #3: stages, packages, prompts, memory I/O). Current task/status lives only in `docs/PROVIDER_TODO.md` (see below). The design docs in `docs/` are the **single source of truth** — read them before writing code.

## Single live task/status tracker

Use **docs/PROVIDER_TODO.md** for all current task IDs, statuses, completed work/evidence and next milestones. Claude and Codex share this one register. Do not create another TASKS.md, roadmap checklist or copied handoff status inventory. Plans/handoffs/logs are context or historical evidence. `python scripts/refresh_tracker.py` refreshes totals and the read-only HTML view; `--check` detects stale output. This status authority does not replace design or schema specifications.

## Read these first (in order)

1. `docs/memspine-structure-plan.md` — **the buildable blueprint**: repo tree, extras matrix, decision register (D-01…D-109), phase plan (P0…P7), enhancement program (E1–E9). This is authoritative.
2. `docs/UNIMEM_V2_REWORK_PROPOSAL.md` — architecture rationale (why, and the evidence base).
3. `docs/DEPENDENCY_ANALYSIS.md` + `docs/PACKAGE_CATALOG.md` — why each dependency was chosen; every candidate package with "does what".

> If a doc and this file disagree, the **structure plan wins** — then fix this file.

## Golden rules (do not violate without an ADR)

- **Event-sourced core.** An append-only `memory_events` log is the source of truth. Vector/graph/FTS/cache are **rebuildable projectors** — never a second source of truth.
- **Anti-lock-in (D-17).** Background pipelines are plain, idempotent step functions under `derive/pipeline/stages/`, registered by name in `derive/pipeline/stages/table.py`. Runners (inline/Temporal+Resonate/taskiq) *decorate* them — no runner imports inside pipeline code.
- **Ports & adapters (D-22/M14).** Engine/memories/policies talk only to `services/*` capability ports. `clients/*` own connections; services never open a connection.
- **Slim core (D-03).** Zero heavy deps in core. Everything optional lives behind a `pip install memspine[extra]`. `torch`/`transformers` never enter core.
- **Run the test suite in chunks on low-memory machines** (a single-process run can die silently, exit 127). DeepEval is no longer a dependency; pass `-p no:deepeval` only while an old `.venv` still has it installed (its plugin hides pytest's summary).
- **Profiles stay green.** Preserve `simple` retrieval behaviour while applying the accepted breaking provider schema. No legacy configuration aliases or compatibility modules (ADR-068); `core` is the bare reference used by mechanics tests.
- **Hard-fail clearly (D-10).** Missing service → `MissingServiceError` naming the extra to install, unless `strict_services: false`.

## Locked defaults (decision register — see plan for D-01…D-109)

| Area | Default | Notes |
|------|---------|-------|
| Language / tooling | **Python 3.13**, uv, ruff, pytest, mkdocs-material | D-02/D-04 |
| Facade | `memspine.Engine` | async-first, thin sync wrappers (D-01); default template `assistant` (D-58/ADR-032); the measured read-path advantages live in `base`/`simple` (D-59/ADR-033); `template="core"` = bare |
| Storage | **SQLite** via **SQLAlchemy Core + Alembic**, async engine (**aiosqlite**) | not full ORM; sqlmodel rejected (D-36/D-44) |
| Event log | `event_log.mode`: **full** (default) / **rolling** / **ephemeral** + optional zstd payload compression | storage-cost control; pruning never passes projector high-water marks (D-45) |
| Vector | **LanceDB** (+ Tantivy FTS) | D-09 |
| Graph | **auto** selects compatible LadybugDB `[graph]` or `sql_adjacency` (SQLite or Postgres; renamed from `sqlite_adjacency`, no alias), resolved once and pinned in the projector binding (ADR-081); explicit Neo4j is separately configured; no kuzu alias | D-26/D-49/D-60/ADR-068/ADR-081 |
| Cache/KV | **memory** (in-process) default; disk / redis / valkey via cashews `[cache]` | D-09/ADR-022 amendment; LMDB replaced |
| Lexical | **FTS5 / Tantivy** BM25, RRF fusion | D-25; **CJK dropped** (D-34) |
| Embedder | **fastembed** (ONNX, CPU) | torch behind `[st]` only (D-08) |
| LLM | local: **Ollama/vLLM/llama.cpp/LM Studio/OpenAI-compatible** + Bedrock | per-role providers; litellm adapter-only (D-07/D-33/D-39) |
| Workers | **inline** default, **Temporal + embedded Resonate** durable, **taskiq** brokered | ADR-071; Temporal requires external cluster + persistent local journal |
| Dedup | **datasketch** MinHash-LSH → cosine confirm | D-27 |
| Entities | **GLiNER2** CPU NER `[ner]` (default checkpoint GLiNER2.5 Base, `fastino/gliner2.5-base-v1`), selected via named `models.profiles` + `models.bindings` (off unless bound) + LLM fallback | D-28/D-97/ADR-075 |
| Ingest | **markitdown + chonkie** `[ingest]` | D-29 |
| Structured output | **instructor** + always-on **json-repair** | D-31 |
| Compression | **zstandard** cold-tier + **llmlingua** assembly `[compress]` | D-32/E5 |
| Hashing/IDs | **xxhash** fingerprints + **fastuuid** ids | D-37 |
| Serialization | **pydantic** models + **orjson** hot-path | D-38 |
| Graph communities | **graspologic-native** `[community]` (Leiden → LPA hybrid; built-in LPA without the extra) | D-40/ADR-028/ADR-043 |
| Prompts | customizable subsystem: YAML pack + registry + config layering + output-model pairing | D-43 |
| Firewall | Memory Firewall (trust/quarantine/anomaly) — OWASP ASI06 | E1 |

## Conventions

- **Async-first**; provide thin sync wrappers on the public API.
- **Types everywhere**; ship `py.typed`. Run `mypy` (or pyright) clean.
- **Logging** via `structlog` with the M11 vocabulary (`memory.write/retrieve/consolidate/decay_transition/conflict/merge/forget/rebuild`).
- **Config** via `pydantic-settings`, layered defaults → template → user YAML → env → kwargs (D-11). Magic numbers live in `config/constants.py` and cite their design-doc section.
- **Prompts** are data (`prompts/defaults/*.yaml`), never inline strings — every internal LLM call resolves a named, versioned prompt (D-43).
- **Tests** mirror `src/` 1:1 under `tests/unit/`; combination-matrix boots under `tests/combinations/`.
- **One decision = one ADR.** New/changed decisions get a `docs/adr/ADR-NNN.md` and a row in the structure-plan register.

## Commands

Dependency baseline (D-92 / ADR-070): Python 3.13 and uv >=0.13.1. Install with
`uv sync --locked --extra dev`; validate with `uv pip check` and
`python scripts/audit_dependencies.py --check`. See `docs/PYTHON313_DEPENDENCIES.md`
for current versions, intentional caps and actual runtime coverage. DeepEval is
no longer a development dependency; no Click override is permitted. Heavy model
extras remain optional and are not proven by metadata/lock resolution alone.

```bash
uv sync --locked --extra dev  # install dev env (add --extra <name> for optional features)
just test                     # pytest
just lint                     # ruff + mypy
just docs                     # mkdocs serve
just check                    # lint + test (pre-commit gate)
# raw equivalents:
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

## How to work here

1. **Find the phase.** Work follows P0→P7 (see plan §5). Confirm which phase a task belongs to; don't build P4 before P1's contracts exist.
2. **Check the register.** Before adding a dependency or pattern, confirm it against D-01…D-109. New dep → justify like `DEPENDENCY_ANALYSIS.md` (what it does, who proves it, which extra) and record a decision.
3. **Keep the core slim and `simple` green.** Guard both in every PR.
4. **Update docs with the code.** If you change a contract, update the structure plan + this file in the same change.

## Repo layout (restructured in place, ADR-067)

```
src/memspine/{cli,plugins,scope,exceptions}.py · api/ (Engine facade, api/engine.py) · memory/ · memories/ · ingest/ · derive/ · read/
  · store/ · providers/ · services/ · clients/ · models/ · semantics/ · trust/ · economy/ · runtime/ · workers/ · compose/
  · core/ · config/ · schemas/ · prompts/ · ops/ · observability/
evals/ (repo root, not shipped) · tests/{unit,integration,combinations}/ · docs/{adr/, *.md}
```

The full annotated tree, extras matrix, and phase→file mapping are in `docs/memspine-structure-plan.md`.

## Evaluation harness (`evals/`)

`evals/` holds the benchmark harness — **outside the wheel (D-35)**, stdlib-only core, its own
`README.md`. One protocol over any system and any dataset, with provenance enforced in the type
system rather than by convention. It is not part of `uv run pytest` (pyproject `testpaths = ["tests"]`);
run it with `python -m pytest evals/tests -q`, and drive it with `python -m memspine_evals {smoke,c0-1,split}`.

Its authority is the research repo's `paper_spine/EVALUATION_PLAN_2026-09.md` and
`paper_spine/evaluation/LOOP_METRIC_CONTRACT.md` — budget, baseline conditions, denominator rules
and the CPC definition come from there. **No paid API runs are authorised**; local
OpenAI-compatible backends only, and QA mode refuses to start without an explicit model-call cap.


Provider architecture: [ADR-068](docs/adr/ADR-068-provider-contracts-and-controlled-changes.md), [ADR-069](docs/adr/ADR-069-remote-providers-and-explicit-recovery.md) and [the accepted plan](docs/PROVIDER_ARCHITECTURE_PLAN.md). Descriptors are owned by `memspine.providers.registry`; no old registry modules or config retirement layer. Constructor fields use nested settings. OpenSearch lexical, Weaviate lexical/vector and pgvector vector are optional adapters; lexical/vector ports remain independent. Pgvector projection DDL requires explicit opt-in, and SQL DELETE is not physical-erasure proof. Cleanup fence recovery requires an application quiescence guard and audited CAS; never steal a lock by age. HTTP service-change execution requires the actual deployment controller; health checks alone do not validate production.

Structured GLiNER extraction (D-94 / ADR-072) returns typed source-cited candidates only and cannot bypass canonical grounding. D-97 / ADR-075 uses named `models.profiles` and task bindings for independently selected GLiNER/LLM models, superseding the single D-96 `models.gliner` field without an alias. Share weights for identical effective settings, never user result caches. Model escalation is explicit, bounded and opt-in; consent gates apply to every candidate. Bounded RRF prefixes apply only to fixed-prefix callers; the main router retains full ranking. Performance validation remains kernel-only.

D-98 / ADR-076: GLiNER mentions are exact source-bound annotations with `asserted=False`; they never key a record as an asserted fact or replace window support evidence. Source sender/message identity comes from the canonical message envelope, not model output. Dedup retains dropped-source annotations in MERGE audit snapshots without rebinding them to kept text.

D-99 / ADR-077 accepts raw-first ingestion plus durable background grounding, source-bound unasserted graph mentions, recoverable lossless raw encoding and separate summaries. Implementation is planned in docs/BACKGROUND_INGESTION_PLAN.md; no target defaults are enabled by the plan. Keep annotation persistence independent of fact yield and derivation readiness separate from raw acceptance.

D-100 / ADR-078 accepts evidence-bound identity/aliases, supported dated residence transitions with multiple homes, and source-local temporal precision plus evidence-known time. Initial contextual linking is engineering-first; retrieval similarity proposes candidates only. See docs/INGESTION_CONTEXT_ENRICHMENT.md; remaining contracts are in review, not implemented.

D-100 schema selection Q-S1..5 B/B/A/A/B: coordinated replacement of affected envelopes, aware-only resolved times (timezone-free dates remain text), typed claim values, explicit domain occurrences and persisted excerpt copies with cascade erasure. docs/ENRICHMENT_SCHEMA_CONTRACTS.md is the target; not implemented.

D-104 / ADR-080: compiled graph edges carry typed qualifiers (aware-only times; semantics vocabularies for lifecycle, sensitivity and origin). `graph_filter` on search/read/REST/MCP gates only the compiled-edge leg. Unknown never passes an allow-filter and erased edges never pass; unqualified graph paths are skipped under a filter. Sensitivity is a separate three-state labelling layer that only tightens policy. Malformed qualifiers are dropped with a warning, never failing the write.

D-105 / ADR-081 (RM-111 WP1-WP3/WP5/WP7 landed): data providers live in one `storage` block with a slot per category (`storage.records`, `.vector`, `.lexical`, `.graph` (the one graph client; the `.graph_mirror` slot was removed 2026-10-23 and is rejected naming `storage.graph`), `.cache`, `.documents` (reserved, must be null)), each `{provider, settings}`, plus a `deployment` block (`id` (was `runtime.deployment_id`), `locality`, `data_dir` (was postgres `settings.data_dir`; unset keeps today's placement base)). The old keys (`storage.profile/provider/settings`, top-level `vector`/`cache`, `graph.provider/settings/mirror`, `read.lexical_*`, `runtime.deployment_id`) are rejected naming the new key; `scripts/migrate_config_keys_adr081.py` migrates configs. Every durable SQL database uses Alembic (Postgres under an advisory lock). Every built-in data descriptor declares `locality` (process/host/cluster); WP6 validation is pending. `embedded` and `server` are config templates. Service providers (embedding model, LLM, models) stay outside `storage`.

D-106 / ADR-082 (RM-117 landed 2026-10-23): model providers move to one `model` block: `model.embedding`, `model.llm.<role>`, `model.rerank` (chain + typed behaviour), `model.extraction.<consumer>`, `model.decider.<consumer>`, `model.vision`, `model.placement`. Each slot is `{provider, typed common keys, settings}` (settings = provider-owned JSON). No named profiles; identical settings share weights (weights key ignores consumer-only fields). The old keys (`embedding`, `llm.roles`, `models.*`, `decision.provider`, `read.rerank*`, `read.decider*`, `ingest.asset_vision*`) are rejected naming the new key; `scripts/migrate_config_keys_adr082.py` migrates configs. Every LLM slot states `timeout_s`. Follow-ups are RM-118. See docs/MODEL_SLOTS_MIGRATION_PLAN.md.

D-107 / ADR-083 (RM-119 landed 2026-10-23): the `mongodb` provider may launch a local mongod (`launch.mode: managed`, exact version pin, explicit port, download only with `download: true` and SHA-256 verification, one-member replica set on loopback, host-local). Default stays `external`; SQLite + filesystem remain the embedded default.

D-108 / ADR-084 and D-109 / ADR-085 (accepted designs, planned as RM-123 / RM-124, not implemented; plan docs/FASTEMBED_LITELLM_REPLACEMENT_PLAN.md): fastembed is replaced by an own `onnx` embedder and cross-encoder (embedder id `embedder:<model>`, parity-certified models keep their vector space), and litellm by an own OpenAI-compatible httpx client (presets ollama/vllm/lmstudio/llamacpp_server, Bedrock on boto3, optional `[anyllm]`). Until they land, the Embedder and LLM rows of the defaults table still describe fastembed and litellm.
