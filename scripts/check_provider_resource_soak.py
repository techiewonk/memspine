"""Run a bounded fresh-process SQLite/provider lifecycle resource soak.

The post-warmup RSS spread is a diagnostic guard only, not a production SLO
or evidence of unbounded leak freedom. SQLAlchemy connection events are scoped
to this worker process and removed before it exits.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WARMUP_CYCLES = 5
MEASURED_CYCLES = 30
MAX_WALL_TIME_S = 180
POST_WARMUP_RSS_SPREAD_GUARD_BYTES = 100 * 1024 * 1024
MESSAGES_PER_CYCLE = 2
SEARCHES_PER_CYCLE = 2


def process_working_set_bytes() -> int | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "PagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "NonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        return None
    return int(counters.WorkingSetSize)


def process_handle_count() -> int | None:
    if os.name != "nt":
        return None
    count = ctypes.c_ulong()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        return None
    return int(count.value)


def percentile_summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)

    def nearest_rank(percent: int) -> float | None:
        if not ordered:
            return None
        rank = max(1, (percent * len(ordered) + 99) // 100)
        return ordered[rank - 1]

    return {
        "sample_count": len(ordered),
        "estimator": "nearest-rank (ceil(p*n), one-based)",
        "p50": nearest_rank(50),
        "p95": nearest_rank(95),
        "p99": nearest_rank(99),
    }


def connection_event_probe() -> tuple[Any, set[int], dict[str, int]]:
    """Install process-local SQLAlchemy Engine events for actual DBAPI lifetimes."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine as SyncEngine

    open_dbapi: set[int] = set()
    totals = {"connect_events": 0, "close_events": 0, "connect_events_without_close": 0}

    def connected(_dbapi_connection: Any, connection_record: Any) -> None:
        identity = id(_dbapi_connection)
        if identity in open_dbapi:
            totals["connect_events_without_close"] += 1
        open_dbapi.add(identity)
        connection_record.info["provider_soak_dbapi_identity"] = identity
        totals["connect_events"] += 1

    def closed(dbapi_connection: Any, connection_record: Any) -> None:
        identity = connection_record.info.pop("provider_soak_dbapi_identity", id(dbapi_connection))
        open_dbapi.discard(identity)
        totals["close_events"] += 1

    event.listen(SyncEngine, "connect", connected)
    event.listen(SyncEngine, "close", closed)
    return (event, SyncEngine, connected, closed), open_dbapi, totals


