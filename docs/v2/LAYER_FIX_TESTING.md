# Testing the memory-layer fixes, one bug at a time

This guide covers PR #5, branch `layer-contract`, worktree `C:\Users\hembad\Documents\GitHub\memspine-LAYER-FIX`. Each bug in `MEMORY_LAYER_BUG_LIST_2026-10-15.md` gets one command. The command prints the expected value, the value it got, and PASS or FAIL. Everything runs on the CPU with in-memory stores and the hash embedder: no GPU, no Ollama and no network. Only invented names are used.

## 0. Setup (once per PowerShell window)

```powershell
cd C:\Users\hembad\Documents\GitHub\memspine-LAYER-FIX
$py = "C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe"
$env:PYTHONPATH = "src;evals"
git log --oneline -3          # you should be on layer-contract
& $py docs/v2/layer_fix_checks.py --list
```

## 1. The three tools

| Tool | What it answers | Command |
|---|---|---|
| `docs/v2/layer_fix_checks.py <ID>` | Is this one bug fixed? Runs a few small checks and prints each value | `& $py docs/v2/layer_fix_checks.py MF-01` |
| `docs/v2/probe_memory_layers_2026_10_15.py` | The original forensic probe: the same cells run on old and new code | `& $py docs/v2/probe_memory_layers_2026_10_15.py` |
| `tests/unit/contract/test_layer_contract.py` | The permanent regression tests (80 cases) | `& $py -m pytest tests/unit/contract/test_layer_contract.py -q -W ignore` |

Running everything at once: `& $py docs/v2/layer_fix_checks.py all` ends with `68/68 checks passed` (dry run on 2026-10-15).

## 2. See the bug before you see the fix (optional, recommended)

This makes a second, read-only checkout of the old code (main, before the fixes) and runs the same probe on both:

```powershell
git worktree add ..\memspine-main-cmp origin/main      # creates a folder; deletes nothing
cd ..\memspine-main-cmp;  & $py docs\v2\probe_memory_layers_2026_10_15.py > $env:TEMP\probe_before.json
cd ..\memspine-LAYER-FIX; & $py docs\v2\probe_memory_layers_2026_10_15.py > $env:TEMP\probe_after.json
code --diff $env:TEMP\probe_before.json $env:TEMP\probe_after.json
```

What should change between the two files:
- `session[*].stored` and `replayed` go from `L1`/`L0` to `L2`.
- `episode_leg_reason` goes from `no_records:L2` to `null`.
- `mapping[*].stamped`: consolidation becomes L2, and entity_summary and reorganize become L3.
- `fact_classification` becomes all `false`.
- `graph.l1_only_reason` goes from `no_records:L3` to `null`, because the graph leg now answers from edges backed by L1 facts.

The old probe script is not in main, so copy it in if it is missing. When you no longer need the comparison checkout, run `git worktree remove ..\memspine-main-cmp`. It contains no junctions (no `evals\runs`); check with `Get-ChildItem ..\memspine-main-cmp -Recurse -Attributes ReparsePoint` first anyway.

## 3. Bug by bug

Each entry has five parts: what was wrong, what changed (with code locations), the command, what you should see, and how to break the fix on purpose to prove the check is real. To break a fix, edit the line, run the check (it should FAIL), then put the line back with `git checkout -- <file>`.

### Write door: LF-A

**MF-01: session-close records landed on L1 or L0 instead of L2**
- Before: `_deposit_session_record` passed `layer="episode"` or `"summary"`. Those are not layer names, so they were silently ignored.
- Fix:
  - `src/memspine/derive/pipeline/integrate.py` (`_deposit_session_record`) now passes `layer="L2"`.
  - `src/memspine/store/layers.py` (`_CHANNEL_LAYER`) gains `session_close → L2`.
- Run: `& $py docs/v2/layer_fix_checks.py MF-01`
- Expect: 8 PASS lines (episode and summary × lineage off and on × stored and after `rebuild()`).
- Break it: delete the `"session_close": "L2"` line in `layers.py`, then remove `layer="L2"` in `integrate.py`. The check should FAIL.

