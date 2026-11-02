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

## 2026-11-02: wave-6 reference-host pass (PA-051, RM-25, RM-26, RS-06, PA-053)

This pass ran on the owner's reference timing host (ADR-096). No other measurement ran
during it. Each family has one PA-051 record under `docs/provider_verification/`, written by
`scripts/perf_protocol.py`. A record carries the commit, a clean-tree flag, host specs and the
power plan, CPU% before and after with the top processes, and n/p50/p95/p99/stdev/min/max.
The records are `perf-rm25-onnx-clean-cache`, `perf-rm26-onnx-cpu-envelope`,
`perf-rs06-packer-token-counting`, `perf-rm26-read-latency-reranker`,
`perf-pa053-concurrent-ingest-search` and `perf-pa053-write-path-profile`, each with the
suffix `-2026-11-02.json`. The harnesses are `scripts/benchmark_onnx_envelope.py`,
`scripts/benchmark_engine_read_ingest.py` and `scripts/profile_write_path.py`. Every record
was taken on a clean tree at commit d737fabf, 74b3a676, 538a5d62 or da523ce0; these commits
differ only in harness changes.

**Host.** AMD Ryzen 9 9950X3D, 16 cores / 32 threads, 125.6 GiB RAM. Windows 11 Enterprise
Insider Preview, build 26340. Power plan: High performance. Python 3.13.5, onnxruntime
1.31.1, tokenizers 0.23.3, huggingface_hub 2.4.0, numpy 2.5.4, lancedb 0.41.0, tantivy
0.27.0, SQLAlchemy 2.1.6. Host CPU was 1.6-8.8% before and after every family.
The top process in each sample (`python.exe`, about 22% of one thread) is most likely the
sampling process itself. An RDP client (`mstsc.exe`, about 20-26% of one thread) and, once,
Chrome (48% of one thread) were active. The host was quiet by total load, but it was not
isolated.

### RM-25: clean cache, first load, steady state

`memspine models fetch --template base --model Xenova/ms-marco-MiniLM-L-6-v2` filled an
empty Hugging Face cache in 14.7 s, which includes the network download. All 8 pinned files
reported `sha256 ok`, and `--check` re-verified them offline in 2.1 s. The figures below come
from 30 fresh processes each, loading from that cache with threads 0 and batch 32.

| Model | import ms p50 | load ms p50 (incl. SHA-256) | first call ms p50 | first load total ms p50/p95 | steady batch-32 ms p50/p95 (n=300) | items/s |
|---|---:|---:|---:|---:|---:|---:|
| bge-small-en-v1.5 (embedder) | 188 | 338 | 50 | 575 / 614 | 40.4 / 43.8 | 792 |
| ms-marco-MiniLM-L-6-v2 (cross-encoder) | 189 | 290 | 40 | 522 / 557 | 29.4 / 31.2 | 1087 |

A cold-disk first load cannot be reproduced on Windows without a reboot. Process 1 ran
right after the download, with the OS file cache warm.

### RM-26 / RS-06: CPU envelope

Each call is one ONNX session run over `batch` invented 15-30-word turns, or over
query-document pairs for the cross-encoder. Each cell has 30-100 repetitions after 5 warm-ups.
The table gives p50 ms, with items/s in brackets. Outputs were bit-identical across thread
settings (max abs diff 0.0).

| Threads | bge b=1 | b=4 | b=16 | b=32 | b=64 | MiniLM b=1 | b=4 | b=16 | b=32 | b=64 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 (ORT default) | 2.33 (429) | 7.14 (561) | 21.1 (758) | 39.5 (811) | 73.2 (874) | 2.03 (493) | 6.09 (657) | 15.8 (1011) | 28.9 (1108) | 57.3 (1116) |
| 1 | 5.25 (190) | 24.0 (167) | 94.3 (170) | 188 (170) | 374 (171) | 2.63 (380) | 13.1 (305) | 50.5 (317) | 101 (318) | 200 (320) |
| 2 | 4.59 (218) | 17.8 (225) | 77.2 (207) | 160 (200) | 302 (212) | 2.69 (371) | 10.8 (371) | 42.3 (379) | 88.4 (362) | 176 (364) |
| 4 | 2.86 (349) | 12.1 (329) | 42.3 (379) | 83.2 (385) | 160 (400) | 1.72 (582) | 8.32 (481) | 25.5 (628) | 49.8 (643) | 98.1 (652) |
| 16 (physical) | 2.03 (492) | 6.73 (594) | 19.4 (825) | 35.4 (903) | 61.9 (1034) | 1.63 (613) | 6.32 (633) | 15.2 (1053) | 27.6 (1159) | 53.2 (1204) |
| 32 (logical) | 2.79 (359) | 7.94 (504) | 27.5 (581) | 50.8 (630) | 86.6 (739) | 2.32 (430) | 7.26 (551) | 20.8 (771) | 37.3 (859) | 83.4 (768) |