async def run_cycles(
    work_dir: Path, open_dbapi: set[int], event_totals: dict[str, int]
) -> dict[str, Any]:
    from memspine import Engine

    samples: list[dict[str, Any]] = []
    process_start_rss = process_working_set_bytes()
    process_start_handles = process_handle_count()
    run_started = time.perf_counter()
    total_cycles = WARMUP_CYCLES + MEASURED_CYCLES

    for cycle in range(total_cycles):
        if time.perf_counter() - run_started >= MAX_WALL_TIME_S:
            break
        warmup = cycle < WARMUP_CYCLES
        measured_index = cycle - WARMUP_CYCLES if not warmup else None
        gc.collect()
        rss_before = process_working_set_bytes()
        handles_before = process_handle_count()
        engine: Engine | None = None
        start_ms: float | None = None
        ingest_ms: float | None = None
        search_ms: list[float] = []
        failure: str | None = None
        owned_count = 0
        stop_ms: float | None = None
        rss_live: int | None = None
        handles_live: int | None = None
        open_dbapi_after_start: int | None = None
        open_dbapi_after_workload: int | None = None
        open_dbapi_after_stop: int | None = None
        stopped_owned_resources = 0
        try:
            database = work_dir / f"cycle-{cycle:02d}.sqlite"
            engine = Engine(
                template="core",
                dotenv_path=None,
                storage={
                    "provider": "sqlite",
                    "settings": {"path": str(database), "busy_timeout_s": 5.0},
                },
                embedding={"provider": "hash", "settings": {"dim": 64}},
                read={
                    "lexical_provider": "tantivy",
                    "lexical_settings": {},
                    "record_access": False,
                    "scoring": {
                        "recency_weight": 0.0,
                        "importance_weight": 0.0,
                        "utility_weight": 0.0,
                    },
                },
            )
            started = time.perf_counter()
            await engine.start()
            start_ms = (time.perf_counter() - started) * 1000
            open_dbapi_after_start = len(open_dbapi)
            owned_states = engine._provider_resources.states if engine._provider_resources else {}
            owned_count = len(owned_states)

            messages = [
                {
                    "message_id": f"soak-{cycle:02d}-{index}",
                    "role": "user",
                    "content": f"Soak cycle {cycle:02d} message {index}: prefers walking and tea in Lisbon.",
                    "created_at": f"2026-10-17T10:{cycle:02d}:{index:02d}+00:00",
                }
                for index in range(MESSAGES_PER_CYCLE)
            ]
            started = time.perf_counter()
            await engine.ingest_messages(
                messages, namespace="provider-soak", session_id=f"cycle-{cycle:02d}"
            )
            ingest_ms = (time.perf_counter() - started) * 1000
            open_dbapi_after_workload = len(open_dbapi)

            for query in ("walking in Lisbon", "tea preference"):
                started = time.perf_counter()
                await engine.search(query, namespace="provider-soak", top_k=5)
                search_ms.append((time.perf_counter() - started) * 1000)
            rss_live = process_working_set_bytes()
            handles_live = process_handle_count()
        except Exception as exc:
            failure = type(exc).__name__
        finally:
            if engine is not None:
                stopped = time.perf_counter()
                try:
                    await engine.stop()
                except Exception as exc:
                    failure = failure or f"stop:{type(exc).__name__}"
                stop_ms = (time.perf_counter() - stopped) * 1000
                manager = engine._provider_resources
                states_after = manager.states if manager else {}
                stopped_owned_resources = sum(
                    state.value == "stopped" for state in states_after.values()
                )
                open_dbapi_after_stop = len(open_dbapi)
            gc.collect()

        rss_after_stop = process_working_set_bytes()
        handles_after_stop = process_handle_count()
        samples.append(
            {
                "cycle": cycle,
                "phase": "warmup" if warmup else "measured",
                "measured_index": measured_index,
                "startup_ms": start_ms,
                "ingest_ms": ingest_ms,
                "search_ms": search_ms,
                "search_sample_count": len(search_ms),
                "stop_ms": stop_ms,
                "rss_before_bytes": rss_before,
                "rss_live_bytes": rss_live,
                "rss_after_stop_bytes": rss_after_stop,
                "handles_before": handles_before,
                "handles_live": handles_live,
                "handles_after_stop": handles_after_stop,
                "open_dbapi_connections_after_start": open_dbapi_after_start,
                "open_dbapi_connections_after_workload": open_dbapi_after_workload,
                "open_dbapi_connections_after_stop": open_dbapi_after_stop,
                "owned_resources": owned_count,
                "owned_resources_stopped": stopped_owned_resources,
                "failure_type": failure,
            }
        )
        if failure:
            break
        if time.perf_counter() - run_started >= MAX_WALL_TIME_S:
            break

    measured = [sample for sample in samples if sample["phase"] == "measured"]
    rss_values = [
        sample["rss_after_stop_bytes"]
        for sample in measured
        if sample["rss_after_stop_bytes"] is not None
    ]
    rss_spread = max(rss_values) - min(rss_values) if rss_values else None
    stop_connection_counts = [sample["open_dbapi_connections_after_stop"] for sample in samples]
    completed_connects = event_totals["connect_events"]
    completed_closes = event_totals["close_events"]
    measured_search_samples = [latency for sample in measured for latency in sample["search_ms"]]
    return {
        "worker_process_id": os.getpid(),
        "host": platform.platform(),
        "python": platform.python_version(),
        "process_start_rss_bytes": process_start_rss,
        "process_start_handles": process_start_handles,
        "warmup_cycles_requested": WARMUP_CYCLES,
        "measured_cycles_requested": MEASURED_CYCLES,
        "warmup_cycles_completed": sum(sample["phase"] == "warmup" for sample in samples),
        "measured_cycles_completed": len(measured),
        "messages_per_cycle": MESSAGES_PER_CYCLE,
        "searches_per_cycle": SEARCHES_PER_CYCLE,
        "wall_time_limit_s": MAX_WALL_TIME_S,
        "wall_time_s": time.perf_counter() - run_started,
        "post_warmup_rss_spread_guard_bytes": POST_WARMUP_RSS_SPREAD_GUARD_BYTES,
        "post_warmup_rss_values_bytes": rss_values,
        "post_warmup_rss_spread_bytes": rss_spread,
        "post_warmup_rss_sample_count": len(rss_values),
        "post_warmup_rss_guard_exceeded": (
            rss_spread > POST_WARMUP_RSS_SPREAD_GUARD_BYTES if rss_spread is not None else None
        ),
        "post_warmup_rss_guard_interpretation": "diagnostic only; not a production SLO and cannot prove unbounded leak freedom",
        "all_cycles_had_zero_open_dbapi_connections_after_stop": all(
            count == 0 for count in stop_connection_counts if count is not None
        )
        and len(stop_connection_counts) == len(samples),
        "pool_event_observation_available": completed_connects > 0,
        "pool_connect_events": completed_connects,
        "pool_close_events": completed_closes,
        "pool_event_counts_balanced": completed_connects == completed_closes,
        "all_cycles_stopped_owned_resources": all(
            sample["owned_resources"] == sample["owned_resources_stopped"] for sample in samples
        ),
        "sqlalchemy_pool_events": event_totals,
        "measured_latency_summary_ms": {
            "startup": percentile_summary(
                [sample["startup_ms"] for sample in measured if sample["startup_ms"] is not None]
            ),
            "ingest": percentile_summary(
                [sample["ingest_ms"] for sample in measured if sample["ingest_ms"] is not None]
            ),
            "search": percentile_summary(measured_search_samples),
            "stop": percentile_summary(
                [sample["stop_ms"] for sample in measured if sample["stop_ms"] is not None]
            ),
        },
        "measured_post_stop_handles": [sample["handles_after_stop"] for sample in measured],
        "open_dbapi_connections_at_end": len(open_dbapi),
        "process_end_rss_bytes": process_working_set_bytes(),
        "process_end_handles": process_handle_count(),
        "samples": samples,
    }


