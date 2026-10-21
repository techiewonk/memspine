# Provider implementation verification — 2026-10-17

## Six selected guard repairs — completed 2026-10-18

[Focused gate](guard-fixes-focused.log): **80 passed, 2 legitimate live Temporal
address skips**. All six M-GUARDS findings are repaired; required/broken imports
fail and temporal correctness assertions execute. Genuine absent optional SDKs
still skip. Installed OpenTelemetry exporter executed. The source review now has
**156 sites** (73 importorskip, 53 skip, 30 skipif), with no stale/missing review.
All 21 standalone evaluator Engine guards and 3 deferred records are unchanged.
[Current review](../TEST_SKIP_REVIEW.md), [mapping/history](guard-fixes-review-mapping.json),
[final evidence](guard-fixes-final-state.json). The prior PA-038 snapshot below
records the original 162-site review and is superseded for current counts.

## Skip review and work-package ownership — 2026-10-18

PA-038/090/091 cover the authorized source review and ownership plan, not a full
pytest sweep or closure of every skipped coverage path. The expanded inventory
has **162 source sites**: 77 `importorskip`, 54 `skip`, 31 `skipif`. All sites
have reviewed reasons, accountable owner roles, prerequisites and milestones.
Static declarations are not parameterized/executed skip counts.

- [Per-site review and open guard findings](../TEST_SKIP_REVIEW.md)
- [Reviewed source inventory](test-skip-inventory.json) and
  [dispositions/source pins](test-skip-dispositions.json)
- [Accountable roles and milestones](../PROVIDER_NEXT_MILESTONES.md)
- [Focused audit tests](test-skip-audit-focused.log): **9 passed**
- [Source audit](test-skip-audit.log): zero parse errors, incomplete or stale reviews
- [Final evidence manifest](pa038-090-091-final-state.json)

Regenerate and validate the current review without importing test modules:

```powershell
python scripts/audit_test_skips.py `
  --dispositions docs/provider_verification/test-skip-dispositions.json
