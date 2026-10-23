"""Bounded local follow-up profiles for provider startup and workload behavior.

This is an offline diagnostic harness. It uses fixed hash embeddings, fresh local
SQLite/LanceDB files, synthetic records, and no remote calls. Timings are samples,
not service-level objectives or semantic-quality measurements.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import importlib
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIZES = (100, 500, 1000)
ACTIVITIES = ("walking", "painting", "cycling", "cooking")
PLACES = ("Lisbon", "Porto", "Oslo", "Delhi")


def process_rss_bytes() -> int | None:
    """Return the current Windows working set without requiring psutil."""
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
    ok = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    )
    return int(counters.WorkingSetSize) if ok else None


def host_cpu_percent() -> float | None:
    """Sample host CPU use from Windows cumulative kernel/user idle counters."""
    if os.name != "nt":
        return None

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    def read() -> tuple[int, int, int]:
        idle, kernel, user = FileTime(), FileTime(), FileTime()
        ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        combine = lambda value: (value.high << 32) | value.low
        return combine(idle), combine(kernel), combine(user)

    before = read()
    time.sleep(0.25)
    after = read()
    total = (after[1] - before[1]) + (after[2] - before[2])
    return 100.0 * (1 - (after[0] - before[0]) / total) if total else None


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def record(index: int) -> dict[str, str]:
    activity = ACTIVITIES[index % len(ACTIVITIES)]
    place = PLACES[(index // len(ACTIVITIES)) % len(PLACES)]
    return {
        "message_id": f"followup-{index:06d}",
        "role": "user",
        "content": f"Synthetic record {index:06d}: {activity} in {place}.",
        "created_at": f"2026-10-01T00:{(index // 60) % 60:02d}:{index % 60:02d}+00:00",
    }


def host_snapshot() -> dict[str, Any]:
    return {
        "controlled_host": False,
        "cpu_count": os.cpu_count(),
        "cpu_percent_sample": host_cpu_percent(),
        "working_set_bytes": process_rss_bytes(),
        "load_average": getattr(os, "getloadavg", lambda: None)(),
    }


async def component_probe(directory: Path) -> dict[str, Any]:
    """Time imports and client phases in a clean process before importing Engine."""
    result: dict[str, Any] = {"sqlite": {}, "lancedb": {}}
    start = time.perf_counter()
    importlib.import_module("sqlalchemy")
    result["sqlite"]["sqlalchemy_import_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    importlib.import_module("aiosqlite")
    result["sqlite"]["aiosqlite_import_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    sqlite_module = importlib.import_module("memspine.clients.sqlite")
    result["sqlite"]["sqlite_client_module_import_ms"] = (time.perf_counter() - start) * 1000
    SQLiteClient = sqlite_module.SQLiteClient
    SQLiteSpec = sqlite_module.SQLiteSpec

    sqlite_client = SQLiteClient(SQLiteSpec(str(directory / "component.sqlite")))
    start = time.perf_counter()
    await sqlite_client.open()
    result["sqlite"]["connect_and_schema_startup_ms"] = (time.perf_counter() - start) * 1000
    await sqlite_client.close()

    start = time.perf_counter()
    importlib.import_module("lancedb")
    result["lancedb"]["module_import_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    lancedb_client_module = importlib.import_module("memspine.clients.lancedb")
    result["lancedb"]["client_module_import_ms"] = (time.perf_counter() - start) * 1000
    LanceDBClient = lancedb_client_module.LanceDBClient
    LanceDBSpec = lancedb_client_module.LanceDBSpec

    client = LanceDBClient(LanceDBSpec(directory / "component-lance"))
    start = time.perf_counter()
    await client.open()
    result["lancedb"]["connect_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    await asyncio.to_thread(client.db.create_table, "probe", data=[{"id": 1}], mode="overwrite")
    result["lancedb"]["table_create_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    await asyncio.to_thread(client.db.open_table, "probe")
    result["lancedb"]["table_open_ms"] = (time.perf_counter() - start) * 1000
    await client.close()
    return result


async def run_engine(args: argparse.Namespace) -> dict[str, Any]:
    from memspine import Engine
    from memspine.config.schema.root import freeze_effective_config

    config_times: list[float] = []
    first_dump_ms: float | None = None
    repeated_dump_times: list[float] = []

    config_for_engine: dict[str, Any] = {
        "model": {"embedding": {"provider": "hash"}},
        "read": {
            "record_access": False,
            "scoring": {
                "recency_weight": 0.0,
                "importance_weight": 0.0,
                "utility_weight": 0.0,
            },
        },
    }

    async def open_engine(path: Path) -> Any:
        engine = Engine(
            template="core",
            dotenv_path=None,
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(path), "busy_timeout_s": 5.0},
                }
            },
            **config_for_engine,
        )
        start = time.perf_counter()
        await engine.start()
        return engine, (time.perf_counter() - start) * 1000

    corpus_results: list[dict[str, Any]] = []
    concurrency_result: dict[str, Any] = {}
    lifecycle_samples: list[dict[str, float]] = []
    for size in args.sizes:
        db = Path(args.work_dir) / f"corpus-{size}.sqlite"
        engine, start_ms = await open_engine(db)
        if not config_times:
            effective_config = engine._config()
            for _ in range(args.config_repetitions):
                start = time.perf_counter()
                freeze_effective_config(effective_config)
                config_times.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            engine.config.model_dump(mode="json")
            first_dump_ms = (time.perf_counter() - start) * 1000
            for _ in range(args.config_repetitions):
                start = time.perf_counter()
                engine.config.model_dump(mode="json")
                repeated_dump_times.append((time.perf_counter() - start) * 1000)
        before_rss = process_rss_bytes()
        messages = [record(index) for index in range(size)]
        ingest_start = time.perf_counter()
        await engine.ingest_messages(messages, namespace="followup", session_id=f"size-{size}")
        ingest_ms = (time.perf_counter() - ingest_start) * 1000
        stored = await engine.retrieve(namespace="followup")
        stored_contents = {item.content for item in stored}
        expected_contents = {item["content"] for item in messages}
        if stored_contents != expected_contents:
            raise AssertionError(f"corpus integrity failed at size {size}")

        query_latencies: list[float] = []
        recall_hits = 0
        recall_total = 0
        returned_total = 0
        oracle_hits = 0
        # Synthetic labels are exact string membership. Hash-vector rankings do
        # not imply semantic relevance; this is an index/fixture sanity check only.
        for activity in ACTIVITIES:
            relevant = {content for content in expected_contents if f": {activity} " in content}
            found = await engine.search(activity, namespace="followup", top_k=min(20, size))
            result_contents = {item.content for item, _ in found}
            recall_hits += len(result_contents & relevant)
            recall_total += len(relevant)
            returned_total += len(result_contents)
            oracle_hits += min(len(relevant), 20)
        for query_index in range(args.searches):
            start = time.perf_counter()
            await engine.search(
                ACTIVITIES[query_index % len(ACTIVITIES)], namespace="followup", top_k=10
            )
            query_latencies.append((time.perf_counter() - start) * 1000)
        rss_after = process_rss_bytes()
        corpus_results.append(
            {
                "size": size,
                "engine_start_ms": start_ms,
                "ingest_ms": ingest_ms,
                "search_p50_ms": statistics.median(query_latencies),
                "search_p95_ms": percentile(query_latencies, 0.95),
                "search_sample_count": len(query_latencies),
                "percentile_method": "nearest-rank for p95; Python median for p50",
                "synthetic_label_recall_at_20": recall_hits / recall_total
                if recall_total
                else None,
                "synthetic_label_precision_at_20": recall_hits / returned_total
                if returned_total
                else None,
                "synthetic_label_oracle_recall_at_20": oracle_hits / recall_total
                if recall_total
                else None,
                "synthetic_label_relevant_count_across_queries": recall_total,
                "synthetic_label_hits_at_20_across_queries": recall_hits,
                "synthetic_label_candidates_returned_across_queries": returned_total,
                "synthetic_label_recall_definition": "matching activity phrase hits across four queries divided by all matching records across those queries; hash embeddings, not semantic quality",
                "corpus_integrity": True,
                "rss_before_bytes": before_rss,
                "rss_after_bytes": rss_after,
            }
        )

        if size == max(args.sizes):
            # Preload a disjoint batch so its visibility lag can be sampled while
            # concurrent searches run. No fixed acceptance budget is assumed.
            added = [record(size + index) for index in range(args.concurrent_ingest)]
            ingest_done = asyncio.Event()
            ingest_mark: list[float] = []

            async def concurrent_ingest(
                live_engine: Any = engine,
                batch: list[dict[str, str]] = added,
                done: asyncio.Event = ingest_done,
                marked: list[float] = ingest_mark,
            ) -> None:
                await live_engine.ingest_messages(
                    batch, namespace="followup", session_id="concurrent"
                )
                marked.append(time.perf_counter())
                done.set()

            async def concurrent_search(
                live_engine: Any = engine,
                done: asyncio.Event = ingest_done,
                required_searches: int = args.concurrent_searches,
            ) -> list[float]:
                timings: list[float] = []
                while not done.is_set() or len(timings) < required_searches:
                    started = time.perf_counter()
                    await live_engine.search("walking", namespace="followup", top_k=10)
                    timings.append((time.perf_counter() - started) * 1000)
                    if done.is_set() and len(timings) >= required_searches:
                        break
                return timings

            expected_new = {item["content"] for item in added}

            async def poll_visibility(
                live_engine: Any = engine,
                expected: set[str] = expected_new,
                timeout_s: float = args.visibility_timeout_s,
            ) -> tuple[float, int]:
                deadline = time.perf_counter() + timeout_s
                polls = 0
                while time.perf_counter() < deadline:
                    polls += 1
                    all_rows = await live_engine.retrieve(namespace="followup")
                    if expected <= {item.content for item in all_rows}:
                        return time.perf_counter(), polls
                    await asyncio.sleep(0.1)
                raise TimeoutError("concurrent ingest did not become list-visible within the bound")

            concurrent_start = time.perf_counter()
            async with asyncio.timeout(args.concurrent_timeout_s):
                timings, _, (visibility_detected_at, visibility_polls) = await asyncio.gather(
                    concurrent_search(), concurrent_ingest(), poll_visibility()
                )
            ingest_completed_at = ingest_mark[0]
            concurrency_result = {
                "base_corpus_size": size,
                "ingest_records": len(added),
                "search_count": len(timings),
                "elapsed_ms": (time.perf_counter() - concurrent_start) * 1000,
                "search_p50_ms": statistics.median(timings),
                "search_p95_ms": percentile(timings, 0.95),
                "search_sample_count": len(timings),
                "percentile_method": "nearest-rank for p95; Python median for p50",
                "visibility_detection_ms_from_concurrent_start": (
                    visibility_detected_at - concurrent_start
                )
                * 1000,
                "post_ingest_visibility_check_ms": (
                    max(0.0, visibility_detected_at - ingest_completed_at) * 1000
                    if visibility_detected_at >= ingest_completed_at
                    else None
                ),
                "visibility_detected_before_ingest_return": visibility_detected_at
                < ingest_completed_at,
                "visibility_poll_count": visibility_polls,
                "visibility_poll_interval_ms": 100,
                "derivation_lag_measured": False,
                "note": "ingest-to-list visibility is not background derivation lag",
            }
        await engine.stop()

    # Repeated startup/workload/shutdown on a single process records RSS and
    # connection deltas. Each cycle uses a new DB path so files do not accumulate.
    lifecycle_dir = Path(args.work_dir) / "lifecycle"
    lifecycle_dir.mkdir()
    for cycle in range(args.lifecycle_cycles):
        engine, startup_ms = await open_engine(lifecycle_dir / f"cycle-{cycle}.sqlite")
        resource_states = engine._provider_resources.states if engine._provider_resources else {}
        owned_resources = len(resource_states)
        await engine.ingest_messages([record(cycle)], namespace="life", session_id=f"cycle-{cycle}")
        await engine.search("walking", namespace="life", top_k=1)
        rss_live = process_rss_bytes()
        stop_start = time.perf_counter()
        await engine.stop()
        lifecycle_samples.append(
            {
                "startup_ms": startup_ms,
                "stop_ms": (time.perf_counter() - stop_start) * 1000,
                "rss_live_bytes": float(rss_live or 0),
                "rss_after_stop_bytes": float(process_rss_bytes() or 0),
                "owned_resources_owned": float(owned_resources),
                "owned_resources_stopped": float(
                    sum(
                        state.value == "stopped"
                        for state in (
                            engine._provider_resources.states.values()
                            if engine._provider_resources
                            else []
                        )
                    )
                ),
            }
        )
    return {
        "configuration_admission": {
            "effective_config_revalidation_ms_median": statistics.median(config_times),
            "repetitions": len(config_times),
            "path": "Config.effective_config() with template=core and hash embedding",
        },
        "serialization": {
            "first_config_dump_ms": first_dump_ms,
            "subsequent_dump_median_ms": statistics.median(repeated_dump_times),
            "subsequent_dump_repetitions": len(repeated_dump_times),
            "mode": "json",
        },
        "corpora": corpus_results,
        "concurrent_ingest_search": concurrency_result,
        "repeated_lifecycle": {
            "cycles": len(lifecycle_samples),
            "samples": lifecycle_samples,
            "owned_resources_stopped_per_cycle": [
                sample["owned_resources_stopped"] for sample in lifecycle_samples
            ],
            "database_connection_count_available": False,
            "database_connection_count_unavailable_reason": "No OS-level per-provider connection collector is installed in the provided environment.",
            "post_stop_rss_endpoint_delta_bytes": lifecycle_samples[-1]["rss_after_stop_bytes"]
            - lifecycle_samples[0]["rss_after_stop_bytes"]
            if lifecycle_samples
            else 0,
            "rss_delta_interpretation": "difference between first and last post-stop process working-set samples; not leak proof",
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="memspine-provider-followups-") as temporary:
        work_dir = Path(temporary)
        # Components are measured before importing Engine in this process. Import
        # durations still include OS/Python cache state and are reported as such.
        component = asyncio.run(component_probe(work_dir))
        engine_args = argparse.Namespace(**vars(args), work_dir=work_dir)
        engine = asyncio.run(run_engine(engine_args))
    return {
        "schema_version": 1,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            **host_snapshot(),
        },
        "source_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "settings": {
            "sizes": args.sizes,
            "searches_per_size": args.searches,
            "config_repetitions": args.config_repetitions,
            "concurrent_ingest_records": args.concurrent_ingest,
            "concurrent_searches_minimum": args.concurrent_searches,
            "concurrent_timeout_s": args.concurrent_timeout_s,
            "visibility_timeout_s": args.visibility_timeout_s,
            "lifecycle_cycles": args.lifecycle_cycles,
            "embedding": "hash",
            "remote_calls": False,
            "model_downloads": False,
        },
        "component_phases": component,
        "engine_workload": engine,
        "limitations": [
            "Shared host; timings are exploratory and cannot set latency budgets.",
            "Synthetic content and hash embeddings; recall field checks fixture retrieval only, not semantic quality.",
            "SQLite connect metric includes schema startup/migrations; no optimization was made from a single sample.",
            "Remote tail latency and bounded timeout behavior are recorded in the separate remote follow-up report.",
            "Process RSS and connection samples cover only this bounded run, not a representative long-running deployment.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES)
    parser.add_argument("--searches", type=int, default=20)
    parser.add_argument("--config-repetitions", type=int, default=200)
    parser.add_argument("--concurrent-ingest", type=int, default=20)
    parser.add_argument("--concurrent-searches", type=int, default=40)
    parser.add_argument("--lifecycle-cycles", type=int, default=5)
    parser.add_argument("--concurrent-timeout-s", type=float, default=180.0)
    parser.add_argument("--visibility-timeout-s", type=float, default=30.0)
    args = parser.parse_args()
    if any(size < 1 or size > 1000 for size in args.sizes):
        parser.error("corpus sizes must be in 1..1000")
    if not 1 <= args.searches <= 500 or not 1 <= args.config_repetitions <= 1000:
        parser.error("searches must be in 1..500 and config repetitions in 1..1000")
    if not 1 <= args.concurrent_ingest <= 100 or not 1 <= args.concurrent_searches <= 500:
        parser.error("concurrent ingest must be in 1..100 and searches in 1..500")
    if not 1 <= args.lifecycle_cycles <= 50:
        parser.error("lifecycle cycles must be in 1..50")
    if not 1 <= args.concurrent_timeout_s <= 600 or not 1 <= args.visibility_timeout_s <= 120:
        parser.error("timeouts must be concurrent 1..600 seconds and visibility 1..120 seconds")
    report = run(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"engine_workload"}},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