def worker(out: Path) -> int:
    print(
        json.dumps(
            {
                "declared_before_run": {
                    "warmup_cycles": WARMUP_CYCLES,
                    "measured_cycles": MEASURED_CYCLES,
                    "post_warmup_rss_spread_guard_bytes": POST_WARMUP_RSS_SPREAD_GUARD_BYTES,
                    "guard_is_diagnostic_not_slo": True,
                    "wall_time_limit_s": MAX_WALL_TIME_S,
                }
            }
        ),
        flush=True,
    )
    listeners, open_dbapi, event_totals = connection_event_probe()
    report: dict[str, Any]
    try:
        with tempfile.TemporaryDirectory(prefix="memspine-provider-soak-") as directory:
            report = asyncio.run(run_cycles(Path(directory), open_dbapi, event_totals))
    except Exception as exc:
        report = {
            "fatal_error_type": type(exc).__name__,
            "open_dbapi_connections_at_end": len(open_dbapi),
            "sqlalchemy_pool_events": event_totals,
        }
    finally:
        event, engine_type, connected, closed = listeners
        event.remove(engine_type, "connect", connected)
        event.remove(engine_type, "close", closed)
    report["schema_version"] = 1
    report["source_revision"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    report["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    complete = (
        report.get("measured_cycles_completed") == MEASURED_CYCLES
        and report.get("all_cycles_had_zero_open_dbapi_connections_after_stop") is True
        and report.get("all_cycles_stopped_owned_resources") is True
        and report.get("open_dbapi_connections_at_end") == 0
        and report.get("pool_event_observation_available") is True
        and report.get("pool_event_counts_balanced") is True
        and report.get("fatal_error_type") is None
    )
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if complete else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return worker(args.out)

    child_env = os.environ.copy()
    child_env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--out",
        str(args.out.resolve()),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=MAX_WALL_TIME_S,
            check=False,
        )
        out = args.out.with_suffix(".log")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(completed.stdout or "", encoding="utf-8")
        return completed.returncode
    except subprocess.TimeoutExpired as exc:
        log_path = args.out.with_suffix(".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        captured = exc.stdout or b""
        log_path.write_bytes(captured if isinstance(captured, bytes) else captured.encode())
        args.out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "timed_out": True,
                    "wall_time_limit_s": MAX_WALL_TIME_S,
                    "post_warmup_rss_spread_guard_bytes": POST_WARMUP_RSS_SPREAD_GUARD_BYTES,
                    "post_warmup_rss_guard_interpretation": "diagnostic only; not a production SLO and cannot prove unbounded leak freedom",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