At 32 threads, p95 is 1.5-2.5x p50: bge b=64 has p95 134 ms against p50 87 ms. SMT
oversubscription is slower and noisier.

**Recommended onnx slot defaults: keep `threads: 0` and `batch_size: 32` for both slots.**
- Threads 0 resolves to the physical cores and matches the explicit 16. Never set threads to
  the logical count.
- Batch 64 gains only 4-15% items/s for twice the per-call latency.
- The read path reranks a 16-document pool in a single call.
- The case where several onnx slots infer at the same time in one process was not measured.
  A deployment that runs ingestion and reads concurrently may want each slot pinned to half
  the physical cores; that would need a measurement.

**int8 vs fp32 parity: no int8 entry.** The pinned catalogue has no separate int8 file
entry (ADR-093 amendment), so parity was not run and no unpinned file was added.

**Packer token counting (RS-06).** The contexts are invented turns rendered one per line.
Each cell gives p50/p95 ms over 200 runs; truncation over 50.

| Context (estimated tokens) | estimate `len//4+1` count | exact bge WordPiece count | `truncate_to_fit` to half, estimate | `truncate_to_fit`, exact |
|---|---:|---:|---:|---:|
| 512 | 0.0001 / 0.0002 | 0.45 / 0.53 | 0.22 / 0.43 | 2.48 / 4.00 |
| 2,048 (default budget) | 0.0001 / 0.0002 | 1.77 / 2.83 | 1.08 / 1.93 | 11.6 / 17.1 |
| 8,192 | 0.0001 / 0.0002 | 8.28 / 11.7 | 5.36 / 7.67 | 54.9 / 68.7 |

At the default budget, an exact count costs about 2 ms per pass over the context. Truncation
recounts on each cut, so with an exact counter it costs about 10x the estimate. tiktoken
`cl100k_base` was not cached and did not resolve offline. Recommendation: keep the estimate
as the default and leave `read.exact_budget` as opt-in.

### Read latency with and without the MiniLM reranker

The store uses the base template, rules-only, with the onnx bge-small embedder. The corpus
is invented turns from one human sender, 20 per session, and 22% of them are rule-derivable
first-person statements. Each cell is 100 reads after 10 warm-ups. The reranker was invoked
on all 100 measured reads, with 16 documents per call and no failures.

| Corpus | rerank off p50/p95/p99 ms | rerank on p50/p95/p99 ms | reranker model call p50/p95 ms | net added p50 / p95 ms | context tokens p50 off → on |
|---|---:|---:|---:|---:|---:|
| 1,000 | 191 / 350 / 365 | 203 / 310 / 361 | 22.2 / 23.9 | +11.2 / −39.4 | 1,857 → 1,230 |
| 2,440 (10k requested) | 449 / 580 / 643 | 452 / 607 / 630 | 22.3 / 24.0 | +3.1 / +26.4 | 1,892 → 1,462 |

The reranker costs about 22 ms per read; the "model call" column is the in-engine span
around `gated_rerank`. The net change at p50 is smaller, because a reranked read packs
a context about a quarter to a third smaller, which shortens the later stages. The p95
differences are within read-to-read noise.

The 10k corpus was not reached: a 600 s seeding cap stopped it at 2,440 records (see the
finding below). Read latency itself grows with the corpus: p50 was 191 ms at 1k and 449 ms
at 2.4k, without the reranker.

**MiniLM default (ADR-093): recommend yes.**
- The cost is 22 ms per read, inside ADR-093's 20-60 ms estimate, and about 1-6% of the
  current read p50.
- The weights fetch and verify cleanly, and the first load is about 0.5 s.
- Two conditions are unchanged: there is no int8 variant, and retrieval quality was not
  measured here.

