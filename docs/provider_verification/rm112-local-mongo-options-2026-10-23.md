# RM-112 spike: local MongoDB-compatible stores without Docker (2026-10-23)

Evaluation spike, no product code changed. Question: which library-based (pip-installable or
single-binary, no Docker, no admin) MongoDB-compatible store can back the `mongodb` records
provider (`MongoRecordsStore`) and/or a GridFS documents provider for local use?

Environment: Windows 11 Enterprise 10.0.26340, Python 3.13 worktree env
(`uv sync --locked --extra dev --extra mongodb`, pymongo 4.19.0), branch `restructure` at
`4197214b`. Candidates were installed into that env with `uv pip install` only (pyproject and
uv.lock unchanged). All throwaway probe scripts, data directories and downloads lived outside
the repo and were deleted afterwards.

## What the records slot needs from the server

From `docs/RECORDS_STORE_CONTRACT.md` and `services/storage/mongodb.py`: the Mongo wire
protocol for pymongo's native asyncio client (`AsyncMongoClient`), a `hello` reply with
`setName` (the connector refuses a standalone server), multi-document transactions
(append-plus-outbox in one transaction), `findOneAndUpdate` with `$inc` for the sequence
counter, unique and compound indexes, a `$jsonSchema` collection validator, aggregation-pipeline
updates that read `$$NOW` (server-clock leases), `$max`, and `dropDatabase`. A GridFS documents
adapter needs `createIndexes` on `<bucket>.files`/`.chunks`, chunked inserts, `find` by metadata,
and deletes. It does not need transactions.

## Comparison

| Candidate | Version checked | What it is | Install effort (Windows) | Admin? | License | Maintenance | Records conformance (15 mongodb cases) | GridFS round trip | First failing capability |
|---|---|---|---|---|---|---|---|---|---|
| **Official MongoDB Community, portable zip, one-member replica set** (baseline) | 9.0.4 (also 8.0.4) | real `mongod` | download zip (989 MB for 9.0.4), extract only `bin/mongod.exe` (157 MB), launch with `--replSet` and `replSetInitiate` | no (the VC++ runtime was already present here; the zip ships `vc_redist.x64.exe` for clean machines, and installing that needs admin) | SSPL v1 (server) | active (9.0.4 is the current production release) | **15/15 pass** (9.8 s), again after a restart; 8.0.4 binary also 15/15 | **pass** (sync 3 MiB, async 1 MiB via `AsyncGridFSBucket`) | none |
| **pymongo_inmemory** | 0.5.0 (2025-03-13) | pip launcher that downloads an official `mongod` | `pip install pymongo-inmemory`; first `Mongod()` downloads the 776 MB 8.0.4 zip and extracts all of it (2.08 GB, PDBs included) | no | MIT (the downloaded server is SSPL) | low activity: last release 2025-03, Windows URL table stops at 8.0.4 | **0/15 as shipped**: always standalone, so the memspine connector raises `StandaloneServerError`; the same downloaded binary started by hand with `--replSet` passes **15/15** | **pass** on its standalone server | `hello` has no `setName` (no `--replSet` option); transactions fail with code 20 |
| **PoloDB standalone server** | 5.3.0 (2026-07-26) | Rust embedded doc DB plus a partial wire-protocol server | single 10.7 MB `polodb-win32-x64.exe` from GitHub releases (sha256 matches the release digest) | no | Apache-2.0 | active | **0/15**: pymongo cannot complete the handshake | **fail** (same) | handshake: `ProtocolError('Unsupported OP_MSG flags: 0xa800')`. The server source has only 11 command handlers (find, getMore, killCursors, aggregate, insert, update, delete, hello, commit/abortTransaction, ping); no `createIndexes`, `findAndModify`, `create`/`collMod`, `count`, `drop`; its `hello` reply has no `setName` |
| **polodb-python** | 0.2.1 (2026-08-09) | experimental in-process bindings | pip | no | Apache-2.0 | experimental | not applicable: no wire protocol, so `AsyncMongoClient` cannot use it | not applicable | needs a separate adapter (not tested further) |
| **Mongita** | 1.2.0 (2023-04-03) | embedded PyMongo-subset, own client classes | `pip install mongita` | no | BSD-3-Clause | stale (last release 2023, last push 2025-07) | not runnable: no wire protocol | **fail**: `gridfs` refuses a `mongita.Database` | `start_session` not implemented; `find_one_and_update`, `aggregate`, `create_collection` not implemented; only `$set/$inc/$push`; no `unique` indexes; no pipeline updates |
| **MontyDB** | 2.5.6 (2025-11-01) | embedded PyMongo-subset (sqlite/flatfile/lmdb/memory storage) | `pip install montydb` | no | BSD-3-Clause | maintained, small | not runnable: no wire protocol | **fail**: no GridFS module | no sessions/transactions; `aggregate` not implemented; no pipeline updates; a `$jsonSchema` validator and a unique index are **accepted but not enforced** (invalid and duplicate inserts succeed) |
| **FerretDB** | 2.7.0 (2025-11-10) | wire-protocol proxy over Postgres plus the DocumentDB extension | release assets are Linux-only (amd64/arm64 binaries, deb, rpm); needs a Postgres with the DocumentDB extension, which also has no Windows build | n/a | Apache-2.0 (DocumentDB extension: MIT) | active | not tested: no no-Docker Windows path | not tested | no Windows binary |
| mongomock / mongomock-motor, mtools `mlaunch`, testcontainers | 4.3.0 / 0.0.36 / 1.7.2 / 4.15.0 | test mock / launcher that needs `mongod` on PATH / Docker | - | - | - | - | not tested: a mock has no wire protocol and no real transactions; `mlaunch` still needs the official binary and is stale (2022); testcontainers needs Docker | - | out of scope |