**MF-02: offline summaries were stamped L0**
- Before: `offline_door.stamp_summary` called `stamp_derived(record, "summary", site)`. `"summary"` was read as a memory type, which mapped to L0.
- Fix: `src/memspine/derive/write.py:stamp_derived`. The layer now comes from `record.memory_type` plus `record.source.channel`, and the second argument is only the route name.
- Run: `... MF-02`
- Expect: consolidation→L2, entity_summary→L3, reorganize→L3, community_summary→L3, multires→L2, observation→L4, standing→L5.

**MF-03: a bad layer value fell back silently**
- Fix: `derive/write.py:_resolve_layer` raises `ValueError("unknown layer 'L9' ...")`.
- Run: `... MF-03`
- Expect: `L9`, `episode`, `summary` and `""` each raise; `L2` is accepted.

**MF-04: with `write.lineage_table` off, an explicit L3/L4/L5 was dropped to L1**
- Before: the lineage-off early return in `_write_derived` came before the layer stamp.
- Fix: `derive/write.py:_write_derived` stamps the layer first.
- Run: `... MF-04`
- Expect: observation→L4, standing→L5, community_summary→L3, all with lineage off.
- Break it: delete the line `record = record.model_copy(update={"layer": _resolve_layer(record, layer)})`. The layers fall back to L1.

**NEW-02: the derivation fingerprint changed whenever a layer label changed**
- Fix: `stamp_derived` hashes only `{site, channel}`.
- Run: `... NEW-02`
- Expect: `True` (same fingerprint for L2 and L3).
- Note: records written after this change carry a different `fingerprint.derivation` than before. Stored values are not rewritten.

**NEW-03: the `memory_type` parameter actually carried a route name**
- Fix: the parameter is renamed to `route` in `stamp_derived` and `write_derived`. Positional callers are unchanged.
- Run: `... NEW-03`

### Persistence: LF-E

**MF-05: the layer was written to the event log only when it differed from what `derive_layer` would compute**
- Before: a replay re-derived the layer, so changing the mapping silently relabelled history.
- Fix:
  - `store/records.py`: the dump always writes `layer`.
  - `store/migrate.py:_upcast_producer_layer` (events v2→v3): only the separate `migrate_sqlite` fresh-copy migration path moves a record from a producer channel that is stuck on L0/L1 to its proper layer. An ordinary `Engine.rebuild()` replays the stored event as-is and preserves an explicit wrong layer.
  - Each correction emits a `migrate.layer_fix` tap event (`was`, `now`).
- Run: `... MF-05`
- Expect: `'layer' present` is True, then the upcast table. session_close L0→L2, consolidation L1→L2, entity_summary None→L3, observation→L4, standing→L5. A plain turn or chat record gives `None` (unchanged).
- Real store, using `migrate_sqlite` on a **fresh target path**. First stop writers and make a filesystem backup of the source DB; keep that original as the rollback copy. The command reads the source and refuses an existing target. Do not point the target at the source or an existing file:
  ```powershell
  $source = (Resolve-Path C:\path\old.db).Path
  $target = Join-Path $env:TEMP 'memspine-layer-migrated.db'
  if (Test-Path $target) { throw "Target already exists; choose a new path: $target" }
  Get-FileHash $source -Algorithm SHA256   # record before value
  & $py -m memspine.cli migrate $source $target
  if ($LASTEXITCODE -ne 0) { throw 'Migration failed; keep using the source DB' }
  Get-FileHash $source -Algorithm SHA256   # must match before value
  ```
  Review the YAML report (`events_read`, `events_upcast`, `projected`, `not_rebuilt`), then compare producer rows in the untouched source and migrated target:
  ```powershell
  $q = "select json_extract(source,'$.channel') ch, layer, count(*) from memory_records group by ch, layer order by ch"
  & $py -c "import sqlite3,sys; [print(r) for r in sqlite3.connect(sys.argv[1]).execute(sys.argv[2])]" $source $q
  & $py -c "import sqlite3,sys; [print(r) for r in sqlite3.connect(sys.argv[1]).execute(sys.argv[2])]" $target $q
  ```
  The `session_close`, `consolidation` and `entity_summary` rows that were on L0/L1 should now be on L2/L3. Turn and chat rows should not move. Inspect the migrated store before directing the application to it; retain the original source for rollback. Logged derived records are restored; unlogged derivations are not regenerated by migration. Review the report's separate limitations for vector/lexical indexes, graph and caches, then verify retrieval after starting an engine on the target.