This is a recommendation only. The template default was not changed.

### PA-053: concurrent ingest plus search, saturated

The store was a 500-record seed (1k was cut for time), with 2 closed-loop readers using a
50 ms think time. Writes were open-loop. Lag was sampled about twice per second.

| Step | target msgs/s | msgs/call | achieved msgs/s | write-ack p50/p95/p99 ms | read p50/p95/p99 ms | derivation lag p50/p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| single_1 | 1 | 1 | 1.00 | 551 / 1,353 / 1,836 | 649 / 1,622 / 2,541 | 120,387 / 121,394 |
| single_10 (earlier, killed run; stdout only) | 10 | 1 | 1.19 | — / 206,955 / — | — / 2,215 / — | — / 183,219 |
| batched_10 | 10 | 5 | 1.71 (saturated) | 33,852 / 129,901 / 146,232 | 1,284 / 2,956 / 3,998 | 145,202 / 191,238 |
| batched_50 | 50 | 25 | 1.33 (saturated; 4 calls cancelled at the 300 s cap) | 68,747 / 280,333 / — | 1,425 / 3,525 / 3,867 | not measured |

What the steps show:
- The write path saturates at about 1-2 msgs/s, whether messages are sent one per call or
  batched. Every write step at or above 10 msgs/s therefore measures queueing, not service
  time. Single-message 50 msgs/s was skipped.
- Derivation lag is about 120 s even at 1 msg/s. That equals `write.window_idle_s`: facts
  readiness waits for the idle cut of the open window.
- **No PA-053 budgets are proposed.** At 10 msgs/s every number is saturated. Budgets wait
  on the write-path fixes below, and the lag budget also waits on a decision about the
  idle-window readiness.

### Where the time goes (`perf-pa053-write-path-profile-2026-11-02.json`)

The profile used the same seeded store, after the PA-053 steps, with about 1,260 messages.
It was the only workload on the host. Each operation ran 10 times after a warm-up, under
cProfile plus wall-clock spans. The spans are inclusive and can nest, so they do not sum to
the operation time.

| Operation | wall p50 / p95 ms |
|---|---:|
| `Engine.write` (episodic) | 309 / 512 |
| `ingest_messages` (1 message) | 314 / 466 |
| `Engine.read` alone | 353 / 711 |
| `Engine.read` during a 20-message ingest | 672 / 820 (ingest 2,675 ms) |

| Span (per write / per single-message ingest) | ms | thread |
|---|---:|---|
| window drain run inline in the writer's task (`windows.drain`) | 218 / 219 | loop |
| — of which outbox `SqlJobStore.jobs()` full scans (6-7 calls) | 192 / 187 | worker |
| LanceDB per-row `upsert` (vector projector) | 58 / 38 | loop → thread |
| Tantivy `_index_one` / `_commit_if_pending` (one `IndexWriter.commit` per record) | 52 / 44 | worker |
| SQL `SELECT`s (36-41 statements) | 38 / 43 | both |
| `windows.observe` | 27 / 29 | loop |
| outbox `put_draft` | 22 / 24 | worker |
| embedding (2 calls) | 21 / 36 | loop → thread |
| derivation discovery (`discover_committed`) | 11 / 19 | loop |
| intake ledger (`record_intent`, `apply_intent`, `intents_by_id`, `record_ids`) | 32 / 32 | loop |
| events `set_offset` (3) + records projector apply | 16 / 13 | loop |
| `BEGIN IMMEDIATE` waits, `COMMIT` + fsync, `wal_checkpoint` | under 1 ms per op at the median | — |

Entity-index `with_entity_links` and `relink_events` did not register: the base template's
entity policy did not engage on these writes.

cProfile for 10 writes has `outbox._row_to_job` called 96,726 times, about 9,700 job rows
deserialized per write. Those calls cost 944 ms cumulative, before the SQL fetch.
`runtime/jobs.py` `claim`, `expire_leases` and `live_jobs` call `store.jobs()`, which selects
the whole outbox table every time. That cost grows with every message ever ingested.

This matches the seeding trace. A 20-turn `ingest_messages` call took 1.0 s on an empty
store and 6.7 s at 2,400 messages, a cost that grows roughly linearly with history.

