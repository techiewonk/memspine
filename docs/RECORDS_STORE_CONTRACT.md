# Records store contract (`storage.records`)

ADR-081 makes `storage.records` the authoritative slot and requires it to be independent of
SQL. This page is the contract a provider must meet. SQLite, Postgres and MongoDB (RM-112) are
held to it. Status lives in `docs/PROVIDER_TODO.md` (RM-111, RM-112).

- **Port:** `src/memspine/services/storage/records_port.py` (`RecordsStore` and its
  facets). The engine needs nothing from the authoritative store beyond this port.
- **Conformance suite:** `tests/unit/services/storage/test_records_conformance.py`. A
  provider is conformant when every case passes for it. To add a provider, add a name to
  `PROVIDERS` and a branch in the `handle` fixture that returns a started store and, if the
  data can be opened twice, a `reopen` factory for a second independent handle (a second
  process).
- **Implementations:** `SqlStorage` (`services/storage/sql_base.py`), with `SQLiteStorage`
  and `PostgresStorage` supplying schema bring-up and leases; `MongoRecordsStore`
  (`services/storage/mongodb.py`, `[mongodb]` extra, RM-112) over the `MongoConnector`
  (`clients/mongodb/connector.py`). See "MongoDB" below.
- **One contract.** The engine reaches the records store only through this port. RM-112 moved
  the read-model helpers, the marker scrub, the deployment identity and the projector bindings
  into it, and routed every lineage call through the lineage facet. Features that keep their
  own SQL tables are outside the port and are refused for a provider without SQL (section 10).

## 1. Event log

- `append_event` assigns `seq`. Sequences are strictly increasing in commit order and are
  never reused, including after pruning or a rolled-back append. Gaps are allowed (a
  Postgres identity skips a rolled-back value).
- Concurrent appenders, both tasks in one process and separate handles on the same data,
  get distinct sequences.
- `read_events(after_seq, limit)` returns events with `seq > after_seq` in ascending
  `seq` order. Paging by the last `seq` replays the log exactly once, in order.
- The payload round-trips byte-identically, so the stored fingerprint still matches.
- An event that already has a `seq` is refused: events pass the write door once.
- `prune_events` deletes nothing in `full` mode. In `rolling` mode it never deletes past
  the slowest projector's offset.

## 2. Atomic append plus outbox

- `append_with_outbox(event, items)` commits the event and every outbox item in one atomic
  unit. If anything fails, neither the event nor any item is visible afterwards. The suite
  injects two faults: an item that fails while it is being encoded, and an item that
  violates a store constraint.
- Item offsets continue per `(scope, stage)` from 0 and are returned in item order.
  `outbox_items(scope, stage, after)` reads them back in ascending order. A failed commit
  leaves no offset behind.
- Concurrent appenders to one `(scope, stage)` either serialise or fail the whole commit;
  they never half-apply. Postgres takes a transaction advisory lock per `(scope, stage)`.
  SQLite has a single writer.
- MongoDB needs a replica set (multi-document transactions) to meet this. Its connector
  refuses a standalone server at `open()`.

## 3. Projector offsets

- `set_offset` only moves forward: a lower value is ignored, and concurrent checkpoints
  keep the highest.
- `reset_offset` is the one sanctioned way to move an offset back (rebuild).
- Offsets are durable and shared by every handle on the same data.

## 4. Leases

`leases()` returns a synchronous lease table with `claim`, `heartbeat`, `release` and
`get_lease`. Lease holders run in threads and processes as well as on the event loop.

- A key has at most one live holder. Of concurrent claims on one key, exactly one wins.
- A lease expires after its TTL. Every takeover bumps the `epoch`.
- A stale holder is fenced: its `heartbeat` returns None and its `release` returns False.
- On a cluster store, expiry is judged by the store's clock, not the host's (Postgres uses
  `clock_timestamp()`).

## 5. Record read model

`upsert_record` (an idempotent upsert keyed by `record_id`), `get_record`, `get_records`
(RM-115: the existing records among a list of ids keyed by id, missing ids absent, equal to one
`get_record` per id; SQL chunks SQLite `IN` lists at 900 ids and binds one array on Postgres,
MongoDB uses one `$in` query), `list_records`
(by namespace, optionally by type or group), `list_namespaces`, `find_active_fact`
(semantic only, open validity, not quarantined, latest), `list_session_records`, and
`delete_all_records` (rebuild: the read model is dropped and the log is kept).

Read-model helpers (RM-112; the firewall, the profile card and the sleep stages call them):

