# Constraints (intel)

> **Provenance note:** The ingest set contains **no SPEC** documents (no
> standalone API-contract/schema/protocol/NFR spec files). The constraints below
> are **ADR/DOC-derived** — the binding invariants come from LOCKED ADRs (highest
> precedence) and the structure-plan's golden rules and non-goals. Treat ADR-sourced
> constraints as authoritative; DOC-sourced non-goals as scope guards.

---

## Architectural invariants (ADR-derived — binding)

### CON-event-sourced-truth  [invariant]
- source: docs/adr/ADR-001-event-sourced-core.md; docs/adr/ADR-011; ADR-013
- constraint: The append-only `memory_events` log is the single source of truth.
  Vector/graph/FTS/cache are rebuildable projectors — never a second source of
  truth. Pruning (retention modes) must never pass projector high-water marks; on
  loss, rebuild/audit guarantees degrade explicitly via audit taint.

### CON-slim-core  [invariant]
- source: docs/adr/ADR-002-slim-core-extras.md
- constraint: Zero heavy deps in core. Everything optional lives behind
  `pip install memspine[extra]`. torch/transformers never enter core. Core
  additions (LanceDB ADR-021, litellm ADR-024 lazy-import, aiosqlite/greenlet
  ADR-010, httpx ADR-012) are the only ADR-sanctioned exceptions.

### CON-ports-and-adapters  [invariant]
- source: docs/adr/ADR-004-ports-and-adapters.md
- constraint: Engine/memories/policies talk only to `services/*` capability ports.
  `clients/*` own connections; services never open a connection.

### CON-no-runner-lock-in  [invariant]
- source: docs/adr/ADR-005-background-execution.md
- constraint: Background pipelines are plain, idempotent step functions in
  `workers/pipelines.py`. Runners (inline/DBOS/taskiq) decorate them — no runner
  imports inside pipeline code.

### CON-profiles-stay-green  [invariant]
- source: docs/adr/ADR-015 §5; ADR-020; ADR-021; ADR-006
- constraint: Every change keeps `profile="simple"` behavior stable and
  backward-compatible. Default-off features (E4 quantization, hybrid RRF, ladybug
  graph) must leave the simple profile byte-identical / installable without extras.

### CON-hard-fail-clearly  [invariant]
- source: docs/adr/ADR-006-config-layering.md; CLAUDE.md (D-10)
- constraint: Missing service → `MissingServiceError` naming the extra to install,
  unless `strict_services: false`.

### CON-config-layering-order  [contract]
- source: docs/adr/ADR-006-config-layering.md
- constraint: Config resolves strictly as schema defaults → template `extends:` →
  user YAML → env → kwargs, with per-key source tracking. `extends` cycles rejected.

### CON-async-first  [contract]
- source: docs/adr/ADR-010-async-storage-driver.md; CLAUDE.md
- constraint: Async-first API; thin sync wrappers over the async path. Types
  everywhere; ship `py.typed`; mypy --strict clean.

### CON-prompts-are-data  [contract]
- source: docs/adr/ADR-009-prompt-subsystem.md
- constraint: Every internal LLM call resolves a named, versioned prompt from
  `prompts/defaults/*.yaml`. No inline prompt strings.

### CON-firewall-trust-boundary  [security/NFR]
- source: docs/adr/ADR-013; ADR-014; ADR-018
- constraint: Retrieved/external content is capped low-trust at write; quarantined
  records are excluded from consolidation/promotion until corroborated; the REST
  `/write` path is an untrusted external channel (no-authn REST is a deployment
  contract behind the operator's own boundary).

### CON-storage-dialect-neutral  [contract]
- source: docs/adr/ADR-025-postgres-storage.md
- constraint: Event log + read model must run identically on SQLite and Postgres
  via the shared `SqlStorage` base. No dialect-specific behavior leaks into the
  storage service surface.

### CON-graph-default  [contract]
- source: docs/adr/ADR-015 §5; ADR-003
- constraint: Default graph provider is `sqlite_adjacency` (zero-dep). ladybug
  `[graph]` and kuzu `[kuzu]` are opt-in; the config default stays sqlite_adjacency
  pending a future ADR. Link budget enforced at creation; prune = weight-0 tombstone
  (replay-deterministic).

### CON-vector-lancedb-only  [contract]
- source: docs/adr/ADR-021-lancedb-core-vector.md; ADR-020
- constraint: LanceDB is the sole core vector store. No SQLite brute-force vector
  fallback. E4 rescore is LanceDB-native only.

---

## Scope guards / non-goals (DOC-derived)

### CON-v01-non-goals
- source: docs/memspine-structure-plan.md §7
- constraint: Out of scope for v0.1 — per-namespace type enablement (key reserved);
  third-party memory plugins (interfaces provisional); WS/gRPC/MCP protocols (seats
  reserved); Postgres/Weaviate/Neo4j/Valkey adapters beyond stubs (note: Postgres
  storage since landed via ADR-025); file-native/Markdown profile (D-30 skip);
  litellm as core (superseded — ADR-024 made litellm core lazy-import); benchmark
  leaderboard claims (harness first; the accuracy/tokens/latency triplet rule).

### CON-quality-gates
- source: CLAUDE.md; docs/memspine-structure-plan.md §6
- constraint: Pre-commit gate = ruff (check + format) + mypy --strict + pytest, all
  clean. C6 combination matrix (15 boots) must pass: clean startup, round-trip per
  type, clean shutdown, describe() schema, config-validate golden tests, runner tests.

### CON-oss-posture
- source: docs/adr/ADR-007-public-oss-posture.md
- constraint: Apache-2.0, semver, additive/conservative schema (no destructive DDL),
  mkdocs-material docs + one ADR per decision, evals packaged in-repo.