- Ordinary `Engine.rebuild()` is not this repair: it replays the explicit layer saved in each event. SQL migration `0011_layer_backfill.py` also does not repair an explicit L0/L1 claim; it uses `COALESCE` and fills only NULL envelope fields. For the historical explicit producer-layer bug, the supported correction is migration by replay into a fresh SQLite store.
- **Watch out:** every new WRITE event now includes `layer`. A golden fixture that compares raw event bytes will show a diff. That diff is expected; regenerate the fixture rather than reverting the change.

**MF-08: Postgres rows from before the record envelope have a NULL layer**
- Fix: new migration `src/memspine/services/storage/postgres/alembic/versions/pg0011_layer_backfill.py` (rev `pg_lf_e_layer_backfill`, follows `pg_w202_stamping`). It fills NULL layers with `classify_stored_layer`.
- Run: `... MF-08` checks the revision chain only.
- Real test, on a **scratch** Postgres database only:
  ```powershell
  # $pg = a scratch database holding some pre-envelope rows (never a shared one)
  & $py -c "import sys; from memspine.services.storage.postgres.migrations import upgrade_to_head; upgrade_to_head(sys.argv[1])" $pg
  # then in psql:  SELECT layer, count(*) FROM memory_records GROUP BY layer;   -- no NULL row left
  ```
  Downgrading only steps back the revision; it does not undo the backfill.

**MF-09: `layer_counts` counted NULL-layer rows as L0, while record hydration derived them**
- Fix: `services/storage/sql_base.py:layer_counts` and `_envelope_fields` both use `store/layer_classify.py:classify_stored_layer`.
- Run: `... MF-09`, then the end-to-end test:
  ```powershell
  & $py -m pytest tests/unit/contract/test_layer_contract.py -q -W ignore -k layer_counts
  ```

### Fact eligibility: LF-C

**MF-10: two "is this a fact?" functions disagreed on derived views**
- Fix: one policy, `src/memspine/derive/fact_policy.py:is_evidence_fact(record, purpose)`, with one channel list, `DERIVED_VIEW_CHANNELS`. `is_fact` and `is_input_fact` are thin wrappers over it.
- Run: `... MF-10`
- Expect: `(False, False)` for observation, standing, session_close, entity_summary, community_summary, multires, consolidation and reorganize; `(True, True)` for a real keyed fact.
- To change which channels count as facts, edit only `DERIVED_VIEW_CHANNELS` / `NON_FACT_TAGS` in `fact_policy.py`.

**NEW-01: near-duplicate merge could merge or archive a summary against a restated fact**
- Fix: `histories.py` uses purpose `HISTORIES_MERGE`, which requires an entity key. Integration, reconcile, the edge compiler and standing each pass their own purpose (see the purpose table at the top of `fact_policy.py`).
- Run: `... NEW-01`
- Expect: unkeyed → False, entity-keyed → True, session_close view → False.

### Read legs: LF-B, LF-D

**MF-07: the graph leg demanded L3 records, but its edges are backed by L1 facts**
- Fix: `src/memspine/read/legs/graph.py:OneHopConvergent.run`. The leg is ready when at least one compiled edge has a live supporting record. Empty reasons are now `no_edges`, then `no_seeds`, then `no_convergence`.
- Run: `... MF-07`
- Expect: 1 hit in a namespace that holds only L1 records; `no_edges` when the edge's record is gone.

**MF-14: the registry only knew "populated"**
- Fix: `src/memspine/store/layer_registry.py` now reports four states: configured, available (configured, or has records, or has artefacts such as edges), populated, and selected (chosen by the router). `read/pipeline/legs.py` records L3 edges as artefacts and the selected layers.
- Run: `... MF-14`
- In a real read, `registry.report()` returns all four states per layer (L0..L5).

