# Provider performance follow-up

Run recorded 2026-10-17 on Windows 11, Python 3.13.5, revision
`e6596f44c1e9937050ef68938037f2cf2341ee34`. The run uses local SQLite and
LanceDB, fixed hash embeddings, synthetic English records, no model downloads,
and no remote calls. The machine has 32 logical CPUs, but it was shared with
other work; observed host CPU was sampled during the run and does not establish
a controlled benchmark environment.

Reproduce from the repository root with the dependency-validation interpreter:

```powershell
$env:PYTHONPATH = 'src'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
C:\Users\hembad\AppData\Local\Temp\memspine-dependency-validation-313\Scripts\python.exe `
  scripts\benchmark_provider_followups.py `
  --out docs\provider_verification\provider-performance-followup.json `
  --sizes 100 500 1000 --searches 30 --config-repetitions 100 `
  --concurrent-ingest 20 --concurrent-searches 40 --lifecycle-cycles 5 `
  *> docs\provider_verification\provider-performance-followup.log
```

The JSON file contains raw results and run settings; the log preserves engine
output and diagnostics. The harness bounds corpora at 1,000 records and keeps
the concurrent and lifecycle loads configurable. `synthetic_label_recall_at_20`
is matching activity phrase hits across four queries divided by all records
carrying those labels. Precision divides those hits by all returned candidates;
oracle recall is the maximum possible at `top_k=20`. These exact string labels
with hash embeddings provide a fixture/retrieval check, not a semantic quality
score.

The remote run is reproducible with the same interpreter and pinned images
through the task-owned disposable-service runner:

```powershell
$env:PYTHONPATH = 'src;scripts'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
C:\Users\hembad\AppData\Local\Temp\memspine-dependency-validation-313\Scripts\python.exe `
  scripts\benchmark_provider_remote_latency.py --samples 30 `
  --out docs\provider_verification\provider-remote-latency-followup.json `
  *> docs\provider_verification\provider-remote-latency-followup.log
```

Each report records its harness SHA-256, source revision, sample count and
measurement method. The remote report omits generated credentials and includes
container image digests and service versions.

## Findings and acceptance

| Item | Result against the stated criterion |
|---|---|
| PA-047 | **Met locally.** The harness times `freeze_effective_config()` on the effective configuration 100 times, including its current range and cross-field revalidation path; median was 2.995 ms. This extends the earlier component profile. This shared-host result is not a controlled latency guarantee. |
| PA-048 | **Partial.** SQLAlchemy/aiosqlite import and SQLite connect plus schema startup are sampled separately. One sample is insufficient to make a reliable migration/startup optimization decision; no code change was retained. |
| PA-049 | **Local phases measured.** LanceDB import, connection, table creation and table open have separate timings. These are one shared-host sample and do not support choosing an optimization. |
| PA-050 | **Met locally.** First JSON serialization measured 0.083 ms; the median of 100 subsequent dumps was 0.040 ms. This run did not reproduce the historical approximately 2.69 ms extra cost in `provider-benchmark.json`; conditions differ, so this is a diagnostic comparison. |
| PA-051 | **Not met; needs controlled environment.** CPU activity is recorded, but the machine was shared and neither CPU nor I/O resources were isolated. Do not derive a latency budget from this run. |
| PA-052 | **Exploratory results at 100, 500 and 1,000 records.** Fixed settings, integrity checks, exact-label hit counts, precision, oracle recall, search p50/p95 and working-set samples are included. Search p95 was 116, 252 and 503 ms. Label Recall@20 was 0.80, 0.16 and 0.08 respectively; precision was 1.00 at each size. Hash embeddings and this synthetic set do not support semantic quality claims. The PA-051 controlled-host dependency remains unmet, and one run per size does not establish variability. |
| PA-053 | **Partial.** Forty searches overlapped a 20-record ingest at the 1,000-record corpus; foreground search p95 was 586 ms. The full new batch became list-visible during ingest, before `ingest_messages` returned. This visibility observation is not derivation lag. No agreed acceptance budget exists, so the criterion is unmet. |
| PA-054 | **Measured against disposable local providers.** Thirty operations per backend report p50/p95/p99 for PostgreSQL, Neo4j, Redis, Valkey, OpenSearch and Weaviate. The same clients were checked against unused loopback ports with configured timeout/error bounds; PostgreSQL's 1-second driver timeout returned in about 2 seconds and is explicitly recorded. These results are local service evidence, not production tail latency or an SLO. See the separate remote report and log. |
| PA-055 | **Partial pending review.** The 5-cycle endpoint-delta check is now supplemented by a separate fresh-process 5-warmup/30-measured-cycle soak. SQLAlchemy events observe actual SQLite DBAPI lifetimes, with zero open after stop and balanced connect/close events; all owned resources stop. The fixed RSS spread diagnostic guard was not exceeded. This finite local run cannot establish unbounded leak freedom or long-running production behavior. |

