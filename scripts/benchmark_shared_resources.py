"""RM-80: evidence for ADR-079 shared-resource defaults (scope, warm-up, pools).

Each scenario runs in a fresh process: N Engines in one process with identical model
settings and separate SQLite files, measuring start time, first-request latency (lazy
loads), steady search latency, RSS, threads and OS handles. Uses the locally cached
fastembed model (no download; fails if absent) or the hash embedder.

    python scripts/benchmark_shared_resources.py --engines 1 2 4 --out result.json
    python scripts/benchmark_shared_resources.py --overrides '{"runtime": {...}}' ...
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


async def scenario(args: argparse.Namespace) -> dict[str, Any]:
    import psutil

    proc = psutil.Process()
    from memspine import Engine

    work = Path(tempfile.mkdtemp(prefix="rm80-"))
    embedding = (
        {"provider": "fastembed", "settings": {"model": args.model}}
        if args.embedder == "fastembed"
        else {"provider": "hash"}
    )
    overrides = json.loads(args.overrides) if args.overrides else {}
    rss0 = proc.memory_info().rss
    engines, start_ms = [], []
    for index in range(args.engines):
        kwargs = _deep_merge(
            {
                "template": "core",
                "dotenv_path": None,
                "storage": {
                    "provider": "sqlite",
                    "settings": {"path": str(work / f"e{index}.db"), "busy_timeout_s": 5.0},
                },
                "embedding": embedding,
            },
            overrides,
        )
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
            t = time.perf_counter()
            await eng.ingest_messages(
                [{"message_id": f"m{index}", "role": "user", "content": "Asha cycles in Lisbon."}],
                namespace=ns,
                session_id="s",
            )
            await eng.search("cycling Lisbon", namespace=ns, top_k=3)
            first_ms.append((time.perf_counter() - t) * 1000)
            for _ in range(args.queries):
                t = time.perf_counter()
                await eng.search("cycling Lisbon", namespace=ns, top_k=3)
                steady_ms.append((time.perf_counter() - t) * 1000)
        mem = proc.memory_info()
        handles = proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
        result = {
            "engines": args.engines,
            "embedder": args.embedder,
            "overrides": overrides,
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
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        print(json.dumps(asyncio.run(scenario(args))))
        return 0
    runs: list[dict[str, Any]] = []
    for count in args.engines:
        for trial in range(args.trials):
            cmd = [
                sys.executable,
                __file__,
                "--child",
                "--engines",
                str(count),
                "--embedder",
                args.embedder,
                "--model",
                args.model,
                "--queries",
                str(args.queries),
                "--settle-s",
                str(args.settle_s),
            ]
            if args.overrides:
                cmd += ["--overrides", args.overrides]
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            done = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
            if done.returncode:
                raise SystemExit(f"scenario failed (engines={count}):\n{done.stderr[-4000:]}")
            row = json.loads(done.stdout.strip().splitlines()[-1])
            row["trial"] = trial
            runs.append(row)
            print(
                f"engines={count} trial={trial} start={[round(x) for x in row['start_ms']]} "
                f"first={[round(x) for x in row['first_request_ms']]} rss_end={row['rss_delta_end_mb']:.0f}MB "
                f"handles={row['os_handles']} threads={row['threads']}",
                flush=True,
            )
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