**MF-11: the episode leg treated any L2 record as an episode**
- Fix: `src/memspine/read/legs/episode.py`.
  - `EpisodeLeg(kinds=...)` takes kinds from `EPISODE_KINDS`. The default is episode, session_summary and episode_fragment. Consolidation and anticipation are opt-in.
  - New empty reason: `no_records:episode`.
- Run: `... MF-11`

**MF-12: one fixed vector top-k (max(4×fetch_k, 50)) across all layers starved sparse L2**
- Fix: `EpisodeLeg._vector_ranking` doubles k (50→100→200→400…, cap 1024) until it has `fetch_k` episode hits or the store runs out.
- Run: `... MF-12`
- Expect: asked `[50, 100, 200, 400]`. In the test, the episodes sit behind 300 other hits.
- To tune: `_VECTOR_OVERSAMPLE`, `_VECTOR_FLOOR` and `_VECTOR_WIDEN_CAP` at the top of `episode.py`.

**MF-06: an episode's lift walked the stub it extends and pulled in turns it does not cite**
- Fix: `src/memspine/read/pipeline/lift.py:_narrow_episode_lineage`. When an episode has `support` edges, only those are walked.
- Run: `... MF-06`
- Expect: `['t3']`. Without the fix, `t1` (the stub's turn) also appears.

### Profile card: LF-F

**MF-13: the profile card was written but nothing read it**
- Fix:
  - New header section `src/memspine/compose/pipeline/sections.py:_profile_card_section`, wired in `pack.py:_read_headers`.
  - New config keys: `read.profile_card_header` (off), `read.profile_card_header_budget` (200 tokens), `read.profile_card_max_age_days` (365; 0 means no bound).
  - The config loader now refuses the flag without its producer chain: `read.profile_card_header` needs `offline.profile_card_v2`, which needs `offline.session_close_v2`.
- Run: `... MF-13`
- Expect: the default is off; the flag alone raises `ConfigError`; the full chain loads cleanly.
- End to end, in the step-mode walkthrough (`INGESTION_WALKTHROUGH.md`):
  1. Use an arm with `{"offline": {"session_close_v2": true, "profile_card_v2": true}, "read": {"profile_card_header": true}}`.
  2. Run `session`, then `sleep`, then `ask`.
  3. The composed context should start with a profile-card block of dated lines that cite source record ids.
  4. Forget a source turn; its line should disappear.

### Coverage and eval hygiene: LF-G

**MF-15: no test asserted the layer after session close**
- Run: `& $py -m pytest tests/unit/contract/test_layer_contract.py -v -W ignore`
- Expect: 80 passed. This covers producer → storage → rebuild → legs, with lineage on and off.

**MF-16: a result row could not say which layers an arm actually ran**
- Run: `& $py -m memspine_evals.memory_type_matrix evals/arms/v2-PICK3-j.json` (add `--json` for data).
- Expect, for J: only L0/L1 effective, the vector, lexical and perspective legs on, and every producer off. That confirms the adopted default never touches the layers fixed here.
- Try a graph arm (`evals/arms/scr-gc3-graphleg.json`, if present) to see L3 come on, with any missing prerequisite listed.

## 4. Fixing something you find

1. Reproduce it with the single check: `... <ID>`. Read the expected and got values.
2. Open the file and function named in that bug's "Fix" line.
3. Change the code, then re-run the same check and then its pytest twin: `-k <keyword>`, for example `-k session_close`, `-k graph_leg`, `-k episode_leg`, `-k invalid_explicit_layer`, `-k input_fact` or `-k layer_counts`.
4. When a whole group is green, run `& $py docs/v2/layer_fix_checks.py all` and the full contract file.
5. Commit on `layer-contract` and push. PR #5 updates itself.

## 5. Known gaps (not bugs in this PR)

- The vector store has no layer column, so the episode leg widens its search instead of filtering by layer. A real push-down needs a schema change in the vector store.
- There are two channel→layer tables: `store/layers.py:_CHANNEL_LAYER` sets layers at write time, and `store/layer_classify.py:PRODUCER_LAYER` drives the replay correction. Keep them in step when you add a producer channel. The upcaster table is frozen on purpose so that replay stays deterministic.
- The adopted default `v2-PICK3-j` runs none of the producers fixed here (see MF-16). Only graph, episode or offline arms change behaviour.