The component timings from this single run were:

| Phase | Time ms |
|---|---:|
| SQLAlchemy import | 183.214 |
| aiosqlite import | 10.519 |
| SQLite client module import | 67.186 |
| SQLite connect plus schema startup | 20.247 |
| LanceDB import | 858.311 |
| LanceDB client module import | 0.341 |
| LanceDB connect | 3.408 |
| LanceDB table create | 9.016 |
| LanceDB table open | 6.253 |

The six local service runs each used 30 round trips. The remote report records
connect time, operation, timeout settings and all raw samples; these tail
percentiles are useful for checking the report and are not workload forecasts.

| Backend operation | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| PostgreSQL `SELECT 1` | 0.670 | 1.459 | 1.562 |
| Neo4j `RETURN 1` | 4.988 | 10.843 | 598.648 |
| Redis `PING` | 0.559 | 0.834 | 1.066 |
| Valkey `PING` | 0.581 | 0.705 | 0.717 |
| OpenSearch HTTP ping | 1.148 | 1.629 | 1.729 |
| Weaviate SDK readiness | 0.944 | 1.238 | 1.280 |

The Neo4j p99 includes one 599 ms outlier in this 30-sample shared-host run.
For fault probes against an unused local port, all six clients returned by the
reported error bound. PostgreSQL's `connect_timeout=1s` took about 2.01 seconds
to report `ConnectionTimeout`; its measured effective bound was 2.5 seconds.

**RM-40 follow-up (2026-10-22).** The probe called `psycopg.connect` directly.
psycopg follows libpq: it raises any `connect_timeout` below 2 s to 2 s, uses
whole seconds, and applies the value to *each* host/address attempt
(`psycopg.conninfo.timeout_from_conninfo`). Measured on Windows with psycopg
3.3.4 against a closed port and against a listener that never answers:
`127.0.0.1` with 1 s took 2.03 s and with 3 s took 3.04 s; `localhost` (::1
then 127.0.0.1) with 1 s took 4.07 s and with 3 s took 6.04 s. MemSpine's
`PostgresClient.connect` now enforces `connect_timeout_s` as one overall
deadline (`TimeoutError`) across all attempts: 1.0 s configured returned in
1.15 s instead of 4.12 s. The per-attempt driver value is
`max(2, ceil(connect_timeout_s))` (`PG_DRIVER_MIN_CONNECT_TIMEOUT_S`), which
still bounds pooled reconnects after start-up and the sync (Alembic) engine.
Those have no overall deadline: allow that value times the number of
resolved addresses. `tests/unit/clients/test_postgres_client.py` checks the
deadline against a silent local listener; no live server is needed.

No provider implementation was changed. In particular, the local component
timings do not isolate a repeatable startup bottleneck with enough evidence to
justify an optimization. There is no latency SLO in this report. Real
derivation queue lag, connection growth and controlled-host acceptance remain
follow-up work requiring the relevant environment and acceptance budgets.

### PA-055 fresh-process lifecycle soak

Run the bounded local-only soak with:

```powershell
$env:PYTHONPATH = 'src'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
C:\Users\hembad\AppData\Local\Temp\memspine-dependency-validation-313\Scripts\python.exe `
  scripts\check_provider_resource_soak.py `
  --out docs\provider_verification\provider-resource-soak.json
```

This worker used SQLite, hash embeddings, Tantivy and LanceDB in a fresh
process. Its settings were fixed before execution: 5 warmup cycles, 30 measured
cycles, 2 messages and 2 searches per cycle, a 180-second wall-time cap, and a
100 MiB maximum post-warmup working-set spread diagnostic guard. It completed
in 20.8 seconds. The measured post-stop RSS spread was 5,079,040 bytes, below
that declared guard. The guard is diagnostic only, is not an SLO and does not
establish an unbounded leak-free trend.

Process-local SQLAlchemy Engine event listeners observed 105 DBAPI connects and
105 closes (three per cycle, including each SQLite engine's anchor connection).
All 35 cycles recorded zero open DBAPI connections after stop; all 3 owned
resources in each cycle reached `stopped`. During the measured cycles,
post-stop Windows handle counts remained 348. The worker's process-start to
process-end RSS/handle counts include imports and one-time initialization, so
they are not per-cycle leak estimates. Raw per-cycle RSS, handle, connection,
resource and latency samples are in the JSON report; the log starts with the
declared guard and retains engine diagnostics. Measured nearest-rank p50/p95/p99
latencies were 174.7/201.8/217.2 ms startup (n=30), 106.8/122.6/123.2 ms ingest
(n=30), 8.6/13.3/15.5 ms search (n=60), and 5.0/5.8/6.0 ms stop (n=30).
These are shared-host local diagnostics, not production budgets.

The finite 30-cycle sample provides evidence about these observed lifetimes;
it cannot prove unbounded leak freedom or represent long-running production
behavior. PA-055 remains **partial pending review** of this additional evidence.
