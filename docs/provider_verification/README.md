# Provider implementation verification — 2026-10-17

## Latest follow-up: ADR-069

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