The background derivation of 105 windows (`flush_windows`) took 11.1 s.
`SqlJobStore.jobs()` alone took 2.46 s of that, across 125 calls and 198,642 rows
deserialized. Per-row Tantivy and LanceDB applies took 1.24 s and 0.83 s.

Reads do not wait on locks: no `asyncio.Lock` wait of 0.5 ms or more was recorded.
- Alone, a read issues 87 `SELECT`s.
- cProfile shows `list_records` called 30 times per read, about 2,700 rows built through
  `_row_to_record` and pydantic validation, about 150 ms per read.
- Episodic `sessions()` re-detects sessions over the whole namespace, about 117 ms per read.
  Both grow with the corpus.
- During an ingest, the same read takes twice as long (`SELECT` time 53 → 114 ms). The
  writer's inline drain and its synchronous outbox SQL occupy the event loop and the GIL.

**Most likely fixes, ranked. Not implemented; `src` is unchanged.**
1. **Indexed outbox queries instead of `SqlJobStore.jobs()` full scans.** In
   `runtime/jobs.py` claim, expire-leases and live-jobs, and in `runtime/runner.py` and
   `transport.py`: query by state, stage and lease expiry with an index, and prune or archive
   finished jobs.
   - Expected: about 190 ms of the about 310 ms per write (about 60%) at 1.3k messages, and
     about 22% of `flush_windows`.
   - It also removes the linear growth: per-call ingest would stay near its empty-store 1 s
     instead of reaching 6.7 s at 2.4k.
   - This is the largest and the only O(history) write term.
2. **Batch the per-record index commits.** One LanceDB `table.add` per `ingest_messages`
   call or projection batch instead of one per row, and a Tantivy group commit per batch or
   timer instead of `IndexWriter.commit` per record.
   - Expected: about 80-100 ms per message (vector about 40-60 ms, lexical about 35-50 ms).
   - For a 20-message call, about 1.1 s of LanceDB and about 0.9 s of Tantivy shrink to
     about one commit each. Fewer LanceDB fragments should also help search.
3. **Bound the read path's namespace scans.** Replay/session expansion and `sessions()`
   should query by session and time window (indexed SQL, or a maintained session index)
   instead of `list_records` over the whole namespace.
   - Expected: about 150-270 ms of the about 350 ms read at 1.3k records.
   - It flattens the read growth seen above (191 → 449 ms from 1k to 2.4k).
   - Moving the window drain off the writer's task would also stop writes slowing
     concurrent reads by 2x.

**Measurement caveats.**
- The 10k corpus was capped at 2,440 records, and the PA-053 seed was 500 rather than 1k.
- single_10 survives only as the stdout line of a killed run.
- The fetch wall time includes the network.
- The read p95 deltas with and without the reranker are within noise.
- Lag polling calls `wait_for_derivation`, which refreshes readiness rows. It was sampled at
  about 2 msgs/s to keep that load small.
- Worker-thread time (LanceDB, Tantivy, onnx) is invisible to cProfile and appears only in
  the spans.

## 2026-11-02 (after the write-path fixes): PA-053 re-measured after RM-159, RM-160 and RM-161

This pass re-ran PA-053, the write-path profile and the read-latency series at commit
92915cf2. That commit is f375b4b6, where RM-159, RM-160 and RM-161 are closed, plus harness
changes only. The protocol and the host are the same as in the pass above. No `src`,
template or default was changed.

The records are under `docs/provider_verification/`, each with the suffix `-2026-11-02-after.json`:
- `perf-pa053-concurrent-ingest-search`;
- `perf-pa053-window-idle-5s`, `perf-pa053-window-idle-30s` and `perf-pa053-window-idle-30s-conversation`;
- `perf-pa053-write-path-profile`;
- `perf-rm26-read-latency-reranker`.

Every record is `completed` on a clean tree. None is `diagnostic_only`.

What changed in the harness (`scripts/benchmark_engine_read_ingest.py`, `scripts/profile_write_path.py`):
- `--suffix` and `--family` set the record names.
- `--window-idle-s` overrides `write.window_idle_s`.
- A probe on `WindowPipeline._enqueue` stamps each record's window cut. Each derivation lag is
  then split into the wait for the cut and the processing after it.
- Per-step window statistics are recorded: cuts by reason, turns and tokens per window, and
  rule facts.
