"""Run timing and GPU probes: latency percentiles, server-side timings, ingest throughput.

Everything here is best-effort and never raises into a run: a probe that cannot read the GPU
or a server that reports no timings yields ``None``, not a failure.

Wall latency of a reader or judge call includes queueing at the server. Ollama's
OpenAI-compatible ``/v1/chat/completions`` returns no timing fields (only ``usage``); its
native ``/api/chat`` returns ``total_duration`` / ``eval_duration`` (nanoseconds), and
llama.cpp servers return ``timings`` (milliseconds). ``extract_server_timing`` keeps whichever
the response carries, so the summary can split server compute from queueing when it exists.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "CONCURRENT_ARMS_ENV",
    "concurrent_arms",
    "extract_server_timing",
    "gpu_memory",
    "ingest_summary",
    "latency_block",
    "percentiles",
]

#: set by the launcher when several arms share one server (recorded in the summary)
CONCURRENT_ARMS_ENV = "MEMSPINE_CONCURRENT_ARMS"


def percentiles(values: Iterable[float | None]) -> dict[str, Any] | None:
    """``{n, p50, p95, mean}`` of the non-None values (nearest-rank); None when empty."""
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    n = len(xs)

    def rank(q: float) -> float:
        return xs[min(n - 1, max(0, int(q * n + 0.999999) - 1))]

    return {"n": n, "p50": rank(0.50), "p95": rank(0.95), "mean": sum(xs) / n}


def extract_server_timing(body: Mapping[str, Any]) -> dict[str, float] | None:
    """Server-side timings (milliseconds) from a chat response body, or None.

    Reads Ollama-native ``total_duration`` / ``load_duration`` / ``prompt_eval_duration`` /
    ``eval_duration`` (ns) and llama.cpp-style ``timings`` (``prompt_ms`` / ``predicted_ms``).
    """
    out: dict[str, float] = {}
    for key in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        value = body.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            out[key.replace("_duration", "_ms")] = float(value) / 1e6
    timings = body.get("timings")
    if isinstance(timings, Mapping):
        for key in ("prompt_ms", "predicted_ms"):
            value = timings.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                out[key] = float(value)
    if "total_ms" not in out and "prompt_ms" in out and "predicted_ms" in out:
        out["total_ms"] = out["prompt_ms"] + out["predicted_ms"]
    return out or None


def concurrent_arms(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    try:
        value = int(env.get(CONCURRENT_ARMS_ENV, ""))
    except ValueError:
        return None
    return value if value > 0 else None


def latency_block(
    rows: Sequence[Mapping[str, Any]],
    judge_server_timings: Sequence[Mapping[str, float]] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """D5: reader and judge latency p50/p95 over result rows (wall time, queueing included),
    server-side timings where the endpoint returned them, and the concurrent arm count."""
    reader = percentiles(r.get("latency_answer_ms") or None for r in rows)
    judge = percentiles(r.get("latency_judge_ms") or None for r in rows)
    reader_server = [
        t for r in rows if isinstance(t := (r.get("meta") or {}).get("server_timing"), Mapping)
    ]

    def server(timings: Sequence[Mapping[str, float]]) -> dict[str, Any] | None:
        stat = percentiles(t.get("total_ms") for t in timings)
        return stat

    return {
        "reader_ms": reader,
        "judge_ms": judge,
        "server_side_ms": {
            "reader": server(reader_server),
            "judge": server(judge_server_timings),
        },
        "wall_includes_queueing": True,
        "concurrent_arms": concurrent_arms(environ),
    }


def ingest_summary(per_item: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    """E4/F4: ingest wall time and turns per second, per item and overall.

    ``per_item[item] = {"turns": n, "wall_s": seconds}`` (deposits plus flushes plus build)."""
    items: dict[str, Any] = {}
    turns = 0.0
    wall = 0.0
    for item, rec in per_item.items():
        t, w = float(rec.get("turns", 0)), float(rec.get("wall_s", 0.0))
        turns += t
        wall += w
        items[item] = {
            "turns": int(t),
            "wall_s": round(w, 3),
            "turns_per_s": round(t / w, 3) if w > 0 else None,
        }
    return {
        "per_item": items,
        "turns": int(turns),
        "wall_s": round(wall, 3),
        "turns_per_s": round(turns / wall, 3) if wall > 0 else None,
    }


def gpu_memory(timeout: float = 3.0) -> dict[str, Any] | None:
    """GPU memory used / total in MiB per device via ``nvidia-smi``; None when unavailable.

    A short timeout and a swallow-everything guard: a missing driver, a hung smi or odd
    output never fails a run."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        done = subprocess.run(
            [exe, "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if done.returncode != 0:
            return None
        gpus = []
        for line in done.stdout.strip().splitlines():
            index, used, total = (p.strip() for p in line.split(","))
            gpus.append({"index": int(index), "used_mib": int(used), "total_mib": int(total)})
        if not gpus:
            return None
        return {
            "gpus": gpus,
            "used_mib": sum(g["used_mib"] for g in gpus),
            "total_mib": sum(g["total_mib"] for g in gpus),
        }
    except Exception:
        return None