## Per-candidate evidence

### 1. Official MongoDB Community server as a portable zip (baseline)

- URL and sha256 from `https://downloads.mongodb.org/full.json`:
  `https://fastdl.mongodb.org/windows/mongodb-windows-x86_64-9.0.4.zip`,
  sha256 `1e6234c8...9256d43` (verified). The download took 26 s here.
- Size: the zip is 988.8 MB, mostly debug symbols (`mongod.pdb` 1.40 GB and `mongos.pdb`
  912 MB uncompressed). Only `bin/mongod.exe` is needed: 156.6 MB unpacked, 50.4 MB
  compressed, extracted in 0.7 s.
- Commands (PowerShell, no admin, no service install):

  ```powershell
  # extract just mongod.exe from the zip (System.IO.Compression), then:
  mongod.exe --replSet rs0 --bind_ip 127.0.0.1 --port 57017 --dbpath <dir> --logpath <dir>\mongod.log
  # once, from any client:
  db.adminCommand({replSetInitiate: {_id: "rs0", members: [{_id: 0, host: "127.0.0.1:57017"}]}})
  # connect with  mongodb://127.0.0.1:57017/?directConnection=true
  ```

- First start on an empty dbpath: ping after 0.55 s, PRIMARY after 0.74 s. Restart on the
  existing dbpath: PRIMARY after 1.09 s (`replSetInitiate` answers `AlreadyInitialized`, which
  is harmless). Working set about 150 MB. The dbpath reached 214 MB after the suite, mostly the
  oplog and journal; `--oplogSize` can cap it.
- Capability probe: `hello` reports `setName=rs0` with maxWireVersion 29. Commit and abort
  transactions behave correctly. The `$jsonSchema` validator rejects an invalid document, and a
  pipeline update reading `$$NOW` works.
- `MEMSPINE_TEST_MONGO_URL=mongodb://127.0.0.1:57017/?directConnection=true pytest
  tests/unit/services/storage/test_records_conformance.py -k mongodb`: **15 passed** in 9.8 s,
  and 15 passed again after a shutdown and restart.
- GridFS: a sync `GridFSBucket` round trip (3 MiB + 17 B, sha256 equal, chunks removed on
  delete) and an async `AsyncGridFSBucket` round trip (1 MiB, lookup by `metadata.digest`,
  delete) both pass.
- Documents conformance suite: the `mongodb` case is still skipped with
  `documents provider 'mongodb' is planned (RM-112), not registered yet` (19 passed,
  57 skipped overall), so GridFS was tested directly with pymongo's `gridfs`.
- Caveats: on a clean machine `mongod.exe` may need the VC++ runtime; the bundled
  `vc_redist.x64.exe` installs it but needs admin. The binary is SSPL, so memspine should
  download it on demand from MongoDB's servers and not vendor it.

