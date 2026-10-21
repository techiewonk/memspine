# Debugging one persisted memory lifecycle

`scripts/trace_memory_lifecycle.py` follows one invented source message through the canonical
`Engine.ingest_messages()` path, window extraction via `Engine.flush_windows()`, the offline
`Engine.sleep()` stages, and retrieval from the same Engine. SQLite, LanceDB, and Tantivy use
file-backed placement; hash embeddings avoid model downloads. Window extraction uses the existing
deterministic offline stub from `docs/v2/flows/f01_ingest.py`. The trace does not call external
models or services.

Run from the repository root with the project environment:

```powershell
$env:PYTHONPATH = 'src'
python scripts/trace_memory_lifecycle.py
```

The default run uses a temporary SQLite file and writes the portable evidence report to
`docs/provider_verification/memory-lifecycle-trace.json`. Supply `--database PATH` to retain the
SQLite database and its adjacent provider projections for inspection; the path must not already
exist. Supply `--output PATH` to write the report elsewhere. The report contains fixed synthetic
example text, message and record IDs, session/conversation identity, the source sender fields and
preserved source timestamp, provider identity and lifecycle outcomes, derived parent IDs, sleep
stage statuses, and search/vector/lexical result IDs. The wiretap has content capture disabled.
The offline transport is scoped to this invocation and restored on success or failure. The report
deliberately excludes provider settings, credentials, raw prompts, input fingerprints, and error
strings.

## Reading the evidence

- `source` describes the synthetic source event. `stages.ingest.raw_turn` gives the persisted
  L0 record and the canonical source message identity/time fields.
- `stages.derive.derived_records` gives derived L1 records. `lineage_valid` requires each such
  record's parent set to be the ingested message record ID; `stats` reports the actual window
  extraction counts.
- `provider_selection` is filtered from `Engine.provider_catalog()`. It records the logical
  service ID, configured and resolved provider names, and lifecycle/readiness state without native
  settings.
- `stages.offline.stages` preserves each stage's `ok`, `noop`, or `skipped` status and safe reason
  where present. A skipped optional stage is visible as such; the overall offline outcome only
  indicates that `sleep()` completed.
- `stages.retrieval` records IDs returned by public hybrid `Engine.search()` and its vector and
  lexical legs. This is a smoke trace of selected providers, not ranking-quality evidence.
- `provider_and_coverage_boundaries` records the graph provider that was actually resolved, whether
  its offline compiler ran, and disabled reranking or untested memory types. This one trace does not
  cover every memory type or provider pairing.

The focused test runs the same trace against a temporary file-backed database and checks raw source
identity, derived lineage, selected provider identities, offline stage reporting, and retrieval.
Run it with:

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -p no:deepeval -o addopts='' -q --tb=short tests/integration/test_memory_lifecycle_trace.py
```
