# Records store contract (`storage.records`)

ADR-081 makes `storage.records` the authoritative slot and requires it to be independent of
SQL. This page is the contract a provider must meet. It is written for the next provider
(MongoDB, RM-112), but SQLite and Postgres are held to it today. Status lives in
`docs/PROVIDER_TODO.md` (RM-111, RM-112).

- **Port:** `src/memspine/services/storage/records_port.py` (`RecordsStore` and its
  facets). The engine needs nothing from the authoritative store beyond this port.
- **Conformance suite:** `tests/unit/services/storage/test_records_conformance.py`. A
  provider is conformant when every case passes for it. To add a provider, add a name to
  `PROVIDERS` and a branch in the `handle` fixture that returns a started store and, if the
  data can be opened twice, a `reopen` factory for a second independent handle (a second
  process).
- **Implementations:** `SqlStorage` (`services/storage/sql_base.py`), with `SQLiteStorage`
  and `PostgresStorage` supplying schema bring-up and leases.

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
- MongoDB needs a replica set (multi-document transactions) to meet this.

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

`upsert_record` (an idempotent upsert keyed by `record_id`), `get_record`, `list_records`
(by namespace, optionally by type or group), `list_namespaces`, `find_active_fact`
(semantic only, open validity, not quarantined, latest), `list_session_records`, and
`delete_all_records` (rebuild: the read model is dropped and the log is kept).

## 6. Lineage

`add_lineage` is idempotent per `(child, parent, role)` and returns the number of edges
actually added. `lineage_parents` and `lineage_children` return edges in insertion order
and can filter by role. `delete_lineage` returns the number of edges removed.

## 7. Erasure

- `redact_event_payloads(record_ids)` is the only sanctioned mutation of the log. It removes
  every snapshot of the records' content from every event. Each affected event keeps its
  `seq`, kind and original fingerprint, and gets `redacted: true`.
- It returns the rewritten sequence numbers. Running it again rewrites nothing.
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

## Known gaps found by the suite (2026-10-23)

- **SQLite `:memory:` with concurrent writers.** The in-memory store is a shared-cache
  database. Its table locks (`SQLITE_LOCKED`) are not retried by `busy_timeout`, so two write
  transactions in flight at once fail with "database table is locked". The two
  concurrent-writer cases are strict `xfail` for `sqlite_memory`. File SQLite and Postgres
  pass. The in-memory lease table is single-threaded, so the thread-race case is skipped
  there.
- **Fixed in this change.** `add_lineage` on Postgres returned -1 per edge, because
  psycopg leaves `rowcount` at -1 for an insert with implicit `RETURNING`. It now counts
  the returned rows.