### 2. pymongo_inmemory 0.5.0

- `uv pip install pymongo-inmemory==0.5.0`. The package is configured through `PYMONGOIM__*`
  environment variables, `setup.cfg` or `pymongo_inmemory.ini` (`mongo_version`, `mongod_port`,
  `mongod_data_folder`, `download_folder`, `extract_folder`, `download_url`, `storage_engine`,
  `use_local_mongod`).
- The default on Windows resolves to 8.0.4 (`.../mongodb-windows-x86_64-8.0.4.zip`, 776 MB).
  It extracts the whole archive, 2.08 GB with the PDBs. The default cache is inside
  `site-packages/.cache`.
- **Windows path-length failure:** with the extract folder under a long directory, `Popen`
  raised `FileNotFoundError: [WinError 206] The filename or extension is too long`. The
  executable path is `<extract>\<64-hex url hash>\mongodb-win32-x86_64-windows-8.0.4\bin\mongod`,
  which ran past MAX_PATH. It worked once `PYMONGOIM__EXTRACT_FOLDER` pointed at a short path.
  Extracting from cache took 14 s and the start took 3.6 s.
- **No replica-set option.** `Mongod.start()` builds the command line from `--dbpath`,
  `--logpath`, `--port`, `--bind_ip` and `--storageEngine` only. There is no `--replSet` and no
  hook for extra arguments. Result: `hello.setName = None`, transactions fail with
  `Transaction numbers are only allowed on a replica set member or mongos` (code 20), and
  `MongoConnector.open()` raises `StandaloneServerError`. **Records: 0/15 as shipped.**
- The same downloaded `mongod.exe` started with `--replSet` plus `replSetInitiate` passed the
  records suite **15/15**. The binary is fine; the launcher is the limitation.
- GridFS on its standalone server: sync and async round trips both **pass**. `$jsonSchema` and
  pipeline updates also work there.
