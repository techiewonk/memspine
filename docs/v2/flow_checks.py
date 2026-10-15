# ruff: noqa: E501
"""Manual checks for every engine flow, one step at a time.

    python docs/v2/flow_checks.py --list                      # all flows and their steps
    python docs/v2/flow_checks.py forget                      # every step of one flow
    python docs/v2/flow_checks.py forget F7.3                 # one step (earlier steps run quietly first)
    python docs/v2/flow_checks.py forget F7.3 --events        # + the wiretap events the step produced
    python docs/v2/flow_checks.py forget F7.3 --lines         # + every engine source line it executed
    python docs/v2/flow_checks.py forget F7.3 --lines --only trust/ --limit 400
    python docs/v2/flow_checks.py all                         # every flow, summary at the end

Run from the repo root with PYTHONPATH=src. CPU only, in-memory engine, invented names.
Guide: docs/v2/FLOW_TESTING.md. Ingestion/sleep/read stage by stage: docs/v2/MANUAL_TESTING.md.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# a Windows console (cp1252) cannot print every character engine messages use; never crash on it
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]

from flows._common import run_steps  # noqa: E402

#: flow name -> module in docs/v2/flows (each defines NAME, TITLE, STEPS and optional CONFIG)
FLOWS = {
    "ingest": "f01_ingest",
    "sleep": "f03_sleep",
    "read": "f04_read",
    "quarantine": "f06_quarantine",
    "forget": "f07_forget",
    "correct": "f08_correct",
    "rebuild": "f09_rebuild",
    "procedural": "f10_procedural",
    "tasks": "f11_tasks",
    "prospective": "f12_prospective",
    "shared": "f13_shared",
    "graph": "f14_graph",
    "assets": "f15_assets",
    "views": "f16_views",
}


def _load(name: str):
    return importlib.import_module(f"flows.{FLOWS[name]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("flow", nargs="?", help="flow name, 'all', or --list")
    ap.add_argument("steps", nargs="*", help="step ids (default: all steps of the flow)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--events", action="store_true", help="print the wiretap events of each step")
    ap.add_argument("--lines", action="store_true", help="print every executed src/memspine line")
    ap.add_argument("--only", help="with --lines: only files whose path contains this (e.g. trust/)")
    ap.add_argument("--limit", type=int, default=300, help="with --lines: max lines per step")
    args = ap.parse_args()

    if args.list or not args.flow:
        for name in FLOWS:
            try:
                mod = _load(name)
            except ModuleNotFoundError:
                print(f"{name:12} (not written yet)")
                continue
            print(f"{name:12} {mod.TITLE}")
            for s in mod.STEPS:
                print(f"  {s.id:7} {s.title}")
        return 0

    names = list(FLOWS) if args.flow == "all" else [args.flow]
    unknown = [n for n in names if n not in FLOWS]
    if unknown:
        sys.exit(f"unknown flow {unknown}; use --list")
    results: list[tuple[str, bool]] = []
    for name in names:
        try:
            mod = _load(name)
        except ModuleNotFoundError:
            print(f"\n{name}: not written yet")
            continue
        results += asyncio.run(run_steps(mod.NAME, mod.STEPS, args.steps, events=args.events, lines=args.lines,
                                         only=args.only, limit=args.limit, config=getattr(mod, "CONFIG", None)))
    bad = sorted({sid for sid, ok in results if not ok})
    print(f"\n{sum(ok for _, ok in results)}/{len(results)} checks passed" + (f"; failing steps: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
