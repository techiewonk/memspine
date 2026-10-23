# ADR-083: Opt-in managed local mongod for the MongoDB provider

**Status:** Accepted (design) on 2026-10-23. Implemented by RM-119 on 2026-10-23 (see the
implementation notes at the end).
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

## Implementation notes (RM-119, 2026-10-23)

- **Settings.** `MongoLaunchSettings` (`config/schema/providers.py`) is `settings.launch` on
  `storage.records` provider `mongodb` (default `{mode: external}`) and on `storage.documents`
  provider `mongodb` (default unset: inherit the records slot's connection and launcher).
  Validation errors name the key: `launch.version` (pattern `X.Y.Z`) and `launch.port` are
  required with `mode: managed`; `mode: managed` without `launch.binary` requires
  `launch.download: true`; managed-only keys under `mode: external` are refused; `settings.url`
  must be unset with `mode: managed` (it is derived) and is required with `mode: external`.
  One key beyond the decision: `launch.cache_dir` (default the user cache directory,
  `%LOCALAPPDATA%\memspine\mongod` on Windows, `~/Library/Caches/memspine/mongod` on macOS,
  `$XDG_CACHE_HOME/memspine/mongod` elsewhere), which holds `<version>/mongod[.exe]`.
- **Launcher.** `clients/mongodb/launcher.py`, `MongodLauncher` (a `Connector`, scope `thread`,
  locality `host`), acquired through `providers.connectors.acquire` as `records.mongod` /
  `documents.mongod` before the `MongoConnector`, which depends on it and so closes first.
  Registry key: (binary path, port, data_dir), replica set as a knob.
  - Binary: explicit path (its `--version` must match `launch.version`), else the cache, else a
    consented download. Candidate archives are on `https://fastdl.mongodb.org` only (Windows zip,
    macOS tgz, Linux tgz per distribution from `/etc/os-release`); the digest is MongoDB's
    published `<archive>.sha256` on the same host (smaller than the 51 MB `full.json` the spike
    read). The archive is hashed while it streams to disk, refused on a mismatch before
    extraction, and deleted after only `bin/mongod[.exe]` is written atomically.
  - Start: `--replSet --bind_ip 127.0.0.1 --port --dbpath --logpath <dbpath>/mongod.log
    --logappend [--wiredTigerCacheSizeGB]`; console output goes to `mongod.console.log`.
    `replSetGetStatus` code 94 (NotYetInitialized) triggers the one `replSetInitiate`
    (AlreadyInitialized is ignored); the launcher polls `hello` until `isWritablePrimary`
    within `start_timeout_s`, terminates its process on a timeout and reports the log tail. An
    exit with 0xC0000135 (STATUS_DLL_NOT_FOUND) names the missing VC++ runtime.
  - Port: a listener that answers `hello` with the same `setName` is adopted and never stopped;
    a non-mongod listener or another replica set is `MongodPortConflictError` naming the port.
  - Close: `shutdown` (then `force: true` if refused), wait up to 8 s
    (`MONGOD_SHUTDOWN_TIMEOUT_S`, below the 10 s lifecycle close timeout), then terminate/kill.
  - The SSPL notice is printed to stderr once per process at the first managed open.
- **Locality.** The descriptors resolve a managed slot to `host` (an inheriting documents slot
  takes the records slot's locality). A new descriptor hook, `host_local_fix(settings,
  locality)`, supplies the error text: `multi_node` rejects the slot saying to use
  `launch.mode: external` with a shared replica set; `multi_process` without
  `deployment.writer: true` says the other processes use `launch.mode: external` with
  `settings.url mongodb://127.0.0.1:<port>/?directConnection=true&replicaSet=<rs>`.
- **Tests.** `tests/unit/clients/test_mongodb_launcher.py` (fake mongod script and fake admin:
  resolution order, consent refusal, checksum refusal, only-mongod extraction for zip and tgz,
  official-host check, idempotent initiate, timeout, early exit, VC++ exit code, adoption, port
  conflicts, refcounted sharing with shutdown on the last release, SSPL notice once);
  `tests/unit/providers/test_mongodb_managed.py` (validation, locality, wiring);
  `tests/integration/test_mongodb_managed_live.py` (live).
- **Live evidence (Windows 11, 2026-10-23).** Consented download of 9.0.4 through the launcher:
  989 MB archive verified and reduced to `mongod.exe` (156.6 MB), first start PRIMARY after
  29.1 s including the download, 1.4 s from the cache. Against the managed server: records
  conformance 20/20, documents conformance 19/19, `test_mongodb_records.py` 5/5, the RM-112
  Engine e2e 9/9; an Engine with managed records and inheriting documents started in 1.9 s,
  wrote, searched, forgot, stopped (port released) and restarted on the same dbpath.