- `quarantined_ids(ids)`: the ids among `ids` whose record exists and is quarantined.
- `recent_contents(namespace, limit)`: the contents of the `limit` most recently recorded
  non-quarantined records, oldest first. Records with equal `recorded_at` keep the
  `list_records` order; `limit <= 0` gives an empty list.
- `layer_counts(namespace)`: live records (status `activated` or `resolving`, not quarantined)
  by layer. A record stored without a layer counts on the layer it hydrates to.
- `count_quarantined(namespace)` and `list_quarantined(namespace)` (in `list_records` order).

## 6. Lineage

`add_lineage` is idempotent per `(child, parent, role)` and returns the number of edges
actually added. `lineage_parents` and `lineage_children` return edges in insertion order
and can filter by role. `delete_lineage` returns the number of edges removed.

## 7. Erasure

- `redact_event_payloads(record_ids)` is the only sanctioned mutation of the log. It removes
  every snapshot of the records' content from every event. Each affected event keeps its
  `seq`, kind and original fingerprint, and gets `redacted: true`.
- It returns the rewritten sequence numbers. Running it again rewrites nothing.
- `scrub_marker_payloads(marker, scrub)` (in the port since RM-112) is the same pass for
  MARKER events named `marker` (the profile card). `scrub` mutates a decoded payload and returns
  True when it changed it; a changed event gets `redacted: true` and keeps its seq, kind and
  fingerprint. It returns the rewritten seqs and is idempotent.
- `delete_record` hard-deletes from the read model. `list_children(namespace, parent_ids)`
  finds the derived records that erasure must visit.
- After erasure the content cannot be read back through any port method.

## 8. Schema and lifecycle

- `start()` brings collections or tables and indexes to the current version and is
  idempotent. SQL stores use Alembic for durable databases and `create_all` for
  `:memory:`.
- `schema_revision()` names the applied version: an Alembic revision, or the
  collection-setup version for MongoDB.
- `stop()` releases what the store opened, including the lease connection.

## 9. Provider metadata (RM-112)

- `claim_deployment_id(candidate)` stores `candidate` as the deployment identity unless one is
  stored, and returns the stored one. The first writer wins, and concurrent callers in one
  process or several agree. `providers/identity.py::resolve_deployment` rejects a configured
  `deployment.id` that differs from the stored one.
- `projection_bindings()` returns the projector target bindings (`ProjectionBindingPort`),
  stored beside the offsets they gate.
  - `ensure(binding)` creates a binding once. It refuses a non-zero offset that has no binding
    (the target may have changed under the same projector name), accepts the same target again,
    and refuses another provider, target or generation. It replaces a binding only through a
    verified switch: the stored binding equals the switch source and the offset equals the
    verified checkpoint (a compare-and-swap).
  - `bound_provider` names the provider a projector is bound to (the ADR-081 `auto` pin).
  - `adopt_renamed_provider` rewrites a binding recorded under a renamed provider, once.
- SQL: `services/storage/provider_metadata.py` (tables `provider_deployment_identity` and
  `provider_projector_bindings`). MongoDB: the `meta` document `deployment` and the
  `projector_bindings` collection.

## 10. Features outside the port

These features keep their state in SQL tables of the records database and are not part of the
contract. `providers/records_features.py::validate_records_features` refuses them when the
records descriptor declares no `sql` capability. It runs before any provider opens and names
every gap at once.

| Feature | Why it needs SQL |
|---|---|
| `workers.outbox` | the outbox job book (`runtime/outbox.py`) is a SQL table |
| `offline.sleep` | sleep leases, cycle watermarks and triggers are side tables (RM-111 WP5); the blocking `sleep()` works |
| `economy.ledger_persist` | the persisted cost ledger is a side table |
| `write.envelope` | its dead-letter list is a side table |
| `runtime.service_switches.projections` | controlled projection switches keep their manifests in SQL tables |
| `storage.vector: pgvector` | the vectors live in the Postgres records database |
| `storage.graph: sql_adjacency` | the graph lives in the SQL records database; `auto` resolves only to `ladybug` and fails naming `storage.graph` when it is not installed |

Two more need SQL and fail at call time, naming the gap: the intake gateway
(`ingest_messages`, `ingest_data` and `ingest_asset` use the intake book and the message index)
and the provider-change controller. The side-file import and the projection switch diagnostics
are skipped. Moving these onto the port is follow-up work.

## MongoDB (RM-112, 2026-10-23)

