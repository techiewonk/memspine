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
`consent.v2`, `graph.edge_compiler_v2`, `offline.session_close_v2`, `read.legs_v2` (plus `write.participants=session`,
see finding F1). The workload: three sessions for two owners (`acme/ilse`, `acme/bram`) and a two-record shared
namespace (`acme/household`), consent choices, session close (call 2 stub), the sleep cycle.

| Step | Test | What it proves |
|---|---|---|
| 1 | `test_step1_workload_written` | all flags are on in the effective config; turns and shared records written |
| 2 | `test_step2_sleep_and_read` | sleep runs; answers cite the right turn; no cross-owner leak; a multi-party read without a viewer is refused (`viewer_required`); a non-participant sees nothing |
| 3 | `test_step3_restart_state` | stop and reopen the same store: record ids, lineage edges, outbox states, consent registry (and its behaviour), erasure state identical; audit chain verifies |
| 4 | `test_step4_hard_erase` | hard-forget one turn: dependants hidden, purge `done`, no read serves it (also after a cache hit and after a restart), lineage edges gone, manifest has no content |
| 5 | `test_step5_replay_migrate` | `migrate_sqlite` into a fresh store; projected records equal the live store field for field; the erased turn does not come back; an engine on the replayed store rebuilds the erasure state |

The JSON report has per-step counts and timings, the flag list and the findings below. `MigrationReport.not_rebuilt`
(vector and lexical index, graph, model-derived records, caches) is recorded; in this workload every record was a
logged write, so no live record needed to be excluded from the comparison.

### Findings (not failures; owner decisions)

- F1: with `write.participants=off` (default) a fact mined from a private turn in a multi-party namespace has no
  visibility tag and is served to a non-participant. `isolation.viewer_required` alone does not cover derived records.
- F2: `privacy.erasure_v2` hides dependants for re-derivation but keeps their rows and WRITE events, which still hold the
  erased text on disk. No read serves them; replay reproduces exactly the same set.
- F3: a kept dependant still lists the erased id in `source.parents` (the lineage-table edge is removed). An id, no text.
- F4: `Engine.verify_forget()["clean"]` is false under `erasure_v2` while a dependant row is retained.
- F5 (fixed): `Memory.retrieve()` on a handle with a `purpose` raised `TypeError` (`src/memspine/api/handle.py`).