- `--conversation-per-session` gives a session's messages one `conversation_id`.
- The seed is now 1,000 records (it was 500). Durations are 60/60/30 s at 1/10/50 msgs/s
  (they were 60/30/10 s). The step cap is still 300 s, with 2 readers and a 50 ms think time.

**Host.** The host is the same: Ryzen 9 9950X3D, 16C/32T, 125.6 GiB RAM, Windows 11 build
26340, power plan High performance, Python 3.13.5, onnxruntime 1.31.1, lancedb 0.41.0,
tantivy 0.27.0. **Not isolated for the whole pass.** Three unrelated single-threaded
CPU-bound Python jobs from other projects started at 19:42-19:47 local time:
`phd05_g29_adaptation.py`, `pr6.confirm` and `sim_bootstrap_null.py`. Host CPU was 7% before
the main run. It was 10-23% during the later runs, two of those jobs at about 100% of one
thread each.
- Clean: the seed and the single_1, single_10 and single_50 steps, which ran 19:23-19:41.
- Overlapped with those jobs: batched_10, batched_50, the profile, the three idle runs and
  the read series.
- The overlapped figures are directional. Their p95/p99 tails are the least reliable numbers.

### PA-053: before and after (window_idle_s 120, the default)

| Step | achieved msgs/s before → after | write-ack p50 / p95 ms after | read p50 / p95 / p99 ms after (before p95) | derivation lag p50 / p95 ms after (before p95) |
|---|---:|---:|---:|---:|
| single_1 | 1.00 → 0.83 | 1,938 / 11,266 | 142 / 3,590 / 4,023 (1,622) | 121,170 / 123,348 (121,394) |
| single_10 | 1.19 → 1.10 (saturated; 271 of 600 calls cancelled at the cap) | 87,197 / 253,597 | 209 / 2,677 / 3,157 (2,215) | 158,000 / 203,933 (183,219) |
| single_50 | not run → 1.53 (saturated; 1,040 cancelled) | 117,978 / 268,229 | 325 / 1,068 / 1,280 | 120,625 / 144,247 (n = 12) |
| batched_10 (5/call)* | 1.71 → 0.76 (saturated) | 120,677 / 264,705 | 1,128 / 3,956 / 4,188 (2,956) | 178,000 / 219,086 (191,238) |
| batched_50 (25/call)* | 1.33 → 0.83 (saturated) | 32,882 / 233,862 | 1,408 / 3,190 / 3,818 (3,525) | none derived within the tail |

\* The step overlapped the external jobs.

**The write path still saturates at about 1-1.5 msgs/s.** Every step at 10 msgs/s or above
measures queueing, as before. The seed is the clearest clean evidence:
- Before and after, a 1k seed took 198 s at 20 turns per call.
- Per call, the time grew from 0.70 s on an empty store to 4.5 s at 900 messages, the same
  slope as before.
- The 600 s read seed again stopped at exactly 2,440 records.

RM-159/160 removed the outbox full scans and the per-row index commits. A second term that
grows with history remains; see the profile below.

**Lag split.** Over all steps, the wait for the window cut is 119.9-120.1 s at p50/p95,
which is `window_idle_s` itself. Every window in these steps was cut by `idle` and held 1 turn.
- Each root message without a `conversation_id` starts its own conversation, so it gets its
  own window. Only the idle timer closes it, and the turn and token caps never apply.
- Processing after the cut is the engine's own lag:

| Step | processing after the cut, p50 / p95 / p99 ms |
|---|---:|
| single_1 | 1,518 / 3,428 / 4,685 |
| single_10 (saturated backlog) | 38,107 / 83,943 / 86,046 |
| batched_10* | 61,550 / 99,598 / 100,845 |

### window_idle_s trade-off (single_1, a fresh 1k seed each time)

