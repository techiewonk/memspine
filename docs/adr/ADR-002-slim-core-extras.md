# ADR-002 — Slim core + extras packaging

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-03 (also D-02/D-04 tooling)
- **Phase:** P0 · **Tier:** QW

## Context

Memory frameworks bloat fast (torch, transformers, servers). Adoption requires `pip install memspine` to give a working brain in seconds, on CPU, with zero heavy deps — the field's converged defaults (fastembed/ONNX, SQLite) make this possible.

## Decision

Core dependencies are only small, pure-ish wheels (pydantic, SQLAlchemy Core+Alembic, orjson, xxhash, fastuuid, structlog, typer, fastembed, json-repair, datasketch, zstandard, jinja2, aiosqlite, pyyaml, httpx, **lancedb**). Everything else is an extra per the §2 matrix (`[graph]`, `[rest]`, …). `torch`/`transformers` never enter core. Python 3.13 floor; uv/ruff/pytest/mkdocs tooling.

**Amended by ADR-021:** LanceDB is core (no `[lance]` extra).

## Consequences

- Positive: instant OOTB brain (SQLite + FTS5 + inline runner); CI matrix per extra; clear capability→extra mapping in `MissingServiceError` (D-10).
- Negative / cost: extras matrix maintenance; conflict management between extras (e.g. graspologic vs markitdown numpy pins, recorded in pyproject).
- Follow-up: `[graph]` extra is empty until the ladybugdb fork is published (P6).

## Alternatives rejected

- **Batteries-included install** — kills serverless/edge/CI use, contradicts engine positioning.
