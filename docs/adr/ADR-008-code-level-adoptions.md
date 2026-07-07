# ADR-008 — Code-level adoptions D-26…D-42 (two dependency-scan passes)

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-26…D-42
- **Phase:** P0 fields, P1–P7 logic · **Tier:** mixed (see register)

## Context

Two scans of every memory framework in `D:\mem` (evidence: `DEPENDENCY_ANALYSIS.md`, `PACKAGE_CATALOG.md`, plan Parts C/D) identified which libraries the field has already proven for each sub-problem, plus five MemOS patterns worth adopting without importing MemOS.

## Decision

Adopt: datasketch MinHash-LSH + cosine confirm dedup (D-27) · gliner2 CPU NER behind a flag (D-28) · markitdown+chonkie ingest (D-29) · instructor + always-on json-repair structured output (D-31) · zstandard cold-tier compression (D-32) · litellm as optional adapter only (D-33) · deepeval in evals only (D-35) · SQLAlchemy Core + Alembic storage (D-36) · xxhash fingerprints + fastuuid ids (D-37) · pydantic + orjson (D-38) · first-class local/open-weight hosting via OpenAI-compatible surface (D-39) · graspologic hierarchical_leiden `[community]` (D-40) · fakeredis test double (D-41) · five MemOS patterns: typed-memory container, rich provenance/lifecycle (`status/version/history/evolve_to/source` — in the P0 record), per-scope stream queues, background graph reorganizer, hybrid+strategy rerank (D-42).

Reject/skip: CJK track (D-34 reversed) · file-native profile (D-30) · nltk/rake-nltk/igraph/mmh3 in core · KV-cache activation memory · backend sprawl.

## Consequences

- Positive: every non-trivial sub-problem uses a field-proven library; core grows by only three small wheels.
- Negative / cost: extras matrix breadth; graspologic's numpy<2 pin conflicts with markitdown (recorded in pyproject `[tool.uv] conflicts`).
- Follow-up: each adoption lands in its mapped phase (plan §5).

## Alternatives rejected

See the anti-decisions note in the plan register (sqlmodel, baml, celery, jieba, …).
