"""Compare isolated offline provider workloads against an explicit source baseline.

Uses SQLite, LanceDB, Tantivy and hash embeddings; no model download or remote API.
Each trial is a fresh process and database. Variants alternate order across trials.
This measures local framework overhead, not production model/backend throughput.
"""

from __future__ import annotations

import argparse
import asyncio
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


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_fingerprint(path: Path) -> str:
    result = hashlib.sha256()
    for source in sorted(path.rglob("*.py")):
        if "__pycache__" not in source.parts:
            result.update(source.relative_to(path).as_posix().encode())
            result.update(source.read_bytes())
    return result.hexdigest()


async def workload(args: argparse.Namespace) -> dict[str, Any]:
    import psutil

    psutil.cpu_percent(interval=None)
    started = time.perf_counter()
    import memspine
    from memspine import Engine

    import_ms = (time.perf_counter() - started) * 1000
    if not Path(memspine.__file__).resolve().is_relative_to(Path(args.worker_src).resolve()):
        raise RuntimeError("worker imported a different source tree")
    database = str(Path(args.work_dir) / "memory.db")
    messages = [
        {
            "message_id": f"bench-{index:04d}",
            "role": "user",
            "content": f"Person {index:04d} likes {('walking', 'painting', 'cycling', 'cooking')[index % 4]} in {('Lisbon', 'Porto', 'Oslo')[index % 3]}.",
            "created_at": f"2026-10-01T10:{index // 60:02d}:{index % 60:02d}+00:00",
        }
        for index in range(args.messages)
    ]
    storage = (
        {"provider": "sqlite", "settings": {"path": database, "busy_timeout_s": 5.0}}
        if args.variant == "current"
        else {"path": database}
    )

    def engine() -> Any:
        return Engine(
            template="core", dotenv_path=None, storage=storage,
            embedding={"provider": "hash"},
            read={"record_access": False, "scoring": {
                "recency_weight": 0.0, "importance_weight": 0.0, "utility_weight": 0.0,
            }},
        )

    started = time.perf_counter()
    eng = engine()
    await eng.start()
    cold_start_ms = (time.perf_counter() - started) * 1000
    try:
        started = time.perf_counter()
        eng.config.model_dump(mode="json")
        first_config_dump_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        await eng.ingest_messages(messages, namespace="bench", session_id="bench-session")
        ingest_ms = (time.perf_counter() - started) * 1000
        expected_contents = sorted(message["content"] for message in messages)
        stored = sorted(record.content for record in await eng.retrieve(namespace="bench"))
        if stored != expected_contents:
            raise AssertionError("ingest/listing lost or changed benchmark messages")
        queries = ["walking in Lisbon", "painting in Porto", "cycling in Oslo", "cooking"]
        for query in queries:
            await eng.search(query, namespace="bench", top_k=8)
        latencies, outputs = [], []
        for index in range(args.queries):
            started = time.perf_counter()
            found = await eng.search(queries[index % len(queries)], namespace="bench", top_k=8)
            latencies.append((time.perf_counter() - started) * 1000)
            outputs.append([record.content for record, _ in found])
        usage = eng.model_usage()
        if usage:
            raise AssertionError("offline provider benchmark unexpectedly used an LLM")
    finally:
        started = time.perf_counter()
        await eng.stop()
        stop_ms = (time.perf_counter() - started) * 1000
    warm_starts = []
    for _ in range(5):
        started = time.perf_counter()
        reopened = engine()
        await reopened.start()
        warm_starts.append((time.perf_counter() - started) * 1000)
        await reopened.stop()
    memory = psutil.Process().memory_info()
    return {
        "variant": args.variant, "input_hash": digest(messages),
        "source_file": str(Path(memspine.__file__).resolve()),
        "import_ms": import_ms, "cold_start_ms": cold_start_ms,
        "first_config_dump_ms": first_config_dump_ms,
        "warm_start_ms": warm_starts, "ingest_ms": ingest_ms,
        "search_ms": latencies, "stop_ms": stop_ms,
        "peak_rss_bytes": getattr(memory, "peak_wset", memory.rss),
        "peak_memory_method": "Windows peak working set" if hasattr(memory, "peak_wset") else "end-of-run RSS (not peak)",
        "output_hash": digest(outputs), "stored_hash": digest(stored),
        "record_count": len(stored), "llm_usage": usage,
        "host_cpu_percent": psutil.cpu_percent(interval=None),
    }


def summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    search = [value for sample in samples for value in sample["search_ms"]]
    warm = [value for sample in samples for value in sample["warm_start_ms"]]
    return {
        "import_median_ms": statistics.median(sample["import_ms"] for sample in samples),
        "process_ready_median_ms": statistics.median(
            sample["import_ms"] + sample["cold_start_ms"] for sample in samples
        ),
        "cold_start_median_ms": statistics.median(sample["cold_start_ms"] for sample in samples),
        "first_config_dump_median_ms": statistics.median(
            sample["first_config_dump_ms"] for sample in samples
        ),
        "warm_start_median_ms": statistics.median(warm),
        "warm_start_p95_ms": percentile(warm, 0.95),
        "ingest_median_ms": statistics.median(sample["ingest_ms"] for sample in samples),
        "search_p50_ms": statistics.median(search), "search_p95_ms": percentile(search, 0.95),
        "stop_median_ms": statistics.median(sample["stop_ms"] for sample in samples),
        "peak_rss_median_bytes": statistics.median(sample["peak_rss_bytes"] for sample in samples),
    }


def run(args: argparse.Namespace) -> int:
    variants = {"current": ROOT / "src"}
    if args.baseline_src:
        variants["baseline"] = args.baseline_src.resolve()
    fingerprints = {variant: source_fingerprint(source) for variant, source in variants.items()}
    samples: dict[str, list[dict[str, Any]]] = {variant: [] for variant in variants}
    with tempfile.TemporaryDirectory(prefix="memspine-provider-benchmark-") as temporary:
        for trial in range(args.trials):
            order = list(variants) if trial % 2 == 0 else list(reversed(variants))
            for variant in order:
                work_dir = Path(temporary) / f"{trial}-{variant}"
                work_dir.mkdir()
                result = work_dir / "result.json"
                env = {**os.environ, "PYTHONPATH": str(variants[variant]),
                       "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
                command = [sys.executable, str(Path(__file__).resolve()), "--worker-src", str(variants[variant]),
                           "--variant", variant, "--work-dir", str(work_dir), "--out", str(result),
                           "--messages", str(args.messages), "--queries", str(args.queries)]
                completed = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=180)
                if completed.returncode:
                    raise RuntimeError(f"{variant} benchmark failed:\n{completed.stderr[-6000:]}")
                samples[variant].append(json.loads(result.read_text(encoding="utf-8")))
                print(f"trial {trial + 1}/{args.trials}: {variant} complete", flush=True)
    report: dict[str, Any] = {
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "variant_order_alternated": True},
        "source_fingerprints": fingerprints,
        "workload": {"messages": args.messages, "queries_per_trial": args.queries, "trials": args.trials,
                     "providers": ["sqlite", "lancedb", "tantivy", "hash"], "remote_calls": False},
        "samples": samples, "summary": {key: summary(value) for key, value in samples.items()},
    }
    hashes = {(sample["input_hash"], sample["stored_hash"], sample["output_hash"]) for rows in samples.values() for sample in rows}
    report["behaviour_matches"] = len(hashes) == 1
    report["source_stable"] = all(
        source_fingerprint(source) == fingerprints[variant] for variant, source in variants.items()
    )
    report["threshold_is_investigation_not_production_guarantee"] = True
    if "baseline" in samples:
        report["paired_trial_change_percent"] = [
            {
                key: (summary([current])[key] / summary([baseline])[key] - 1) * 100
                for key in report["summary"]["current"]
            }
            for current, baseline in zip(samples["current"], samples["baseline"], strict=True)
        ]
        report["change_percent"] = {
            key: (report["summary"]["current"][key] / report["summary"]["baseline"][key] - 1) * 100
            for key in report["summary"]["current"]
        }
        report["investigate_over_10_percent"] = [
            key for key in ("cold_start_median_ms", "warm_start_p95_ms", "search_p95_ms", "peak_rss_median_bytes")
            if report["change_percent"][key] > 10
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, indent=2))
    return 0 if report["behaviour_matches"] and report["source_stable"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-src", type=Path)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--messages", type=int, default=120)
    parser.add_argument("--queries", type=int, default=60)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--worker-src")
    parser.add_argument("--variant", choices=("baseline", "current"))
    parser.add_argument("--work-dir")
    args = parser.parse_args()
    if args.trials < 1 or not 1 <= args.messages <= 3600 or args.queries < 1:
        parser.error("positive trials/queries and 1..3600 messages are required")
    if args.worker_src:
        args.out.write_text(json.dumps(asyncio.run(workload(args))), encoding="utf-8")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
