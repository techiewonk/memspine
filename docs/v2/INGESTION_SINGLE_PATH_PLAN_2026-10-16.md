# Canonical ingestion: implementation map and verification

Date: 2026-10-16. Status: the authorized single-path consolidation is implemented in the current working tree. This document describes that implementation and records the checks completed so far; it does not claim a full-suite or production-database verification.

## Required behavior

- All message and document ingestion uses durable acceptance and one apply/recovery path. There is no `write.intake_v2` switch.
- Public breaking names are `Engine.ingest_messages` and `Engine.ingest_data`. The old `write_messages` and `Engine.ingest` aliases are removed.
- Identity hierarchy is namespace -> session -> conversations -> messages. Each independent root receives its own conversation unless a conversation is supplied or inherited through a valid reply.
- Supplied message/session/conversation IDs and source times are preserved. Missing IDs and creation times are generated once and persisted.
- Source message IDs are namespace-unique. Replies resolve through source message IDs, then translate to record IDs for the write path. Reusing a message ID with changed source data is rejected until an explicit revision operation is available; hard-erased IDs remain reserved.

## Actual 12-stage file map and boundaries

| # | File(s) | Responsibility and boundary |
|---:|---|---|
| 1 | `api/engine.py`, `api/handle.py`, `api/rest/*`, `api/tools.py` | Public facades and transport adapters. They expose the canonical methods and pass user input to ingestion; they do not implement a second pipeline. |
| 2 | `ingest/message_schema.py` | Defines the validated message batch contract used by the gateway apply adapter. |
| 3 | `ingest/message_identity.py` | Validates and resolves message, session, conversation, sender, time and reply identities. Independent roots remain separate conversations. |
| 4 | `ingest/message_retry.py` | Compares source fingerprints with indexed identities; identical retries reuse the original record, changed payloads are rejected with an explicit-revision-required error. A revision operation is not part of this flow. |
| 5 | `ingest/message_ingestion.py` | Coordinates indexed lookup, pending-intent waiting, retry preparation, identity resolution, reply translation and submission. It does not scan every raw record in the namespace. |
| 6 | `ingest/message_store.py` | Provides namespace-scoped message/conversation lookups, startup backfill from source metadata, and hard-forget tombstoning. It stores no raw message text. |
| 7 | `ingest/gateway.py` | Adapts messages, documents and assets to the common accepted-source flow; routes resolved messages to apply and resumes accepted intents. |
| 8 | `ingest/intake.py` | Validates an envelope once into `ValidatedEnvelope`; `IntakeBook.accept_validated` applies write-ID idempotency, source versioning, quota outcome and receipt/readiness decisions. |
| 9 | `ingest/intake_store.py` | In one database transaction, commits source/write/intent state plus public message and conversation reservations. Apply completion records landed record IDs and clears the text-bearing recovery payload. |
| 10 | `ingest/gateway.py` | Recovers accepted intents, locates writes already landed, translates recovered reply references and advances receipt state. Recovery does not regenerate public identities. |
| 11 | `ingest/message_apply.py` | Applies each resolved turn through the established write door while preserving role/firewall policy, provenance, reply links, attachments and per-turn bookkeeping. |
| 12 | `ingest/write.py`, `workers/windows.py` | `write.py` owns the lower-level write and projection behavior. The window worker owns optional extraction/derivation; prompt tails and batches stop at conversation boundaries while physical session lifecycle remains session-scoped. |

The public message path is:

```text
Engine.ingest_messages
  -> message_ingestion: indexed identities -> retries -> resolved batch
  -> gateway: one ValidatedEnvelope -> IntakeBook.accept_validated
  -> intake_store.commit: source/write/intent + message/conversation reservations
  -> gateway recovery/apply -> message_apply -> write door -> projections
```

`Engine.ingest_data` and asset ingestion enter the same gateway and acceptance lifecycle through their format adapters. They do not select an alternate direct-write route. Optional extraction, embeddings, grounding and derivation remain policy/configuration stages around the common path.

At startup, gateway recovery finishes accepted intents and the identity backfill imports existing `SourceInfo.message` metadata into the lightweight index. The backfill reads record ID, namespace and source metadata only, never content. Hard forget uses the existing common erase hook to replace identity metadata with an `{erased: true}` tombstone, clear its record/intent/write references, and retain the IDs so they cannot be reused.

## What changed from the flag comparison

The old flag did not select two independent memory engines. With intake off, messages went directly through the existing write path. With intake on, durable acceptance wrapped that same lower write path. The consolidated route retains the wrapper guarantees and removes the branch.