```

Changed source hashes, missing per-site reviews and orphan review IDs fail the
review gate. The six test-guard findings remain open repair work; three evaluation
findings retain PA-107's deferred boundary. Legitimate optional Engine guards in
the standalone evaluation harness are preserved. Historical reports below retain
their original run scope and cannot supply missing individual skip outcomes.

## Named profiles and reasoning levels — 2026-10-18

The single GLiNER settings field from the earlier performance pass is superseded
by named profiles and explicit task bindings (ADR-075), with no alias. These
are local implementation checks; deployment acceptance remains separate.

| Evidence | Result |
|---|---|
| [Configuration](model-profiles-config-focused.log) | 78 passed |
| [GLiNER/windows](model-profiles-gliner-focused.log) | 130 passed, one intentional real-model inference skip, 16 existing LanceDB warnings |
| [LLM/lifecycle/privacy](model-profiles-llm-focused.log) | 148 passed; one example test also appears in configuration group |
| [Lint](model-profiles-ruff.log) / [format](model-profiles-format.log) | 21 selected Python files clean |
| [Typing](model-profiles-mypy.log) | 15 source files strict mypy clean |
| [References](model-profiles-reference-check.json) | Defaults, stage catalog and schema snapshot match |
| [Final state](model-profiles-final-state.json) | Dirty-tree source/artifact hashes and explicit chunk counts |

[Guide and example](../MULTI_MODEL_PROFILES.md). No full-suite run, complete-Engine
performance benchmark or paid/remote model call is implied by this evidence.

## Performance follow-up — 2026-10-18

| Evidence | Meaning |
|---|---|
| [Focused checks](performance-final-focused.log) | 192 passed, one intentional real-model ingestion skip; 17 existing LanceDB cleanup warnings |
| [RRF prefixes](bounded-topk.json) / [log](bounded-topk.log) | Final production kernel versus frozen full-sort reference; only two fixed-prefix graph seed callers changed |
| [MinHash](minhash-batch.json) / [log](minhash-batch.log) | Exact persisted signature and LSH fixture parity; bounded batch matrix comparison |
| [GLiNER ownership](gliner-ownership-kernel.json) | Three fake consumers, three independent loads versus one shared load; no model latency/RAM claim |
| [Actual GLiNER load](gliner-ownership-local-load.json) / [log](gliner-ownership-local-load.log) | One cached Base snapshot load/release, offline; no inference |
| [Embedding batches](embedding-batch-kernel.json) / [log](embedding-batch-kernel.log) | Actual local ONNX inference and vector parity; temporary staging required because standard cached snapshot resolution failed |
| [SQLite startup](sqlite-startup-kernel.json) / [log](sqlite-startup-kernel.log) | Five fresh-process standalone client probes; no Engine or Alembic measurement |
| [Ruff](performance-final-ruff.log), [format](performance-final-format.log), [typing](performance-final-mypy.log) | 26 selected Python files lint/format clean; 16 source files strict typing clean |
| [References](performance-reference-check.json) | Generated defaults, catalog and configuration snapshots match |
| [Final state](performance-final-state.json) | Dirty-tree source/artifact hashes and explicit verification counts |

[Implementation summary](../PERFORMANCE_IMPLEMENTATION_RESULTS.md). Per owner scope,
no complete-Engine performance test or full-suite rerun accompanies this change.

## Current durable runner: Temporal + embedded Resonate

DBOS is removed from active source, templates, configuration and packaging.
Install `[temporal]` for the server-backed durable runner; see
[operations](../TEMPORAL_RESONATE_OPERATIONS.md) and
[ADR-071](../adr/ADR-071-temporal-resonate-orchestration.md).

| Gate | Actual result | Evidence |
|---|---|---|
| True live Temporal Server + local Resonate | **9 passed, no skips** | [Final live log](temporal-live-final-tests.log) |
| Final config/runner/journal/boot/import regressions | **161 passed** | [Focused log](temporal-migration-final-state-tests.log) |
| Earlier expanded selection | 1,308 passed, 13 skipped, 1 config failure during implementation; corrected config is covered by final green gate | [Original log](temporal-migration-regression.log) |
| Static | Ruff passed; mypy passed across 608 source files | [Ruff](temporal-migration-ruff-final.log), [mypy](temporal-migration-mypy.log) |
| Packaging/dependencies | Wheel built and inspected; lock/current direct dependencies checked; 174 installed distributions compatible | [Final manifest](temporal-runtime-final-state.json), [build](temporal-migration-build.log) |

The live gate uses `MEMSPINE_TEST_TEMPORAL_ADDRESS`; pytest scrubs other
`MEMSPINE_*` variables. The initial unprefixed-gate attempt skipped live dispatch;
it is not live evidence. The final nine-test run used a real task-owned Temporal
Server 1.32.0 (CLI 1.9.1), SDK 1.34.0 and Resonate 0.8.1 on Python 3.13.5.

Verified behavior includes actual Engine sleep/reopen, failed Activity retry,
Worker reopen with committed result reuse, and changed-input rejection. Recovery
uses the same event database and journal volume. These tests do not prove hard
container failover, cross-host storage attachment, production authorization or
exactly-once external effects. The checkpoint unit is a registered maintenance
pipeline; the ingestion outbox remains its own inline/taskiq transport.

The optional import/packaging gate passed 3 tests, and Workflow/runner type checking
passed with both SDKs absent. The final full source type check also passed with
the SDKs installed; CI includes that optional-SDK matrix.

Earlier DBOS/dependency/template reports below are historical. Their hashes and
results are preserved; they do not describe active DBOS support.

## Refreshed Python 3.13 lock follow-up

The reports below supersede the dependency compatibility gate only. Historical
performance and earlier test reports remain unchanged; they are not measurements
of the refreshed source/dependency tree.

| Gate | Result | Evidence |
|---|---|---|
| Native Postgres/Neo4j/Redis/Valkey/OpenSearch/Weaviate contracts | 8 passed, no skips | [Refreshed lock report](live-provider-refreshed-lock.json) |
| Native pgvector contract and migration inspection | 1 passed; Alembic head and recovery table confirmed | [pgvector report](pgvector-live-refreshed-lock.json) |
| Granular synthetic ingestion F1.8 | 7/7 checks passed; no model weights | [Trace](ingest-f18-trace.log) |
| Synthetic offline flow | 30/30 checks passed | [Trace](sleep-flow-trace.log) |
| Synthetic retrieval flow | 33/33 checks passed | [Trace](read-flow-trace.log) |
| GLiNER AutoExtractor adapters/config defaults | 69 passed, 1 skipped; fake SDKs | [Log](gliner25-adapters-tests.log) |
| SQLite journal and verification ledger | 27 passed; subprocess-exit recovery and stale fencing included | [Log](journal-and-ledger-tests.log) |
| Real Resonate + Temporal ActivityEnvironment | Committed-step reuse and sandbox preparation passed | [Report](temporal-resonate-journal-template.json) |

These three flow scripts use separate in-memory fixtures. They do not prove one
message traverses a single persisted end-to-end run or every provider combination.
[Ledger usage](LEDGER_USAGE.md) explains the manually recorded, hashed evidence
index. Deployment failover and real GLiNER model inference remain separate gates.

## Historical implementation follow-up: ADR-069

Repository changes include OpenSearch lexical, Weaviate lexical/vector,
pgvector vector, audited cleanup-fence recovery, dependency-safe teardown,
typed HTTP service-change execution and running-deployment inspection.
Database copying, re-embedding, paid probes and production cutover were not run.

| Current gate | Evidence |
|---|---|
| Full offline unit sweep, 513 files | 7,434 passed, 61 skipped; three metadata failures were corrected and retested |
| Affected metadata/registry/pgvector suites after fixes | 37 passed; the two metadata files separately passed all 26 tests |
| Final config/runtime/provider regression suites | 352 passed, including mutated typed-config range and cross-field validation |
| Combinations/integration | 96 passed, 9 skipped (unconfigured live-service gates) |
| Latest combined live matrix | 8 passed, no skips; [version/image evidence](live-provider-core-latest.json) |
| Separate pgvector native contract | 1 passed; [evidence](pgvector-live-contract.json) |
| Static checks | Ruff passed; mypy passed across 606 source files |
| Packaging | Wheel built; dependency lock and generated references checked |

The full sweep's three initial failures were obsolete registry expectations and
native-provider settings incorrectly counted as pipeline feature flags. Native
settings now remain in their separate defaults/schema catalog. Every identified
failure was fixed and the affected suites passed; the full partitions were not
rerun after those metadata-only fixes. Do not describe the initial sweep as an
unqualified green run. Logs are in `%TEMP%/memspine-unit-final-20261017` and
`%TEMP%/memspine-provider-followup-metadata.log`.

### Final five-trial performance measurement

The [latest raw samples](provider-benchmark.json) use alternating fresh processes,
120 synthetic messages and 60 searches per trial, with all task-owned heavy test
jobs stopped. Source fingerprints were stable; stored/search hashes match every
baseline/current trial and LLM usage is empty. The baseline is `a81e7f0`.

| Metric | Baseline | Current | Change |
|---|---:|---:|---:|
| Import, median | 2,194.51 ms | 1,932.69 ms | -11.93% |
| Cold startup after import, median | 1,343.96 ms | 1,505.99 ms | +12.06% |
| Import plus cold startup, median of sums | 3,551.27 ms | 3,453.42 ms | -2.76% |
| First explicit config serialization, median | 0.28 ms | 2.97 ms | +967.28% |
| Warm startup, median | 140.10 ms | 142.15 ms | +1.46% |
| Warm startup, p95 | 178.75 ms | 168.46 ms | -5.76% |
| Ingestion, median | 3,399.46 ms | 3,419.19 ms | +0.58% |
| Search, p50 | 68.42 ms | 68.31 ms | -0.16% |
| Search, p95 | 107.11 ms | 103.85 ms | -3.05% |
| Peak Windows working set, median | 310.55 MiB | 315.50 MiB | +1.59% |

Snapshot construction defers redundant frozen-model schema building. The earlier
single instrumented profile fell from about 70 ms to 22.6 ms; that profile predates
the final typed-input range/cross-field revalidation fix and is retained as component
evidence, not a final end-to-end speed claim. Original input validation remains eager,
and startup revalidates mutable typed input before freezing while preserving field
presence. First serialization is measured separately (about 2.69 ms extra).

Cold startup after import remains **12.1% slower**, exceeding the investigation
threshold. The profile shows about 180 ms of SQLite engine/migration module import
paid during startup rather than the old eager import. Combined import/startup is
2.8% faster in the final sample, but that does not erase the cold-start regression.
LanceDB import/connect remains the largest first-start component.

Warm-start p95 is 5.8% faster in this final run, unlike the earlier sample's slower
tail. The variation across runs on a shared Windows host prevents a general latency
or production p95 guarantee. Ingestion is 0.6% slower and search p95 3.0% faster.
Stored and ordered-search hashes match. No unconditional performance pass is claimed.

### Actual deployment prerequisite

No production endpoint, authorized workload or deployment controller was supplied.
The HTTP adapter is executable and protocol-tested, but the application must
provide the controller that actually fences/restarts services and reconciles
worker acknowledgements. The recovery API similarly requires a real quiescence
guard. `inspect_deployment` never labels basic readiness as full production
validation. See [the deployment runbook](../PROVIDER_DEPLOYMENT_RUNBOOK.md).

## Earlier ADR-068 gate (historical)

The following counts/profiles describe the initial gate before the follow-up
features above. They are retained as evidence rather than current status.

| Gate | Result |
|---|---|
| Full offline unit suite, 506 files | 7,440 passed, 31 skipped, zero failures/errors |
| Combinations and integration | 96 passed, 5 skipped |
| Disposable live backend matrix | 5 passed; no skips; all owned containers removed |
| Interactive ingestion F1.8 | 7/7 checks passed |
| Ruff | Source and new provider scripts passed |
| Mypy | 587 source files passed |
| Generated references and whitespace | Config/default/stage references and `git diff --check` passed |

The unit run collected `tests/unit` normally in each process and selected three
disjoint partitions through a temporary collection-filter plugin. Partition
results were 2,487/3, 2,474/17 and 2,479/11 (passed/skipped). Logs and manifests
are in `%TEMP%/memspine-unit-filtered-20261017-202214`. An earlier file-list
partition attempt had fixture-discovery errors and is not counted as a pass.
The run exposed and fixed rejection of valid JSON enum names by blanket strict
policy validation; policies now share scalar validation with the config tree.

The [live report](live-provider-contracts.json) records versions and image
digests. The [live test guide](../v2/PROVIDER_LIVE_CONTRACTS.md) describes the
write/reopen/search, binding, graph traversal/deletion and cache TTL/isolation
checks. Unconfigured remote cases skip explicitly in the ordinary integration
run; their separate Docker execution is the live evidence.

## Repeated local performance comparison

[Earlier raw measurements](provider-benchmark-before-optimization.json) compare that source with the
captured `a81e7f0f2077f50a6cd1e3cbe339e83e386ab6fc` source. Five fresh-process
trials per revision alternate execution order. Each uses a fresh database, 120
invented messages, 60 measured searches, and five warm reopenings. SQLite,
LanceDB, Tantivy and hash embeddings are selected. There are no model downloads
or remote calls. Source fingerprints were unchanged throughout the run.

| Metric | Baseline | Current | Change |
|---|---:|---:|---:|
| Engine import, median | 2,381.7 ms | 2,095.6 ms | −12.0% |
| Cold startup after import, median | 1,320.4 ms | 1,503.4 ms | **+13.9%** |
| Import plus cold startup, median of paired sums | 3,708.6 ms | 3,557.6 ms | −4.1% |
| Warm startup, median | 124.0 ms | 128.4 ms | +3.5% |
| Warm startup, p95 | 142.9 ms | 140.4 ms | −1.7% |
| Batch ingestion, median | 3,470.0 ms | 3,389.7 ms | −2.3% |
| Search, p50 | 64.3 ms | 65.4 ms | +1.6% |
| Search, p95 | 98.2 ms | 106.4 ms | +8.4% |
| Stop, median | 6.37 ms | 6.65 ms | +4.5% |
| Peak Windows working set, median | 311.25 MiB | 316.83 MiB | +1.8% |

Stored-text and ordered search-result hashes match across every trial and both
revisions. Every trial reports zero LLM usage. These invariants complement the
unit tests; matching content hashes alone do not establish all API semantics.

### Cold-start investigation

Cold startup exceeds the agreed 10% investigation threshold. Separate cProfile
captures identify first-start lazy imports and configuration snapshot creation:
the current capture includes about 0.07 s in `freeze_effective_config`, of which
about 0.06 s is frozen-model creation. It also imports SQLite storage/migration
modules during startup (about 0.19 s inclusive). Import work overall during the
profiled startup is larger than in the baseline. These inclusive timings must
not be added, and profiling overhead makes them different from benchmark times.

The [current profile](startup-profile-current.json) and
[baseline profile](startup-profile-baseline.json) retain module/function evidence.
This supports a partial explanation of the cold-start increase; it does not
attribute every millisecond. No safety validation was removed to conceal the
regression. Faster combined import/startup does not erase the separate startup
regression. The result is investigated and documented, rather than represented
as an unconditional performance pass.

This is a shared Windows development machine, with recorded average host CPU
usage of roughly 15–26% across workers. Task-owned test jobs had stopped before
measurement, but unrelated host activity was not controlled. Five trials are
limited evidence; production backend/model throughput and quality remain
deployment-specific. No production latency guarantee or universal speedup is
claimed.

To repeat, provide the captured baseline source directory explicitly:

```powershell
$env:PYTHONPATH = 'src;evals'
& $py scripts/benchmark_provider_architecture.py `
    --baseline-src $baselineSrc --trials 5 --messages 120 --queries 60 `
    --out docs/provider_verification/provider-benchmark.json