| window_idle_s | achieved msgs/s | write-ack p50 / p95 ms | read p50 / p95 ms | lag p50 / p95 ms | wait for cut p50 / p95 | processing p50 / p95 | windows (turns/window) | rule facts | 1k seed wall s / final flush s |
|---|---:|---:|---:|---:|---:|---:|---|---:|---:|
| 120 (main run, clean) | 0.83 | 1,938 / 11,266 | 142 / 3,590 | 121,170 / 123,348 | 119,901 / 120,059 | 1,518 / 3,428 | 60 idle (1) | 14 | 198 / 30.7 |
| 30* | 0.66 | 8,698 / 28,261 | 241 / 2,868 | 31,406 / 40,774 | 29,898 / 30,282 | 1,441 / 11,151 | 60 idle (1) | 14 | 145 / 8.9 |
| 5* | 1.01 | 707 / 1,283 | 232 / 954 | 5,398 / 7,455 | 4,857 / 5,080 | 508 / 2,631 | 60 idle (1) | 14 | 108 / 3.2 |
| 30, one conversation per session* | 0.81 | 9,503 / 16,344 | 244 / 2,418 | 18,145 / 31,820 | 8,182 / 21,303 | 10,968 / 13,403 | 4: 3 by turns, 1 by idle (p50 18, max 20 turns; p50 402 tokens) | 14 | 147 / 9.1 |

\* The run overlapped the external jobs.

What windowing does with a shorter idle:
- **Single-message conversations.** The windows and the extraction are unchanged: 60
  one-turn windows and 14 rule facts at every setting. Only the lag moves, and it moves
  almost 1:1 with `window_idle_s`.
- **One conversation per session.** The rule-only segmenter grows a window to the
  `segment_grow_tokens` line, about 400 tokens or 15-20 turns here. So the turn cap closes
  windows before a 30 s idle does. The facts are again the same 14.
- **The ingest cost also falls with a shorter idle.** The 1k seed took 108 s at 5 s idle,
  145 s at 30 s and 198 s at 120 s, because fewer windows stay open (see the profile).
- The write-ack and read tails at 30 s are worse than at 120 s, but those runs were not on
  an isolated host. Do not read that ordering as an effect of the setting.

### Where the time goes now (`perf-pa053-write-path-profile-2026-11-02-after.json`)

The store is the main run's store after its steps, about 2,300 messages. There were 10 reps
after a warm-up; the run overlapped the external jobs.

| Operation | before (da523ce0, 1.3k) p50 / p95 | RM-160 profile (500) p50 / p95 | after (2.3k) p50 / p95 |
|---|---:|---:|---:|
| `Engine.write` | 309 / 512 | 195 / 369 | 293 / 344 |
| `ingest_messages`, 1 message | 314 / 466 | 187 / 217 | 300 / 391 |
| `Engine.read` alone | 353 / 711 | 288 / 482 | 213 / 245 |
| `Engine.read` during a 20-message ingest | 672 / 820 | 398 / 615 | 312 / 444 |
| `flush_windows` (s / windows) | 11.1 / 105 | 0.88 | 8.5 / 64 (133 ms per one-turn window) |
| 20-message `ingest_messages` | 2,675 | 1,620 | 1,654 |

- `SqlJobStore.jobs()` no longer appears. The outbox span is now `claim_job`, at 10-21 ms
  per op.
- The index applies are one call per batch, but their cost is per call: LanceDB
  `write_pending` (`merge_insert`) takes 49-57 ms and Tantivy `IndexWriter.commit` 48-57 ms.
  The single-message ingest also takes about 70 ms in a LanceDB flat `_search` (the
  neighbour prefetch).
- A read alone issues 3 `SELECT`s, down from 87, at about 62 ms. That is RM-161's single
  validated listing.
- **The remaining term that grows with history is the window draft.**
  - On every `observe`, `WindowPipeline._persist_draft` re-serialises every open turn of
    the namespace, with its 384-float vector, into `outbox.put_draft`.
  - In a 20-message ingest, `windows.observe` takes 362 ms and `put_draft` 278 ms, both
    once per message. `SELECT` time is 498 ms over 355 statements.
  - The open-turn count is (msgs/s × `window_idle_s`) for single-message conversations. It
    is the whole seed for a bulk load that flushes only at the end.
  - That explains the linear seed slope and why a shorter idle makes ingest cheaper.
  - **Next fix (not implemented):** persist the draft per window or as a delta, without
    vectors or with them stored once, so a write costs O(its window) instead of O(open
    turns).
  - After that, the per-call LanceDB `merge_insert` and Tantivy commit, about 100 ms per
    single-message call, bound single-message throughput near 3 msgs/s on this host. Group
    commits across calls would be needed to reach 10.

