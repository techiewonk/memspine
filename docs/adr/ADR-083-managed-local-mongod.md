# ADR-083: Opt-in managed local mongod for the MongoDB provider

**Status:** Accepted (design) on 2026-10-23. Implementation is RM-119.
**Date:** 2026-10-23
**Deciders:** Owner ("Managed launcher (ADR)", 2026-10-23)
**Relates to:** ADR-081 (storage slots, locality), ADR-079 (shared resources),
ADR-068 (no aliases), RM-112 (MongoDB records and GridFS documents).
**Evidence:** [rm112-local-mongo-options-2026-10-23.md](../provider_verification/rm112-local-mongo-options-2026-10-23.md)

## Context

The MongoDB records provider needs multi-document transactions, which need a replica set.
The 2026-10-23 spike tested library-style alternatives on Windows without Docker:

- PoloDB, Mongita and MontyDB fail the records conformance suite at the first capability;
  they have no sessions or transactions.
- pymongo_inmemory cannot start a replica set.
- FerretDB has no Windows path.

Only the official `mongod` binary, started as a one-member replica set, passed: 15/15 records
conformance and a GridFS round trip. It became primary in about 1 s, and only `mongod.exe`
(157 MB) from the zip is needed. The owner wants MongoDB usable locally without manual
server setup.

## Decision

1. **Opt-in launcher in the provider settings.** The `mongodb` provider of `storage.records`
   and `storage.documents` gains `launch`:
   - `mode: external` (the default) connects to `settings.uri` as today.
   - `mode: managed` makes memspine start and own a local `mongod`.
2. **Managed settings:**
   - `version`: required, an exact pin such as `9.0.4`; never "latest".
   - `port`: required; never picked implicitly.
   - `data_dir`: defaults to `<deployment.data_dir>/mongod/<port>`.
   - `binary`: optional path to an existing `mongod`.
   - `download`: default `false`.
   - `replica_set` (default `rs0`), `start_timeout_s`, `cache_size_gb` (optional
     WiredTiger cap).
3. **Download only on explicit consent.** Without `binary`, `download: true` is required.
   - The archive comes only from MongoDB's official download host for the pinned version and
     platform.
   - It is verified against the published SHA-256 before extraction.
   - Only the `mongod` executable is extracted, into a short cache path. Windows long-path
     limits broke pymongo_inmemory.
   - memspine never ships or vendors the binary, which is under the SSPL; the launcher prints
     that licence notice once.
   - No new dependency: stdlib plus pymongo.
4. **Lifecycle.**
   - The connector (`clients/mongodb`) gains a launcher component owned by the client layer.
   - Start runs `mongod --replSet <rs> --bind_ip 127.0.0.1 --port <p> --dbpath <dir>`.
   - Initiation is idempotent: `replSetInitiate` runs only when no config exists. The launcher
     then waits for PRIMARY within `start_timeout_s`.
   - The connection uses `mongodb://127.0.0.1:<p>/?directConnection=true&replicaSet=<rs>`.
   - The process is shared through the ADR-079 registry: one `mongod` per (binary, port,
     data_dir) in a process, reference-counted, stopped cleanly with `shutdown` on the last
     release.
   - A `mongod` already listening on the port with the same replica set is adopted, never
     killed. A different process on the port is a start error naming the port.
5. **Locality.** A managed slot is `host`-local:
   - `deployment.locality: multi_node` rejects it.
   - Under `multi_process`, only the writer (`deployment.writer: true`) may use
     `mode: managed`. Other processes use `mode: external` with the writer's port; the error
     message says so.
6. **Bind address.** The managed server binds loopback only. Exposing it is out of scope; use
   `mode: external` for that.
7. **Defaults unchanged.** SQLite records and filesystem documents stay the embedded default.
   The managed launcher is never used unless configured.

## Consequences

- A single config block gives a working local MongoDB on Windows, macOS and Linux with no
  Docker and no admin, after a one-time explicit download.
- On a clean Windows machine `mongod.exe` may need the VC++ runtime. The launcher detects the
  exit code and names the missing runtime instead of timing out.
- Tests:
  - Launcher unit tests with a fake binary and archive.
  - Checksum-mismatch refusal.
  - Port-conflict and adoption cases.
  - A live test that skips unless `MEMSPINE_TEST_MONGOD_BINARY` (or an opted-in download) is
    available and runs the records and documents conformance suites against a managed server.