`MongoRecordsStore` meets every case of the suite against a one-member replica set
(`mongo:7`). It is registered as `storage.records: {provider: mongodb}` (`[mongodb]` extra,
`providers/builtin/mongodb.py`): settings `MongoRecordsSettings`, descriptor locality `cluster`,
`deployment.id` required, `acquire()` through the shared connector, no SQL access. How it maps
the contract:

- **Topology.** `MongoConnector.open()` runs `hello` within the connect deadline and refuses
  a standalone `mongod` (`StandaloneServerError`: no multi-document transactions, so
  append-plus-outbox could not be atomic). A replica set or a sharded cluster is accepted.
  Locality is `cluster`.
- **Sequence.** `events._id` is the `seq`. It comes from the `events` counter document
  (`findOneAndUpdate` with `$inc`) inside the append transaction. Two appenders conflict on
  the counter and one retries, so sequences are dense, strictly increasing in commit order
  and never reused; an aborted append rolls the counter back too. Every append, with or
  without outbox items, is a transaction (snapshot read concern, majority write concern;
  the driver retries transient conflicts with backoff). A non-transactional counter would
  let a reader see seq n+1 before n commits and skip n.
- **Outbox.** Each `(scope, stage)` has its own counter document in the same transaction.
  The store-side constraint is a `$jsonSchema` validator on `outbox` (stage, scope fields,
  offset and payload typed and required); a violating item aborts the transaction.
- **Payload.** The canonical JSON bytes the fingerprint hashed are stored as BSON binary
  (optionally zstd), not as a BSON document, so they round-trip exactly (no int64 or
  key-order drift).
- **Offsets.** `$max` upsert: advance-only and race-free; `reset_offset` deletes the mark.
- **Leases.** One atomic single-document upsert per operation, on a sync client the connector
  lends (lease holders run in threads). Expiry is judged by the server clock (`$$NOW`). The
  claim is a conditional update pipeline that writes the claimant's token only when the
  lease is absent or expired; the claim won when its token comes back. (`$expr` is not
  allowed in an upsert filter, so the condition lives in the pipeline.) The cross-handle and
  16-thread race cases pass.
- **Read model.** One document per record: the record's own pydantic JSON (lossless: bytes as
  base64, big integers exact) plus indexed key fields (namespace, type, group, entity,
  attribute, open validity, status, quarantine, a sortable `recorded_at`, session key,
  role, `source.parents`). `list_records` returns insertion order.
- **Lineage.** Unique `(child, parent, role)` index; a counter reserves an insertion-order
  block per call so parents/children come back in insertion order across processes.
- **Erasure.** The redaction pass rewrites each affected event with a compare-and-set on its
  old payload bytes. It is not one transaction over the whole log (as in SQL), but each
  rewrite is atomic and the pass is idempotent, so a crash mid-pass is finished by running
  it again.
- **Read-model helpers.** Each record document also stores its `layer`. `recent_contents`
  sorts by `(recorded_key, order)` descending with a limit; `layer_counts` is one `$group`.
- **Provider metadata.** `meta._id = "deployment"` is claimed with `$setOnInsert`. A projector
  binding is one document in `projector_bindings`: creation is an insert that loses to a
  concurrent first writer, and a verified switch is an `update_one` filtered on the old
  provider, target and generation.
- **Schema.** `meta.schema` holds the collection-setup version (`mongodb-records-v1`).
  `start()` creates collections, the validator and indexes idempotently and refuses a
  database written by a newer version.
- **Modes.** `full` and `rolling` (prune never passes the slowest projector). `ephemeral` is
  refused: a cluster store that persists nothing has no use, and an in-process seq would
  not be shared.

Engine end to end (`tests/e2e/test_mongodb_engine.py`, live replica set): write, search,
read, hard forget and sleep; a restart that keeps the identity; `storage.graph: auto` pinned to
LadybugDB; a GridFS documents slot sharing the records connector; and every SQL-only feature
refused before a collection is created.

## Known gaps found by the suite (2026-10-23)

- **SQLite `:memory:` with concurrent writers.** The in-memory store is a shared-cache
  database. Its table locks (`SQLITE_LOCKED`) are not retried by `busy_timeout`, so two write
  transactions in flight at once fail with "database table is locked". The three
  concurrent-writer cases (appenders, checkpoints and, since RM-112, identity claims) are
  strict `xfail` for `sqlite_memory`. File SQLite and Postgres
  pass. The in-memory lease table is single-threaded, so the thread-race case is skipped
  there.
- **Fixed in this change.** `add_lineage` on Postgres returned -1 per edge, because
  psycopg leaves `rowcount` at -1 for an insert with implicit `RETURNING`. It now counts
  the returned rows.
