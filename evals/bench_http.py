"""E1: `localhost` versus `127.0.0.1` request latency (gap register E1). CPU, no model.

    python evals/bench_http.py [--base http://127.0.0.1:11434] [--path /api/version] [--n 20]
                               [--hosts localhost 127.0.0.1] [--fail-ratio 3] [--json] [--log]

On Windows `localhost` can resolve to ::1 first; a server that listens on IPv4 only then costs one
failed IPv6 connect (up to ~2 s) before every fresh connection. This opens a NEW connection per
request, so the DNS lookup and the connect are inside the timing, and reports for each host the
addresses it resolves to (in order), the mean / p50 / p95 / max in milliseconds and the failure
count, and the ratio of the slowest host's mean to the fastest. The default path is Ollama's
`/api/version` (a version string: it loads no model and uses no GPU). With `--fail-ratio R` the exit
code is 1 when that ratio exceeds R, so a script can gate on it. `--log` appends a JSON line to
`evals/runs/_logs/bench_http.jsonl`.

Run it against the real server only when nothing is in flight; the harness itself keeps using
127.0.0.1 and warns about `localhost` with `--warn-localhost` (``netcheck.py``).
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = ["measure", "resolve_order", "summarise", "main"]


def resolve_order(host: str, port: int) -> list[str]:
    """The addresses ``host`` resolves to, in the order a client tries them."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return [f"unresolved: {exc}"]
    seen: list[str] = []
    for family, _, _, _, sockaddr in infos:
        label = ("ipv6 " if family == socket.AF_INET6 else "ipv4 ") + str(sockaddr[0])
        if label not in seen:
            seen.append(label)
    return seen


def measure(host: str, port: int, path: str, n: int, timeout: float = 5.0) -> dict[str, Any]:
    """``n`` requests, each on a fresh connection. Returns the raw seconds of the successful
    ones and the failure count."""
    times: list[float] = []
    failures = 0
    for _ in range(n):
        started = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request("GET", path)
            conn.getresponse().read()
            conn.close()
            times.append(time.perf_counter() - started)
        except Exception:  # unreachable, refused, timed out: counted, not raised
            failures += 1
    return {"host": host, "times": times, "failures": failures}


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-host statistics in milliseconds and the slowest / fastest mean ratio."""
    hosts: list[dict[str, Any]] = []
    for run in runs:
        t = [x * 1000.0 for x in run["times"]]
        hosts.append(
            {
                "host": run["host"],
                "addresses": run.get("addresses", []),
                "n_ok": len(t),
                "failures": run["failures"],
                "mean_ms": round(statistics.fmean(t), 2) if t else None,
                "p50_ms": round(_pct(t, 0.5), 2) if t else None,
                "p95_ms": round(_pct(t, 0.95), 2) if t else None,
                "max_ms": round(max(t), 2) if t else None,
            }
        )
    means = [h["mean_ms"] for h in hosts if h["mean_ms"]]
    ratio = round(max(means) / min(means), 2) if len(means) >= 2 else None
    return {"hosts": hosts, "ratio_slowest_to_fastest": ratio}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:11434", help="scheme and port are taken from here")
    ap.add_argument("--path", default="/api/version")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--hosts", nargs="+", default=["localhost", "127.0.0.1"])
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--fail-ratio", type=float, default=None,
                    help="exit 1 when the slowest host's mean exceeds this multiple of the fastest")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", action="store_true", help="append the result to evals/runs/_logs/bench_http.jsonl")
    args = ap.parse_args(argv)
    parsed = urlparse(args.base)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    runs = []
    for host in args.hosts:
        run = measure(host, port, args.path, args.n, args.timeout)
        run["addresses"] = resolve_order(host, port)
        runs.append(run)
    result = {"base": args.base, "path": args.path, "n": args.n, **summarise(runs)}
    if args.log:
        out = Path(__file__).parent / "runs" / "_logs" / "bench_http.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **result}) + "\n")
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"{args.base}{args.path}  n={args.n} (fresh connection per request)")
        for h in result["hosts"]:
            print(f"  {h['host']:<12} mean {h['mean_ms']} ms  p50 {h['p50_ms']}  p95 {h['p95_ms']}  "
                  f"max {h['max_ms']}  failures {h['failures']}  resolves: {', '.join(h['addresses'])}")
        print(f"  slowest / fastest mean: {result['ratio_slowest_to_fastest']}")
    ratio = result["ratio_slowest_to_fastest"]
    if args.fail_ratio is not None and ratio is not None and ratio > args.fail_ratio:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
