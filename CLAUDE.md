# CLAUDE.md — working guide for `memspine`

This file is loaded into every Claude Code session. Keep it accurate and terse.

## What this is

`memspine` is an open-source **cognitive-memory engine** for AI agents: one clean API (`Engine`) over a real write pipeline, hybrid + graph retrieval, and background learning dynamics — with pluggable, composable stores. It is the *engine*, not a product.

**Status:** pre-alpha, under active construction. **P0–P5 are implemented and review-passed** (substrate · working memory + retrieval · semantic · episodic + lifecycle · Memory Firewall · procedural + reflective) — 293 tests, `ruff` + `mypy --strict` clean, 14 ADRs. **P6 (associative graph) is next.** Current snapshot lives in `STATUS.md` (auto-refreshed every 30 min). The design docs in `docs/` are the **single source of truth** — read them before writing code.

## Read these first (in order)

1. `docs/memspine-structure-plan.md` — **the buildable blueprint**: repo tree, extras matrix, decision register (D-01…D-47), phase plan (P0…P7), enhancement program (E1–E9). This is authoritative.
2. `docs/UNIMEM_V2_REWORK_PROPOSAL.md` — architecture rationale (why, and the evidence base).
3. `docs/DEPENDENCY_ANALYSIS.md` + `docs/PACKAGE_CATALOG.md` — why each dependency was chosen; every candidate package with "does what".

> If a doc and this file disagree, the **structure plan wins** — then fix this file.

## Golden rules (do not violate without an ADR)

- **Event-sourced core.** An append-only `memory_events` log is the source of truth. Vector/graph/FTS/cache are **rebuildable projectors** — never a second source of truth.
- **Anti-lock-in (D-17).** Background pipelines are plain, idempotent step functions in `workers/pipelines.py`. Runners (inline/DBOS/taskiq) *decorate* them — no runner imports inside pipeline code.
- **Ports & adapters (D-22/M14).** Engine/memories/policies talk only to `services/*` capability ports. `clients/*` own connections; services never open a connection.
- **Slim core (D-03).** Zero heavy deps in core. Everything optional lives behind a `pip install memspine[extra]`. `torch`/`transformers` never enter core.
- **Profiles stay green.** Every change keeps `profile="simple"` behavior stable and backward-compatible.
- **Hard-fail clearly (D-10).** Missing service → `MissingServiceError` naming the extra to install, unless `strict_services: false`.

## Locked defaults (decision register — see plan for D-01…D-46)

| Area | Default | Notes |
|------|---------|-------|
| Language / tooling | **Python 3.13**, uv, ruff, pytest, mkdocs-material | D-02/D-04 |
| Facade | `memspine.Engine` | async-first, thin sync wrappers (D-01) |
| Storage | **SQLite** via **SQLAlchemy Core + Alembic**, async engine (**aiosqlite**) | not full ORM; sqlmodel rejected (D-36/D-44) |
| Event log | `event_log.mode`: **full** (default) / **rolling** / **ephemeral** + optional zstd payload compression | storage-cost control; pruning never passes projector high-water marks (D-45) |
| Vector | **LanceDB** (+ Tantivy FTS) | D-09 |
| Graph | **LadybugDB** default, **kuzu** supported alt, sqlite_adjacency fallback | D-26 |
| Cache/KV | **LMDB** | D-09 |
| Lexical | **FTS5 / Tantivy** BM25, RRF fusion | D-25; **CJK dropped** (D-34) |
| Embedder | **fastembed** (ONNX, CPU) | torch behind `[st]` only (D-08) |
| LLM | local: **Ollama/vLLM/llama.cpp/LM Studio/OpenAI-compatible** + Bedrock | per-role providers; litellm adapter-only (D-07/D-33/D-39) |
| Workers | **inline** default, **DBOS** durable, **taskiq** brokered | D-16 |
| Dedup | **datasketch** MinHash-LSH → cosine confirm | D-27 |
| Entities | **gliner2** CPU NER (flag) + LLM fallback | D-28 |
| Ingest | **markitdown + chonkie** `[ingest]` | D-29 |
| Structured output | **instructor** + always-on **json-repair** | D-31 |
| Compression | **zstandard** cold-tier + **llmlingua** assembly `[compress]` | D-32/E5 |
| Hashing/IDs | **xxhash** fingerprints + **fastuuid** ids | D-37 |
| Serialization | **pydantic** models + **orjson** hot-path | D-38 |
| Graph communities | **graspologic** `[community]` | D-40 |
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

```bash
uv sync --all-extras          # install dev env
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
2. **Check the register.** Before adding a dependency or pattern, confirm it against D-01…D-46. New dep → justify like `DEPENDENCY_ANALYSIS.md` (what it does, who proves it, which extra) and record a decision.
3. **Keep the core slim and `simple` green.** Guard both in every PR.
4. **Update docs with the code.** If you change a contract, update the structure plan + this file in the same change.

## Repo layout (target — being built out)

```
src/memspine/{engine,cli}.py · core/ · memories/ · services/ · clients/ · workers/ · config/ · prompts/ · protocols/ · observability/
evals/ (repo root, not shipped) · tests/{unit,integration,combinations}/ · docs/{adr/, *.md}
```

The full annotated tree, extras matrix, and phase→file mapping are in `docs/memspine-structure-plan.md`.
