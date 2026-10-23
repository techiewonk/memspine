# ADR-081: One storage block with a slot per data category, plus a deployment block

**Status:** Accepted (design) on 2026-10-23. Implementation is RM-111; providers are
RM-112.
**Date:** 2026-10-22 (proposed), 2026-10-23 (accepted)
**Deciders:** Owner (RM-108 discussion, 2026-10-22 and 2026-10-23)
**Supersedes:** the `storage.profile` selector (F-16d / M-04 C, plan Q11 "preserve SQL
bring-up modes"). It also supersedes, for data stores only, the scattering of provider
selection across `storage`, `vector`, `graph`, `cache` and `read.lexical_*`.
**Relates to:** ADR-068 (provider contracts, no aliases), ADR-069 (remote providers),
ADR-079 (shared resources), ADR-080 (graph qualifiers).

## Context

The 2026-10-22 review (`docs/STORAGE_TOPOLOGY_REVIEW_2026-10-22.md`) found the
following.

- `storage.profile` switches one behaviour: Alembic versus `create_all` on Postgres. A
  Postgres store selected without `profile: server` silently gets no migration history.
- Data-store providers are chosen in five places: `storage`, `vector`, `graph`, `cache`
  and `read.lexical_*`.
- File-based stores are placed relative to the SQL provider's path or `data_dir`, even
  when every selected store is remote.
- Two nodes sharing one Postgres share projector offsets but keep node-local indexes, and
  nothing validates against this.
- Graph `auto` changes its target when an optional extra is installed.
- Side files (`.sleep`, `.cost`, `.deadletters`) bypass SQLCipher.

The owner wants many data providers and service providers, each selected separately.

## Decision

1. **Data providers live under one `storage` block, one slot per data category.** Each
   slot is `{provider, settings}`:
   - `storage.records`: event log, records, lineage, offsets, outbox and job store,
     sleep leases, cost and economy ledgers, dead letters, deployment identity.
   - `storage.vector` and `storage.lexical` (both required), `storage.graph`,
     `storage.graph_mirror` (optional), `storage.cache`, and `storage.documents`
     (optional; non-structured payloads such as attachments, assets, large payloads and
     ingest blobs; the event log keeps references and content hashes).
   - **`records` is not tied to SQL.** Its storage port is defined by a SQL-independent
     contract and conformance suite:
     - append-only event log with a monotonic sequence;
     - atomic append-plus-outbox;
     - projector offsets and leases;
     - lineage and erasure queries;
     - versioned schema and index setup.

     SQLite and Postgres implement it today. MongoDB, with a replica set for
     multi-document transactions, is a planned provider (RM-112).
   - **Weaviate can serve the `documents` slot** (objects with properties), alongside its
     vector and lexical roles. It cannot serve `records`: it has no ordered append-only
     log and no multi-object transactions. MongoDB can serve `records` and `documents`
     (GridFS for large payloads). Filesystem and S3-compatible stores are the other
     planned `documents` providers.
2. **Service providers stay outside `storage`.** These are compute: `embedding` (the
   embedding model), `llm`, `models` (profiles and bindings; GLiNER, rerankers, decider).
   The vector store that holds embeddings is the `storage.vector` slot. The embedding
   model is not storage.
3. **Settings are per slot (owner choice).** There is no shared `connections` block. Two
   slots that point at the same target share one client or pool automatically, through
   the ADR-079 registry keyed by target identity and settings, as today.
4. **A `deployment` block.**
   - Fields: `deployment.id` (replaces `runtime.deployment_id`), `deployment.locality`
     (`single_process` | `multi_process` | `multi_node`) and `deployment.data_dir`.
   - Every provider descriptor declares a locality trait: `process` (memory, `:memory:`),
     `host` (sqlite, lancedb, tantivy, ladybug, disk cache) or `cluster` (postgres,
     pgvector, opensearch, weaviate, neo4j, redis/valkey).
   - **Validation rules:**
     - `multi_node` rejects host-local slots.
     - `multi_process` rejects single-writer host-local stores (tantivy, ladybug) outside
       the writer process.
     - `data_dir` is required only when a host-local slot is selected; it defaults to
       today's placement base, so paths and projection fingerprints do not change.
5. **No profile.**
   - `storage.profile` is removed, with no alias (ADR-068).
   - Every durable SQL database is migrated with Alembic; `create_all` is used only for
     `:memory:`.
   - Postgres migrations take an advisory lock.
   - Existing `create_all` Postgres databases are adopted on first start by the existing
     legacy stamp and upgrade.
6. **Pinned slots.** `storage.graph: auto` (and any future `auto`) resolves once and is
   recorded in the projector binding. Installing an extra later never changes it; a
   change is an explicit provider change (ADR-068 controlled changes).
7. **Side files move into `storage.records`.** Sleep leases, the cost ledger and dead
   letters are stored there: encrypted with the database on SQLite, shared on Postgres. A
   one-time import reads any existing side file.
8. **Mirror gate by capability.** `storage.graph_mirror` requires a provider with the
   `mirror` capability, not a profile.
9. **Templates instead of profiles.** `embedded` and `server` presets ship as config
   templates that fill the slots. They are data, not a runtime switch.

## Owner selections (2026-10-23)

- **Slot naming:** `storage.<slot>`, with settings per slot (no shared `connections`
  block).
- **Deployment block:** included in the same change.
- **Graph in the embedded template:** `auto`, resolved once and pinned.
- **`multi_process`:** single-writer host-local stores hard-fail outside the writer
  process.
- **Required slots:** `vector` and `lexical`.
- **Rename:** `sqlite_adjacency` becomes `sql_adjacency` (SQLite and Postgres), with no
  alias.
- **Non-structured stores:** both the SQL-independent `records` contract and the optional
  `documents` slot. The contract ships now; the MongoDB provider comes later.
- **Weaviate:** used as a `documents` provider where possible.

## Consequences

- One place shows where every piece of memory lives. Remote-only deployments need no
  `data_dir`. Multi-node mistakes fail at configuration time.
- This is a breaking configuration change across `storage`, `vector`, `graph`, `cache`,
  `read.lexical_*` and `runtime.deployment_id`. It means:
  - templates, USAGE, the config baseline, e2e and unit configs and examples are updated
    in the same change;
  - the eval harness configs move with PA-107;
  - there are no aliases.
- Implementation notes (RM-111 WP6 and the documents slot, 2026-10-23): the locality rules
  are `memspine/providers/locality.py`, run in `check_capabilities` before any provider
  opens; the writer process is designated by `deployment.writer: true` (usually
  `MEMSPINE_DEPLOYMENT__WRITER=true`); the declared trait wins over the endpoint, so a cluster
  provider behind a loopback sidecar stays `cluster`; outside `single_process` an `auto` slot
  must be explicit; `storage.documents` ships with the `filesystem` provider (port
  `services/documents/base.py`, connector `clients/fs`), host-local and single-writer. Details
  in `docs/STORAGE_SLOTS_MIGRATION_PLAN.md` section 9. RM-112 adds the `s3` (`[aws]`,
  connector `clients/s3`) and `weaviate` (shared Weaviate connector, dedicated non-vectorized
  collection) documents providers, both `cluster`; Weaviate still never serves `records`.
- Behaviour flags stay where they are: `graph.edge_compiler_v2`, `graph.key_v2`,
  `graph.entity_embeddings`, `read.lexical_scorer` / `lexical_language`, `event_log.*`
  and `embedding.*`. Only the provider selection moves.
