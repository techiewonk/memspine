# SQLite startup kernel measurements

This five-trial diagnostic measures SQLAlchemy, aiosqlite, and
`memspine.clients.sqlite` imports sequentially in fresh Python subprocesses.
Each subprocess constructs one file-backed `SQLiteClient` in its own temporary
directory, times `connect()`, performs a small SQLite schema read, then times
`close()`. The harness removes each trial directory and records the hash of the
SQLite client source used by the run.

The refreshed local medians were:

| Phase | Median | Min | Samples |
|---|---:|---:|---:|
| SQLAlchemy import | 197.72 ms | 190.28 ms | 5 |
| aiosqlite import | 12.28 ms | 11.70 ms | 5 |
| SQLite client module import | 77.54 ms | 71.21 ms | 5 |
| `SQLiteClient.connect()` | 22.10 ms | 20.20 ms | 5 |
| Schema probe | 2.32 ms | 2.18 ms | 5 |
| `SQLiteClient.close()` | 1.08 ms | 0.96 ms | 5 |

The import timings are cold-process sequential samples. Summing their medians
gives roughly 288 ms of import work before client construction on this
environment, compared with about 22 ms for the measured client connect phase.
That supports treating import deferral as a way to shift public module-import
latency; it does not show a reduction in total work required to start the
SQLite-backed service. Deferring imports into `connect()` would move most of
that cost onto the database-start path, while truly lazy database connection
would also change when initialization and failures occur. This evidence alone
does not justify either source change. Keep SQLAlchemy, aiosqlite, WAL setup,
schema management, and integrity behavior unchanged.

The harness calls `SQLiteClient.connect()` directly. That operation opens the
file and establishes WAL/pragmas. Its subsequent `sqlite_schema` count and
`schema_version` read are only a schema visibility probe; they are not an
Alembic migration or full Engine startup measurement. The database files were
empty (zero schema objects) because migration ownership is outside
`SQLiteClient`. The run used Python 3.13.5 on a shared Windows host, with no
controlled CPU or filesystem cache state. Subprocess wall time includes Python
startup and is reported separately from measured phases.

Reproduce with:

```powershell
$env:PYTHONPATH = 'src'
python scripts/benchmark_sqlite_startup_kernels.py
```

See [machine-readable results](provider_verification/sqlite-startup-kernel.json),
[captured run](provider_verification/sqlite-startup-kernel.log), and the
[benchmark harness](../scripts/benchmark_sqlite_startup_kernels.py).
