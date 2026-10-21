#!/usr/bin/env python
"""Measure cold-process SQLite imports and standalone client startup locally."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRIALS = 5
MAX_RUN_SECONDS = 60.0


def _finite_ms(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a numeric duration")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return result


def _worker(database_path: Path) -> dict[str, Any]:
    """Measure imports sequentially, then connect/probe/close one client."""
    import importlib

    samples: dict[str, float] = {}
    for label, module_name in (
        ("sqlalchemy_import_ms", "sqlalchemy"),
        ("aiosqlite_import_ms", "aiosqlite"),
        ("sqlite_client_module_import_ms", "memspine.clients.sqlite"),
    ):
        started = time.perf_counter_ns()
        module = importlib.import_module(module_name)
        samples[label] = (time.perf_counter_ns() - started) / 1_000_000

    SQLiteClient = module.SQLiteClient

    async def lifecycle() -> tuple[float, float, float, int]:
        client = SQLiteClient(database_path)
        started = time.perf_counter_ns()
        await client.connect()
        connect_ms = (time.perf_counter_ns() - started) / 1_000_000

        started = time.perf_counter_ns()
        async with client.engine.connect() as connection:
            result = await connection.exec_driver_sql("SELECT count(*) FROM sqlite_schema")
            schema_object_count = int(result.scalar_one())
            await connection.exec_driver_sql("PRAGMA schema_version")
        schema_probe_ms = (time.perf_counter_ns() - started) / 1_000_000

        started = time.perf_counter_ns()
        await client.close()
        close_ms = (time.perf_counter_ns() - started) / 1_000_000
        return connect_ms, schema_probe_ms, close_ms, schema_object_count

    connect_ms, schema_probe_ms, close_ms, schema_object_count = asyncio.run(lifecycle())
    samples.update(
        {
            "client_connect_ms": connect_ms,
            "schema_probe_ms": schema_probe_ms,
            "client_close_ms": close_ms,
        }
    )
    return {
        "samples_ms": samples,
        "database_file_created": database_path.is_file(),
        "database_file_bytes": database_path.stat().st_size,
        "schema_object_count": schema_object_count,
    }


def _worker_entry(database_path: Path) -> None:
    print(json.dumps(_worker(database_path), sort_keys=True, allow_nan=False))


def _source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run() -> dict[str, Any]:
    started = time.monotonic()
    script_path = Path(__file__).resolve()
    source_path = ROOT / "src/memspine/clients/sqlite.py"
    samples_by_phase: dict[str, list[float]] = {}
    trial_reports: list[dict[str, Any]] = []
    subprocess_wall_ms: list[float] = []

    env = os.environ.copy()
    source_root = str(ROOT / "src")
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")

    for trial in range(TRIALS):
        remaining = MAX_RUN_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("SQLite startup benchmark exceeded 60 seconds")
        with tempfile.TemporaryDirectory(prefix=f"memspine-sqlite-startup-{trial}-") as temp_dir:
            database_path = Path(temp_dir) / "startup.sqlite"
            command = [sys.executable, str(script_path), "--worker", str(database_path)]
            child_started = time.perf_counter_ns()
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=max(1.0, remaining),
            )
            subprocess_wall_ms.append((time.perf_counter_ns() - child_started) / 1_000_000)
            try:
                report = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"worker did not return JSON: {completed.stdout!r}") from exc
            if not report.get("database_file_created"):
                raise AssertionError(f"trial {trial} did not create its temporary database")
            if not isinstance(report.get("schema_object_count"), int):
                raise AssertionError(f"trial {trial} did not return a schema object count")
            for phase, value in report["samples_ms"].items():
                samples_by_phase.setdefault(phase, []).append(_finite_ms(value, phase))
            trial_reports.append(
                {
                    "trial": trial + 1,
                    "samples_ms": report["samples_ms"],
                    "database_file_bytes": report["database_file_bytes"],
                    "schema_object_count": report["schema_object_count"],
                    "temporary_directory_removed_after_trial": True,
                }
            )

    summaries = {
        phase: {
            "count": len(values),
            "min_ms": round(min(values), 3),
            "median_ms": round(statistics.median(values), 3),
            "samples_ms": [round(value, 3) for value in values],
        }
        for phase, values in sorted(samples_by_phase.items())
    }
    if any(summary["count"] != TRIALS for summary in summaries.values()):
        raise AssertionError("every phase must have exactly five finite samples")
    return {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "controlled_host": False,
        },
        "method": {
            "trials": TRIALS,
            "process_model": "one fresh Python subprocess per trial",
            "imports": ["sqlalchemy", "aiosqlite", "memspine.clients.sqlite"],
            "import_order": "sequential; dependencies remain imported in later phases",
            "client_path": "one file-backed SQLiteClient per trial",
            "schema_probe": "SELECT count(*) FROM sqlite_schema; PRAGMA schema_version",
            "scope": "standalone SQLiteClient connect, schema read, and close; no Engine/Alembic migration",
            "per_trial_temporary_directory": True,
            "total_wall_clock_ms_including_subprocesses": round(
                (time.monotonic() - started) * 1000, 3
            ),
        },
        "source": {
            "path": "src/memspine/clients/sqlite.py",
            "sha256": _source_sha256(source_path),
        },
        "subprocess_wall_ms_including_python_startup": {
            "count": len(subprocess_wall_ms),
            "min_ms": round(min(subprocess_wall_ms), 3),
            "median_ms": round(statistics.median(subprocess_wall_ms), 3),
            "samples_ms": [round(value, 3) for value in subprocess_wall_ms],
        },
        "phases": summaries,
        "trials": trial_reports,
        "limitations": [
            "Host load and filesystem cache state were not controlled; five fresh processes do not isolate OS page cache.",
            "Import phase times are sequential and cold-process, not independent concurrent imports.",
            "SQLiteClient.connect establishes its engine and WAL/pragma connection; the schema probe is a read, not Alembic migration timing.",
            "Deferring imports could shift work to first use but would not remove the SQLAlchemy/aiosqlite requirement or necessarily reduce DB service startup cost.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker is not None:
        _worker_entry(args.worker)
        return
    result = run()
    output_path = ROOT / "docs/provider_verification/sqlite-startup-kernel.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    output_path.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
