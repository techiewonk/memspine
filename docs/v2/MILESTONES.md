# MILESTONES: runnable phase-exit checks

Milestones are model-free end-to-end tests that live under `tests/e2e/` and carry the pytest marker `milestone`.
They use the hash embedder and stub LLM callables only (no GPU, no real model call), invented names only, and the
public API (`memspine.open()` and the scoped `Memory` handle).

## EX-1a: embedded workload survives restart, erasure and replay

Gate (PLAN.md, ER-05): restart, erasure and replay on the embedded profile. This is not a server release (EX-1 is).

Run:

```
PYTHONPATH=src;evals python -m pytest tests/e2e/test_ex1a_embedded.py -m milestone -o addopts="" -q
# add EX1A_REPORT=1 to write docs/v2/data/milestones/ex1a_<date>.json
```

About 25 s. Every foundation flag that has landed is on: `write.lineage_table`, `workers.outbox` (inline transport),
`isolation.scope_keys_v2`, `isolation.viewer_required`, `privacy.erasure_v2`, `retention.v2`, `audit.chain_v2`,
`consent.v2`, `graph.edge_compiler_v2`, `offline.session_close_v2`, `read.legs_v2`; `write.participants` stays at its default (off), see F1). The workload: three sessions for two owners (`acme/ilse`, `acme/bram`) and a two-record shared
namespace (`acme/household`), consent choices, session close (call 2 stub), the sleep cycle.

| Step | Test | What it proves |
|---|---|---|
| 1 | `test_step1_workload_written` | all flags are on in the effective config; turns and shared records written |
| 2 | `test_step2_sleep_and_read` | sleep runs; answers cite the right turn; no cross-owner leak; a multi-party read without a viewer is refused (`viewer_required`); a non-participant sees nothing |
| 3 | `test_step3_restart_state` | stop and reopen the same store: record ids, lineage edges, outbox states, consent registry (and its behaviour), erasure state identical; audit chain verifies |
| 4 | `test_step4_hard_erase` | (run with `privacy.erasure_redact_hidden` off and on) hard-forget one turn: dependants hidden, purge `done`, no read serves it (also after a cache hit and after a restart), lineage edges gone, manifest has no content |
| 5 | `test_step5_replay_migrate` | `migrate_sqlite` into a fresh store; projected records equal the live store field for field; the erased turn does not come back; an engine on the replayed store rebuilds the erasure state |

The JSON report has per-step counts and timings, the flag list and the findings below. `MigrationReport.not_rebuilt`
(vector and lexical index, graph, model-derived records, caches) is recorded; in this workload every record was a
logged write, so no live record needed to be excluded from the comparison.

### Findings (not failures; owner decisions)

- F1 (fixed, FX-EX1A): with `write.participants=off` a fact mined from a private turn in a multi-party namespace had no
  visibility tag and was served to a non-participant. Now, while `isolation.viewer_required` is on, a multi-party
  namespace (`isolation.multi_party_namespaces`, a `multi_party=True` handle, or detected) behaves as
  `write.participants: session`, and a derived record inherits the participants and the strictest visibility of its
  parents (`ingest/write.py:_govern_write`, `derive/write.py:_inherit_visibility`, `trust/visibility.py:restrict_to_parents`),
  so it is never more visible than its most restrictive parent. The workload no longer sets `write.participants`; with
  the isolation flags off nothing changes.
- F2 (fixed behind a flag, FX-EX1A): `privacy.erasure_v2` hides dependants for re-derivation but keeps their rows and WRITE
  events, which still hold the erased text on disk (no read serves them); re-derivation itself is still not implemented.
  `privacy.erasure_redact_hidden` (default off) hard-forgets the hidden dependants instead and marks their queue entry
  `dropped_no_rederive`. The milestone runs both ways; with the flag on a byte scan of the sqlite file finds no erased text.
- F3 (left as is): a kept dependant still lists the erased id in `source.parents` (the lineage-table edge is removed). An
  id, no text; the dependant stays supported by its other live parent.
- F4 (fixed, FX-EX1A): `Engine.verify_forget()["clean"]` is `privacy.erasure_v2`-aware: clean when the erased id is purged
  everywhere and each dependant is kept (another live parent) or hidden and queued. New fields
  `descendants_hidden_pending_rederive`, `descendants_kept`, `descendants_leaked` (and `erasure_v2`); only a leaked
  dependant blocks `clean`. The milestone asserts `clean`.
