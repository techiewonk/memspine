# Phase 15: Documentation & Usage Finalization - Context

**Gathered:** 2026-07-10
**Status:** Ready for planning
**Source:** /gsd-discuss-phase (default mode)

<domain>
## Phase Boundary

Finalize the **full user-facing doc set** so it describes the shipped engine
*exactly*, positions memspine as the strongest composable memory engine in its
class, and can never silently drift from the code again. Clarifies HOW to write
the docs — not WHETHER to add engine capabilities (those are their own phases).

**Goal (ROADMAP):** the user-facing docs describe exactly what the shipped
engine does and how to drive it.
</domain>

<decisions>
## Implementation Decisions

### Doc-set scope — FULL set (not just the 3 named docs)
- This phase finalizes **all** user-facing docs as one coherent pass, so every
  doc agrees with the shipped code *and with each other*:
  - `docs/USAGE.md`, `docs/FEATURES.md`, `README.md` (REL-01 named)
  - `docs/ARCHITECTURE_FLOWS.md`, `docs/ECOSYSTEM_COMPARISON.md` (user added; reconcile)
  - a **config-key reference** covering every `MemspineConfig` key.

### Positioning — lead with "composable & swappable"
- **Lead claim:** *"memspine is the memory engine where every store —
  vector / graph / cache / SQL storage / LLM / embedding / secrets — is swappable
  by config alone, over an event-sourced, rebuildable core."* (Aligns with the
  PROJECT.md success metric.)
- Also foreground: 9 memory types, Memory Firewall (E1), hybrid + graph
  retrieval (RRF), background learning dynamics, slim core.
- **Ecosystem comparison:** a summarized comparison table in `README.md`; the
  full deep-dive (vs EverMemOS / cognee / honcho / SimpleMem, etc.) lives in
  `docs/ECOSYSTEM_COMPARISON.md`.

### Example depth — comprehensive & runnable
- `USAGE.md` walks **install → Engine → CLI → REST**, each with a runnable,
  copy-paste snippet against the *current* API.
- A **"swap a backend"** section shows each swap as a config diff:
  `storage sqlite→postgres`, `llm local→litellm/bedrock`,
  `cache memory→redis/lmdb`, `secrets env→AWS` (`MEMSPINE_SECRETS_BACKEND`),
  `embedding →litellm (+dim)`.
- A **full config-key reference table** for every `MemspineConfig` block
  (event_log / storage / vector / cache / embedding / llm / read / graph /
  workers / secrets), including the keys added this session.

### Drift-prevention — automated doc-test (structural guarantee)
- Add an automated test (under `tests/`) that **extracts** documented config
  keys — and, where feasible, CLI verbs, REST routes, and fenced ```python
  examples — and **asserts each resolves against the shipped code**
  (`MemspineConfig` schema fields, `cli.py` commands, the REST router). This
  makes success-criterion 3 ("no drift") a standing guard, not a one-time audit.

### Claude's Discretion (not asked — implementation detail)
- Exact section ordering, prose, and which examples to feature per doc.
- Whether the config-key reference is **hand-written or generated** from the
  pydantic schema — prefer generated/schema-introspected if it's cheap, so it
  can't drift either.
- The precise extraction strategy for the doc-test (regex vs a small parser).
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Doc targets (what this phase edits)
- `docs/USAGE.md` — install→Engine→CLI→REST walkthrough (primary REL-01 target)
- `docs/FEATURES.md` — catalog of 9 memory types, profiles, config keys
- `README.md` — top-level positioning + summarized comparison table
- `docs/ARCHITECTURE_FLOWS.md` — architecture/stage flows (reconcile)
- `docs/ECOSYSTEM_COMPARISON.md` — competitive deep-dive (reconcile)

### Source-of-truth for "no drift" verification
- `src/memspine/config/schema.py` — **the** authority for every config key (`MemspineConfig` and all sub-configs)
- `src/memspine/cli.py` — CLI verbs
- `src/memspine/protocols/rest/` — REST routes
- `src/memspine/engine.py` — the public `Engine` API surface + `describe()`

### Decision records the docs must reflect (this session's changes)
- `docs/adr/ADR-021-lancedb-core-vector.md` — LanceDB sole vector store
- `docs/adr/ADR-022-shared-cache-backends.md` — cache backends
- `docs/adr/ADR-023-pluggable-secrets-aws.md` — `MEMSPINE_SECRETS_BACKEND`
- `docs/adr/ADR-024-litellm-llm-embedding.md` — litellm model-prefix routing (BREAKING config)
- `docs/adr/ADR-025-postgres-storage.md` — `storage.backend/url/data_dir`
- `docs/memspine-structure-plan.md` — authoritative blueprint (register D-01…D-54)

### Planning inputs
- `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md` (REL-01), `.planning/intel/SYNTHESIS.md`
</canonical_refs>

<specifics>
## Specific Ideas

- **User's overarching target:** *"develop the best memory system"* — the docs
  must make the composability **and** correctness case explicitly, not just
  describe the API.
- **New config surface to document** (added this session, currently under-doc'd):
  `storage.backend|url|data_dir`, `vector.backend` (lance sole),
  `cache.backend|path|url|namespace|default_ttl_seconds`,
  `embedding.provider=litellm` + `dim`, `llm.roles.<role>.model` (prefix routing:
  `openai/`, `ollama/`, `bedrock/`, `vertex_ai/`, `llamacpp/<path>`) +
  `api_base`/`aws_region`, `read.rerank=litellm` + `rerank_model`, and the
  `MEMSPINE_SECRETS_BACKEND` bootstrap env var.
- **BREAKING change to call out:** `llm.roles.<role>` dropped `provider`/`base_url`
  in favor of `model` prefix + `api_base` (ADR-024) — the migration note belongs
  in USAGE/README.
</specifics>

<deferred>
## Deferred Ideas

- **Live-backend contract verification** (real Postgres/Redis/LLM round-trips) —
  already roadmapped as **Phase 16**; not part of this docs pass.
- **Alembic migration squash** — **Phase 17**.
- **mkdocs site build/publish** — only if not already wired; note for a follow-up.

## ⚠ Coordination hazard
- A concurrent session was mid-edit on `USAGE.md` / `FEATURES.md` (the exact
  files this phase finalizes). **Execution must confirm that session has settled
  first**, or plan around it, to avoid documenting a moving target / clobbering.
</deferred>

---

*Phase: 15-documentation-usage-finalization*
*Context gathered: 2026-07-10 via /gsd-discuss-phase*