### Read latency with and without the MiniLM reranker (after)

| Corpus | rerank off p50 / p95 / p99 ms, before → after | rerank on p50 / p95 / p99 ms after | model call p50 / p95 ms | net added p50 / p95 ms | context tokens off → on |
|---|---:|---:|---:|---:|---:|
| 1,000 | 191 / 350 / 365 → **62 / 79 / 81** | 85 / 101 / 105 | 19.4 / 25.4 | +23.4 / +22.5 | 1,857 → 1,230 |
| 2,440 (the 600 s seed cap; 10k requested) | 449 / 580 / 643 → **159 / 203 / 490** | 183 / 224 / 235 | 23.0 / 31.5 | +23.8 / +20.8 | 1,892 → 1,462 |

Reads are 3x faster after RM-161: 62 ms at 1k and 159 ms at 2.4k. They still grow with
the corpus. The reranker now costs a steady +21-24 ms at p50 and p95: 100 calls with 16
documents each, and no failures. The 2.4k off-p99 of 490 ms is one outlier; this series
overlapped the external jobs.

### Proposals (for owner approval; nothing changed)

**PA-053 budgets at 10 msgs/s: still not proposable.** The engine does not sustain
10 msgs/s. It achieved 1.10 single and 0.76 batched. The mechanical rule (p95 × 1.3), which
the record also stores, gives write-ack 329,700 ms, read 3,500 ms and lag 265,200 ms. Those
are queue lengths, not budgets.

Provisional budgets at the sustained rate are below. They use the 1 msg/s single-message
step at `window_idle_s` 5 (p95 × 1.3, rounded up), and that run overlapped the external
jobs. **Proposal only, not a default:**

| Budget (at 1 msg/s, 2 concurrent readers, 1k-1.1k store) | measured p95 | proposed |
|---|---:|---:|
| write-ack p95 | 1,283 ms | 1,700 ms |
| read p95 during ingest | 954 ms | 1,300 ms |
| processing lag p95 (after the window cut) | 2,631 ms | 3,500 ms |
| facts-ready lag p95 | `window_idle_s` + processing | `window_idle_s` + 3,500 ms |

Re-measure at 10 msgs/s once the draft fix lands. Until then, a 10 msgs/s budget would only
record the backlog.

**`window_idle_s`: recommend 30 s for the base template, instead of 120 s.**
- Gain: facts readiness for single-message traffic drops from about 121-123 s to about
  31-41 s.
- Gain: the open-window draft shrinks about 4x. On this harness, 1k-message ingest was 27%
  faster (145 s against 198 s).
- No change in rule-only output: the same windows and the same facts.
- Cost: an idle cut closes a conversation's window early when its turns are more than 30 s
  apart. With LLM `window_extract` (not measured here), that means more and smaller window
  calls, and less context per call.
- 5 s gives the best latency: lag p95 7.5 s and write-ack p95 1.3 s. But it would cut most
  human-paced conversations after every turn, so it suits only rule-only or bulk-load
  deployments.
- LLM-extraction templates should keep a longer idle, or rely on `conversation_id` and the
  turn and token caps.

**MiniLM default (ADR-093): yes.**
- It adds 23.4 ms at p50 and 22.5 ms at p95 at 1k, and 23.8 / 20.8 ms at 2.4k. That is
  inside ADR-093's 20-60 ms estimate.
- Because reads are now faster, it is a larger share of a read than before: +38% of p50 at
  1k (62 → 85 ms) and +15% at 2.4k. Reranked reads stay at or under about 105 ms p99 at 1k
  and 235 ms at 2.4k.
- The cost is flat with the corpus, and context drops 23-34% in tokens.
- Unchanged caveats: there is no int8 variant, and retrieval quality was not measured.

**Unreliable or limited in this pass.**
- External CPU load affected every run after 19:42, as listed above.
- single_50 and the batched steps are saturated by construction.
- batched_50 derived no tracked message within the 150 s tail, so it has no lag figure.
- The 30 s and conversation runs have about 60 lag samples each, from one run. Their p95
  ordering against the 120 s run is not trustworthy.
- The conversation run's processing lag (p50 11 s for 4 windows) was not profiled.
- The 10k corpus is still capped at 2,440 records by the 600 s seed limit, which is itself
  the ingest-growth finding.