- F5 (fixed): `Memory.retrieve()` on a handle with a `purpose` raised `TypeError` (`src/memspine/api/handle.py`).

## EX-1: server profile (Phase 1 exit)

Gate (PLAN.md): "raw-only read non-inferior; golden; replay NO CHANGE". The record is
`docs/v2/data/milestones/ex1_<date>.json` (per-check pass / skip / deferred).

- **Raw-only read non-inferior**: needs GPU QA. Deferred to GPU (not run here).
- **Golden**: `tests/golden -m golden` (flag-off behaviour byte-stable).
- **Replay NO CHANGE**: F-16b replay byte-stability on embedded (`tests/unit/store/test_migrate.py`) plus the server
  matrix cell `test_rebuild_then_read[server-*]` (`Engine.rebuild`, then read). `migrate_sqlite` replays a SQLite source,
  so `test_erase_replay_read[server-*]` skips by design.

Start throwaway servers (loopback only, random passwords kept in your shell, never in a file):

```
PW=$(openssl rand -hex 12); NPW=$(openssl rand -hex 12)
docker run -d --name memspine-pg-test -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=$PW -e POSTGRES_DB=memspine_test pgvector/pgvector:pg16
docker run -d --name memspine-neo4j-test -p 127.0.0.1:57687:7687 -e NEO4J_AUTH=neo4j/$NPW neo4j:5
export MEMSPINE_TEST_PG_URL="postgresql://postgres:$PW@127.0.0.1:55432/memspine_test"
export MEMSPINE_TEST_POSTGRES_URL=$MEMSPINE_TEST_PG_URL
export MEMSPINE_TEST_NEO4J_URL="bolt://127.0.0.1:57687" MEMSPINE_TEST_NEO4J_USER=neo4j MEMSPINE_TEST_NEO4J_PASSWORD=$NPW
```

Needs the `psycopg` and `neo4j` drivers in the venv (`asyncpg` is not used). The Postgres tests wipe the database they
are pointed at: use a scratch one. On Windows the suite switches to the selector event loop when a Postgres URL is set
(psycopg async cannot run on the Proactor loop).

Run:

```
python -m pytest tests/unit/store/test_f16d_storage_profile.py tests/integration/test_storage_backends.py \
    tests/unit/wiring/test_f_16e_neo4j_outbox.py -o addopts="" -q          # contract, Alembic parity, live Neo4j
python -m pytest tests/e2e/test_f16f_matrix.py -m milestone -o addopts="" -q   # 14 server cells (12 run, 2 skip)
python -m pytest tests/e2e/test_ex1a_server.py -m milestone -o addopts="" -q   # EX-1a steps 1-4 on Postgres
```

`tests/e2e/test_ex1a_server.py` re-uses the EX-1a step functions with `storage.profile: server`; step 5
(`migrate_sqlite`) skips by design, and the on-disk byte scan of step 4 (`redact_on`) is vacuous on Postgres (the
`verify_forget` checks still run). Stop the servers with `docker rm -f memspine-pg-test memspine-neo4j-test`.

Bugs found on Postgres and fixed: the shared table metadata typed `simhash` as `Integer` (overflowed as int4 on
Postgres; `sqlite/schema.py` is now `BigInteger`, SQLite DDL unchanged); the test conftest scrubbed
`MEMSPINE_TEST_*` DSNs with the other `MEMSPINE_*` variables (so every live cell silently skipped); the lineage
contract test relied on SQLite continuing a transaction after an `IntegrityError` (now a SAVEPOINT).

## Final CPU gate, 2026-10-13 (all non-GPU work merged)

Run on `restructure` after W2-06c and owner-bound handles, serial, nothing else running:
`tests/unit` 6944 passed / 29 skipped / 0 failed · `evals/tests` 1548 passed / 14 skipped · golden 1 passed ·
`tests/e2e -m milestone` 32 passed / 30 skipped (server cells skip without `MEMSPINE_TEST_PG_URL`; they passed live in the EX-1 run) ·
import-linter 14 kept / 0 broken · single Alembic head (sqlite and Postgres trees).
Remaining work needs the GPU: the 71 "Needs measurement" screens (resume list in `docs/v2/SCREENS.md`), EX-2..EX-5, T5-07, R6-01, R6-02.