- Other fragility: it picks the first free port in 27017-28000 when none is set, so it can
  collide with a developer's real 27017. The health check is a busy loop (`while not
  is_healthy: pass`). The data directory is a temporary folder that is deleted on `stop()`
  unless `mongod_data_folder` is set. Version support is limited to its hard-coded URL table,
  which stops at 8.0.4 for Windows; newer versions need `download_url`.

### 3. PoloDB 5.3.0 standalone server

- `polodb-win32-x64.exe` (10.7 MB) from the v5.3.0 GitHub release. The sha256
  `2756b94c...aabbf` matches the release digest. Started with
  `polodb serve --path <dir> --host 127.0.0.1 --port 57018`. There is no pip package for the
  server; the upstream install route is `cargo install polodb`.
- pymongo 4.19 never got past server selection: `ServerSelectionTimeoutError ...
  ProtocolError('Unsupported OP_MSG flags: 0xa800')`. Every probe (hello, transactions,
  `$jsonSchema`, pipeline update, GridFS sync and async) failed the same way. **Records 0/15,
  GridFS fail.**
- The upstream README says the server "implements the subset of the MongoDB wire protocol used
  by its supported commands. It is not a drop-in replacement". The source
  (`src/polodb/handlers/`) registers 11 handlers and none of `createIndexes`, `findAndModify`,
  `create`/`collMod` (validators), `count`/`countDocuments`, `drop`, `dropDatabase` or
  `listIndexes`. Its `hello` reply carries no `setName`. So even with the handshake fixed, the
  memspine connector would refuse it, and `MongoRecordsStore.start()` and GridFS would fail on
  `createIndexes`.
- `polodb-python` 0.2.1 (experimental in-process bindings) has no wire protocol and would need
  a separate adapter. Not pursued.

### 4. Mongita 1.2.0 and MontyDB 2.5.6 (embedded PyMongo subsets)

Neither speaks the wire protocol, so `AsyncMongoClient`, the connector and the conformance suite
cannot point at them. Both also offer sync APIs only. The probe called the operations
`MongoRecordsStore` uses through each library's own client:

| Operation | Mongita (disk) | MontyDB (sqlite storage) |
|---|---|---|
| sessions / transactions | `start_session` not implemented | none |
| `find_one_and_update($inc, upsert)` | not implemented | works |
| pipeline update with `$$NOW` | rejected (dict only) | rejected (dict only) |
| `$max` | unsupported (only `$set/$inc/$push`) | works |
| unique / compound index | `unique` rejected | accepted, **not enforced** (a duplicate insert succeeded) |
| `$jsonSchema` validator | `create_collection` not implemented | accepted, **not enforced** (an invalid insert succeeded) |
| `aggregate` | not implemented | not implemented |
| GridFS | `gridfs` refuses a `mongita.Database` | no GridFS module |

Neither can serve records (no atomic append-plus-outbox, no server clock) or documents.

### 5. FerretDB 2.7.0

The v2.7.0 release assets are Linux-only (`ferretdb-amd64-linux`, `-arm64-linux`, `.deb`,
`.rpm`). FerretDB 2.x also needs PostgreSQL with the DocumentDB extension, which has no Windows
build, so there is no no-Docker Windows path. Not tested empirically. On Linux it would be a
two-process setup (Postgres + DocumentDB, then FerretDB), and its transaction support would have
to be verified separately before it could stand in for records.

## Recommendation

1. **Records: only a real `mongod` started as a one-member replica set qualifies.** The
   official portable binary passes the records conformance suite 15/15 (9.0.4 and 8.0.4)
   with no admin rights and no Docker, and it reaches PRIMARY in about 1 s. Every
   library-based alternative fails on transactions first: Mongita and MontyDB have no sessions
   at all, PoloDB cannot complete the pymongo handshake and lacks `createIndexes` and
   `findAndModify`, and pymongo_inmemory runs a standalone server. Keep the connector's
   replica-set requirement; none of these results argues for relaxing it.
2. **Documents (GridFS): any real `mongod` works, standalone included.** The pymongo_inmemory
   standalone server and the official binary both pass sync and async GridFS round trips.
   PoloDB, Mongita and MontyDB do not. A documents-only deployment could therefore use
   `MongoSpec(require_transactions=False)` (already anticipated in the connector), though
   sharing the records replica set is simpler.
3. **Do not depend on pymongo_inmemory.** Its value is downloading and caching an official
   binary, but it cannot start a replica set, extracts 2 GB per version, breaks on long
   Windows paths, can grab port 27017, and its version table stops at 8.0.4. The ~150 lines
   memspine would need are smaller than working around it.
4. **What memspine would need to add (a proposal, no code here):** an opt-in managed local
   launcher for the `mongodb` provider, for example `storage.records = {provider: mongodb,
   settings: {launch: {mode: managed, version: "9.0.4", port: 57017, data_dir: ...}}}`, with
   `deployment.data_dir` as the default base. It would:
   - resolve the version from `downloads.mongodb.org/full.json`, download the Windows zip,
     macOS tgz or Linux tgz over HTTPS, **verify the published sha256**, and extract only
     `bin/mongod[.exe]` into a short cache path (for example `<data_dir>/mongod/<version>/`),
     which avoids MAX_PATH and the 1.3-2 GB of PDBs;
   - spawn `mongod --replSet <name> --bind_ip 127.0.0.1 --port <p> --dbpath <dir> --logpath ...
     [--oplogSize N]`, run `replSetInitiate` idempotently (ignore `AlreadyInitialized`), and
     wait for `isWritablePrimary` before the connector opens
     `mongodb://127.0.0.1:<p>/?directConnection=true`;
   - own the process lifecycle under `ProviderLifecycleManager` (graceful `shutdown`, then
     terminate), never pick a port implicitly, and refuse to reuse a dbpath that another live
     `mongod` holds (`mongod.lock`);
   - report `locality: host` instead of `cluster` for the managed mode, never vendor the SSPL
     binary, and never download without explicit opt-in;
   - optionally accept `launch.binary: <path>` to use a binary the user already has, which
     also covers air-gapped machines and the VC++-runtime caveat.

   That would need its own decision row and ADR (a new optional behaviour of the `[mongodb]`
   extra, with no new Python dependency because it uses only the stdlib and pymongo). Until
   then, the documented local recipe is the four-step baseline in section 1, and the
   conformance suites run against it through `MEMSPINE_TEST_MONGO_URL` and
   `MEMSPINE_TEST_MONGODB_URL`.
