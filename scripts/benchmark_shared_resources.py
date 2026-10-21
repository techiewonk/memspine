"""RM-80: evidence for ADR-079 shared-resource defaults (scope, warm-up, pools).

Each scenario runs in a fresh process.

``--mode multi`` (default): N Engines in one process with identical model settings and
separate stores, measuring start time, first-request latency (lazy loads), steady search
latency, RSS, threads and OS handles.

``--mode concurrent``: one Engine with K concurrent ingest+search workers for a fixed
duration, reporting throughput, p50/p95 per operation, errors and (Postgres) server-side
connection count.

Uses the locally cached fastembed model (no download; fails if absent) or the hash
embedder. Postgres runs use a throwaway database per scenario, dropped afterwards.

    python scripts/benchmark_shared_resources.py --engines 1 2 4 --out result.json
    python scripts/benchmark_shared_resources.py --overrides '{"runtime": {...}}' ...
    python scripts/benchmark_shared_resources.py --mode concurrent --workers 8 \\
        --storage postgres --pg-url postgresql://u:p@127.0.0.1:55432/postgres
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        out[key] = (
            _deep_merge(out[key], value)
            if isinstance(value, dict) and isinstance(out.get(key), dict)
            else value
        )
    return out


def _pg_admin(url: str, sql: str) -> Any:
    import psycopg

    plain = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(plain, autocommit=True) as conn:
        cursor = conn.execute(sql)
        return cursor.fetchall() if sql.lstrip().upper().startswith("SELECT") else None


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return json.loads(args.overrides) if args.overrides else {}


def _engine_kwargs(
    args: argparse.Namespace, work: Path, index: int, database: str | None
) -> dict[str, Any]:
    embedding = (
        {"provider": "fastembed", "settings": {"model": args.model}}
        if args.embedder == "fastembed"
        else {"provider": "hash"}
    )
    if args.storage == "postgres":
        base_url = args.pg_url.rsplit("/", 1)[0]
        storage: dict[str, Any] = {
            "provider": "postgres",
            "settings": {
                "url": f"{base_url}/{database}",
                "data_dir": str(work / f"pg{index}"),
                "connect_timeout_s": 5.0,
                "statement_timeout_s": 30.0,
            },
        }
    else:
        storage = {
            "provider": "sqlite",
            "settings": {"path": str(work / f"e{index}.db"), "busy_timeout_s": 5.0},
        }
    base: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": storage,
        "embedding": embedding,
    }
    if args.storage == "postgres":
        base["runtime"] = {"deployment_id": "rm80-bench"}  # required by shared Postgres
    return _deep_merge(base, _overrides(args))


def _pct(values: list[float], q: float) -> float | None:
    return sorted(values)[max(0, int(len(values) * q) - 1)] if values else None


async def concurrent(args: argparse.Namespace, work: Path, database: str | None) -> dict[str, Any]:
    from memspine import Engine

    eng = Engine(**_engine_kwargs(args, work, 0, database))
    await eng.start()
    ingest_ms: list[float] = []
    search_ms: list[float] = []
    errors: list[str] = []
    try:
        warm = {"message_id": "warm", "role": "user", "content": "Asha cycles in Lisbon."}
        await eng.ingest_messages([warm], namespace="w0", session_id="s")
        await eng.search("cycling Lisbon", namespace="w0", top_k=3)  # excludes lazy loads
        deadline = time.perf_counter() + args.duration
        counter = 0

        async def worker(wid: int) -> None:
            nonlocal counter
            ns = f"w{wid}"
            while time.perf_counter() < deadline:
                counter += 1
                n = counter
                message = {
                    "message_id": f"c{n}",
                    "role": "user",
                    "content": f"Person {n} likes tea.",
                }
                try:
                    t = time.perf_counter()
                    await eng.ingest_messages([message], namespace=ns, session_id="s")
                    ingest_ms.append((time.perf_counter() - t) * 1000)
                    t = time.perf_counter()
                    await eng.search("likes tea", namespace=ns, top_k=5)
                    search_ms.append((time.perf_counter() - t) * 1000)
                except Exception as exc:  # recorded, not raised: errors are a metric
                    errors.append(type(exc).__name__)

        wall = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(args.workers)))
        wall = time.perf_counter() - wall
        connections = None
        if args.storage == "postgres":
            query = f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{database}'"
            connections = _pg_admin(args.pg_url, query)[0][0]
        return {
            "mode": "concurrent",
            "storage": args.storage,
            "workers": args.workers,
            "overrides": _overrides(args),
            "ops_per_s": (len(ingest_ms) + len(search_ms)) / wall,
            "ingest_p50_ms": _pct(ingest_ms, 0.5),
            "ingest_p95_ms": _pct(ingest_ms, 0.95),
            "search_p50_ms": _pct(search_ms, 0.5),
            "search_p95_ms": _pct(search_ms, 0.95),
            "errors": len(errors),
            "error_types": sorted(set(errors)),
            "server_connections": connections,
        }
    finally:
        await eng.stop()


async def multi(args: argparse.Namespace, work: Path, database: str | None) -> dict[str, Any]:
    import psutil

    from memspine import Engine

    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    engines, start_ms = [], []
    for index in range(args.engines):
        kwargs = _engine_kwargs(args, work, index, database)
        t = time.perf_counter()
        eng = Engine(**kwargs)
        await eng.start()
        start_ms.append((time.perf_counter() - t) * 1000)
        engines.append(eng)
    rss_started = proc.memory_info().rss
    if args.settle_s:
        await asyncio.sleep(args.settle_s)  # lets background warm-up run, when enabled
    first_ms, steady_ms = [], []
    try:
        for index, eng in enumerate(engines):
            ns = f"n{index}"
            message = {
                "message_id": f"m{index}",
                "role": "user",
                "content": "Asha cycles in Lisbon.",
            }
            t = time.perf_counter()
            await eng.ingest_messages([message], namespace=ns, session_id="s")
            await eng.search("cycling Lisbon", namespace=ns, top_k=3)
            first_ms.append((time.perf_counter() - t) * 1000)
            for _ in range(args.queries):
                t = time.perf_counter()
                await eng.search("cycling Lisbon", namespace=ns, top_k=3)
                steady_ms.append((time.perf_counter() - t) * 1000)
        mem = proc.memory_info()
        handles = proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
        return {
            "mode": "multi",
            "engines": args.engines,
            "embedder": args.embedder,
            "storage": args.storage,
            "overrides": _overrides(args),
            "start_ms": start_ms,
            "first_request_ms": first_ms,
            "steady_search_p50_ms": statistics.median(steady_ms) if steady_ms else None,
            "rss_delta_started_mb": (rss_started - rss0) / 2**20,
            "rss_delta_end_mb": (mem.rss - rss0) / 2**20,
            "peak_mb": getattr(mem, "peak_wset", mem.rss) / 2**20,
            "threads": proc.num_threads(),
            "os_handles": handles,
        }
    finally:
        for eng in engines:
            await eng.stop()


async def scenario(args: argparse.Namespace) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix="rm80-"))
    database = None
    if args.storage == "postgres":
        if not args.pg_url:
            raise SystemExit("--pg-url (or MEMSPINE_BENCH_PG_URL) is required for postgres")
        database = f"rm80_{uuid.uuid4().hex[:12]}"
        _pg_admin(args.pg_url, f"CREATE DATABASE {database}")
    try:
        if args.mode == "concurrent":
            return await concurrent(args, work, database)
        return await multi(args, work, database)
    finally:
        if database is not None:
            _pg_admin(args.pg_url, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")


def _summary(row: dict[str, Any], label: str) -> str:
    if row["mode"] == "concurrent":
        return (
            f"{label} ops/s={row['ops_per_s']:.1f} ingest p95={row['ingest_p95_ms']:.0f} "
            f"search p95={row['search_p95_ms']:.0f} errors={row['errors']} "
            f"conns={row['server_connections']}"
        )
    return (
        f"{label} start={[round(x) for x in row['start_ms']]} "
        f"first={[round(x) for x in row['first_request_ms']]} "
        f"rss_end={row['rss_delta_end_mb']:.0f}MB handles={row['os_handles']} "
        f"threads={row['threads']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("multi", "concurrent"), default="multi")
    parser.add_argument("--storage", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--pg-url", default=os.environ.get("MEMSPINE_BENCH_PG_URL", ""))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--engines", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--embedder", choices=("fastembed", "hash"), default="fastembed")
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--settle-s", type=float, default=0.0)
    parser.add_argument("--overrides", default="")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.engines = args.engines[0]
        # psycopg async cannot run on Windows' default Proactor loop (same as the test hook).
        factory = (
            asyncio.SelectorEventLoop
            if args.storage == "postgres" and sys.platform == "win32"
            else None
        )
        print(json.dumps(asyncio.run(scenario(args), loop_factory=factory)))
        return 0
    counts = [1] if args.mode == "concurrent" else args.engines
    runs: list[dict[str, Any]] = []
    for count in counts:
        for trial in range(args.trials):
            cmd = [
                sys.executable, __file__, "--child",
                "--engines", str(count), "--embedder", args.embedder, "--model", args.model,
                "--queries", str(args.queries), "--settle-s", str(args.settle_s),
                "--mode", args.mode, "--storage", args.storage, "--pg-url", args.pg_url,
                "--workers", str(args.workers), "--duration", str(args.duration),
            ]  # fmt: skip
            if args.overrides:
                cmd += ["--overrides", args.overrides]
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            done = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            if done.returncode:
                raise SystemExit(f"scenario failed (count={count}):\n{done.stderr[-4000:]}")
            row = json.loads(done.stdout.strip().splitlines()[-1])
            row["trial"] = trial
            runs.append(row)
            print(_summary(row, f"count={count} trial={trial}"), flush=True)
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    ).stdout.strip()
    report = {
        "label": args.label,
        "revision": revision,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "runs": runs,
    }
    if args.out:
        args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