```

## Explicit boundaries

OpenSearch, Weaviate and pgvector were implemented by the follow-up above.
Switching non-projection services requires the application's actual controller,
for which the typed HTTP adapter and wire contract are now supplied.
Database copying and re-embedding are excluded by the owner's scope decision.
Cleanup is opt-in and drains on graceful shutdown; its public recovery API
requires verified quiescence and persists an audited fence/revision CAS.
See [provider operations](../PROVIDER_OPERATIONS.md) and the
[architecture plan](../PROVIDER_ARCHITECTURE_PLAN.md).


## Delegated follow-up wave — 2026-10-17

| Gate | Observed result | Evidence |
|---|---|---|
| Affected provider/config/runner/tooling/lifecycle regression | 355 passed, 1 skipped; skip explicitly requires a Temporal Server endpoint | [Final regression](provider-followup-final-regression.log) |
| Strict disposable native contracts | 8 passed, zero skips; PostgreSQL, Neo4j, Redis, Valkey, OpenSearch, Weaviate; task-owned cleanup | [Report](native-strict-final.json), [log](native-strict-final.log) |
| Installed-wheel and independent extras | Fresh Windows/Python 3.13.5 wheel Engine write/search/shutdown; isolated OpenSearch/Weaviate/Postgres extras and dependency checks | [Report](installed-wheel-checks.json), [log](installed-wheel-checks.log) |
| Installed plugin wheel | Version/RECORD pins, incompatible contract rejected before build, strict settings, Engine write/search and owned close | [Report](plugin-wheel-checks.json), [example](../PROVIDER_PLUGIN_EXAMPLE.md) |
| One persisted message lifecycle | Raw source identity/time, L1 lineage, offline outcomes and derived retrieval verified; scoped offline model stub | [Report](memory-lifecycle-trace.json), [focused tests](memory-lifecycle-trace-tests.log), [debugging](../MEMORY_LIFECYCLE_DEBUGGING.md) |
| Modern asyncio hook | Combined hook/config gate 124 passed with deprecation warnings treated as errors; native Windows/Postgres also exercised | [Audit](ASYNCIO_AND_SKIP_AUDIT.md), [log](asyncio-hook-tests.log) |
| Static skip inventory | 98 callsites, zero parse errors; historical node-level reasons remain unresolved | [Inventory](test-skip-inventory.json) |
| Breaking guide examples | Four YAML examples validate; services not started | [Report](upgrade-examples-validation.json), [guide](../PROVIDER_UPGRADE_GUIDE.md) |
| Changed tooling/tests | Ruff check and format passed | [Log](provider-followup-final-ruff.log) |

The [CI review](../PROVIDER_CI_COVERAGE.md) and
[platform matrix](../PROVIDER_PLATFORM_MATRIX.md) distinguish configured workflows
from executions observed here. The new native workflow is manual, digest-pinned
and strict about skipped contracts; no remote CI run was triggered. Existing CI
pytest commands now preserve `-rs` reasons. No historical skip attribution or
production acceptance is inferred from these local gates. The earlier full-sweep
failure/retest split is not replaced by this focused regression.

The [wave manifest](provider-followup-final-state.json) binds final source/tooling,
config and evidence hashes. The portable [ledger](runs.jsonl) records selected
executed gates separately from configuration-only checks. See the
[tracker](../PROVIDER_TODO.md) for completed items and remaining boundaries.


## Selected proposals and measured kernels — 2026-10-18

[All 31 selected proposals](../PROVIDER_PROPOSED_DELIVERY.md) now have an explicit
disposition: 25 local gates completed, six implementation/environment/release gates
remain. The [stable tracker](../PROVIDER_TODO.md) now has 125 IDs including PA-124/125
for the owner's fast-library request. Selection is not counted as execution.

| Evidence | Observed scope |
|---|---|
| [Memory types](memory-types-acceptance.json) / [log](memory-types-acceptance.log) | 12 passes: six behaviors under memory and disk cache providers |
| [Graph, literal and accented-Latin](graph-scope-acceptance.json) / [log](graph-scope-acceptance.log) | Six passes, installed Ladybug included |
| [Compression](compression-acceptance.json) | Two deterministic cases; no LLMLingua quality claim |
| [GLiNER structured runtime](gliner-structured-real.json) | Three-call gate ends in live record/relation/mention mapping; candidates remain unasserted |
| [Base](ner-base-comparison.json) / [Small](ner-small-comparison.json) | Ten calls per model on five fixed invented cases; no broad model ranking |
| [Local profiles/scaling](provider-performance-followup.json) | Current validation/import/dump and 100/500/1,000-record diagnostics |
| [Remote timing](provider-remote-latency-followup.json) | Six disposable service operations/fault probes; not search/production workload latency |
| [Finite resource soak](provider-resource-soak.json) | Five warmups + 30 cycles; 105 connects/closes, zero open DBAPI connections after each stop |
| [Native hybrid](native-hybrid-evaluation.json) | Weaviate versus portable RRF synthetic diagnostic; distinct candidate-budget caveat |
| [Earlier affected regression](proposed-wave-regression.log) | 411 passed, one live-Temporal skip before final guard/kernel changes |
| [Final focused gate](proposed-wave-final-focused.log) | 45 passed on final kernels, candidate guards and measurement boundary tests |
| [Final source typing](proposed-wave-final-mypy.log) | Three changed source files pass |
| [JSON benchmark](json-path-benchmark.json) | Existing orjson paths retained; guarded control-metadata replacement slower |
| [CPU primitive benchmark](engine-primitives-benchmark.json) | Final production SimHash kernel versus frozen reference; exact parity, bounded chunk memory |
| [Read-only references](release-reference-check.json) | Defaults, stage catalog and schema/template snapshot match |

The owner explicitly requested **kernel-only** performance validation and no full
engine test for that optimization. No full-engine speedup or final full-suite run
is implied. See [performance audit](../ENGINE_PERFORMANCE_AUDIT.md),
[delivery standard](../PROVIDER_DELIVERY_STANDARD.md) and
[hashed final state](proposed-wave-final-state.json). The final wheel is checked
for source parity; earlier installed Engine smoke predates final source changes.
A final installed import/kernel-only check is separate evidence, not an Engine
acceptance run. PR/release destination and model/representative-host gates remain
explicit in the delivery register. No push or production activation occurred.

Source review pins hash UTF-8 source text after Python universal-newline
normalization, matching the AST audit input; CRLF/LF alone does not invalidate a
review. The final verification manifest separately hashes exact file bytes.
