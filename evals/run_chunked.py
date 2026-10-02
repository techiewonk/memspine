"""Run a C0-1 retrieval/QA job in item chunks, each in a fresh process, skipping items
already present in earlier runs. Long single-process runs on Windows can be killed
(no traceback) part-way; chunking bounds the loss to one chunk.

    python evals/run_chunked.py --done-run c0-lmes-retrieval-full -- <c0-1 args without --run-id/--item-ids>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent / ".venv" / "runs"


def done_items(run_prefix: str, system: str) -> set[str]:
    done: set[str] = set()
    for d in RUNS.glob(f"{run_prefix}*--{system}"):
        f = d / "results.jsonl"
        if not f.exists():
            continue
        for line in f.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "result":
                done.add(str(row["item_id"]))
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--done-run", required=True, help="run-id prefix whose finished items are skipped"
    )
    ap.add_argument("--system", default="memspine")
    ap.add_argument("--all-ids", required=True, help="file with one item id per line, in order")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    rest = [a for a in args.rest if a != "--"]
    ids = [
        line.strip() for line in Path(args.all_ids).read_text("utf-8").splitlines() if line.strip()
    ]
    todo = [i for i in ids if i not in done_items(args.done_run, args.system)]
    print(f"{len(todo)} items to run in chunks of {args.chunk}", flush=True)
    for n, start in enumerate(range(0, len(todo), args.chunk)):
        chunk = todo[start : start + args.chunk]
        run_id = f"{args.done_run}-chunk{n:02d}"
        cmd = [
            sys.executable,
            "-m",
            "memspine_evals",
            "c0-1",
            *rest,
            "--only-systems",
            args.system,
            "--item-ids",
            ",".join(chunk),
            "--run-id",
            run_id,
        ]
        rc = subprocess.call(cmd, cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"chunk {n:02d} ({len(chunk)} items) rc={rc}", flush=True)


if __name__ == "__main__":
    main()