| Capability | Old direct route: intake off | Old intake route: intake on | Current canonical route |
|---|---|---|---|
| Public entry | `write_messages`; document ingest | Same entries, dispatched by the flag | `ingest_messages`; `ingest_data`; no old-name aliases |
| Message execution | Direct apply/write | Durable gateway acceptance, then the same apply/write | Identity lookup/resolution, common acceptance, then the same apply/write |
| Envelope validation | Per-turn and write-policy checks | Source envelope normalized in gateway, then write checks | One `ValidatedEnvelope` produced and passed to `accept_validated` |
| Source write IDs | No acceptance-ledger write-ID guarantee | Scope/source/version/position/content write IDs | Retained for durable apply/recovery; distinct from public message IDs |
| Public message IDs | No dedicated source-ID contract | Internal ledger IDs only | Supplied/generated IDs persisted in a namespace-scoped unique index |
| Identical retry | Could create another raw write; semantic dedup was separate | Ledger replay/resume | Indexed source identity and fingerprints return/reuse original result |
| Changed source payload | No message-level revision contract | Source/version conflict rules | Rejected for the same public ID; an explicit revision operation is not yet part of this flow |
| Crash recovery | Appended writes survive individually; no accepted-source intent | Durable intent, landed-record scan and startup recovery | Retained; recovery is a lifecycle step, never a second public mode |
| Quota and receipt | No gateway acceptance receipt | Gateway quota check and receipt/readiness | Retained in the common acceptance stage |
| Documents | Resource extraction/chunk path | Version diff, chunk reuse and retirement coordination | Same gateway lifecycle with document adapter and version handling |
| Assets | Could be refused by intake flag and describer configuration | Asset adapter with describer enabled | No intake-flag dependency; describer remains optional |
| Reply identity | Record ID or earlier-batch position | Same, remapped during recovery | Public source `reply_to` resolves to an internal record ID after validation |
| Conversation grouping | Session/group fields; no distinct conversation resolver | Batch/session-derived source ID | Explicit session/conversation/message hierarchy; roots are independent by default |
| Derivation | Optional windows | Optional windows with readiness tracking | Optional windows respect conversation boundaries and do not leak context tails across them |

## Configuration and lifecycle boundaries

Canonical identity validation, source ownership, durable acceptance, recovery and hard-forget tombstoning are always present. Configuration continues to govern policy: firewall and trust behavior, embeddings and projections, window extraction, grounding, scheduling, summaries and profile layers. Readiness reports only the capabilities enabled and reached by the configured stages.

The intake flag has been removed from active configuration and runtime dispatch. An old configuration file containing the removed key is not a supported way to select behavior. The public API rename is intentionally breaking; there are no compatibility aliases.

## Verification recorded

Final combined verification: **1,586 passed, 6 skipped, 1 deselected** in 334.21 seconds. The run included the changed unit callers, all W2 ingestion wiring tests, ingestion/config/store/contract/REST suites, both ingestion/persistence integration tests, and the batch/dataset/perspective eval-adapter tests. The deselected test is the real GLiNER model check; it was excluded from the final CPU contract pass. This is the affected test set, not the repository's entire suite.

The manual ingest walkthrough passed **43/43 checks**, including date-only precision, role trust, extraction lineage and layer assignment. Ruff passed across `src` and new ingestion tests. Targeted mypy passed the five message schema/identity/orchestration/retry/store modules; the envelope/gateway modules also passed their targeted agent check. The generated stage-parameter reference passes its freshness check.

Regression cases include interrupted acceptance/apply followed by retry, identity conflict transaction rollback, stored parents included later as retries, excluded-message payload changes, hard-erasure tombstones, conversation-isolated extraction context, image-caption single application, and original timestamp precision/zone retention.

Full model-quality benchmarks, PostgreSQL integration, and cross-process concurrency stress tests were not run. Database uniqueness and rollback are tested, but that is not a multi-process exactly-once guarantee. The record-list return and optional sender fields remain the current contract; a richer receipt and revision API are separate decisions.

No commit, push, or production database migration has been performed.

## Run and inspect the implemented path

```python
records = await eng.ingest_messages(
    [
        {
            "message_id": "msg-001", "role": "user",
            "sender": {"id": "user-123", "kind": "human", "name": "Hembad"},
            "content": "I moved from Porto to Lisbon last month.",
            "created_at": "2026-10-01T10:00:00+05:30",
        },
        {
            "message_id": "msg-002", "role": "assistant",
            "sender": {"id": "assistant-main", "kind": "agent", "name": "Personal Assistant"},
            "content": "What do you like about Lisbon?", "reply_to": "msg-001",
        },
    ],
    namespace="demo", session_id="S2",
)
for record in records:
    print(record.record_id, record.source.message)
```

For interactive stepping, break in `message_ingestion._ingest_once`,
`message_identity.normalise_messages`, `gateway.IntakeGateway.submit`,
`intake_store.commit`, and `message_apply.apply_turns`. These are distinct stages
of the same path. Step into `write.write` for policy/event/projection work and
`workers.windows` for optional derivation. No intake-mode switch is required.

The public return remains a list of deposited records. Skipped turns do not
produce records; a richer per-message receipt remains an API design decision.
Sender fields are individually optional; supplied kind is validated against
human/agent/service. Missing source time is explicitly marked generated, and
`skip_defaulted_dates` continues to suppress treating that time as historical
source evidence. Image captions are folded into validated content exactly once.
