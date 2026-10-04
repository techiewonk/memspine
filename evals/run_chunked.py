"""Run a C0-1 job in item chunks, each in a fresh process, skipping items already done.

Long single-process runs on Windows can be killed (no traceback) part-way; chunking bounds
the loss to one chunk.

    python evals/run_chunked.py --done-run c0-lmes-retrieval-full --all-ids ids.txt \\
        -- <c0-1 args without --run-id/--item-ids>

R3-9, four rules a resume must keep:

1. It reads and writes **one** runs directory: the ``--out`` in the c0-1 args, else the
   CLI's own default, and it passes that ``--out`` to every chunk explicitly.
2. An item is done only when every row of it is COMPLETED or TRUNCATED. ERROR and
   UNATTEMPTED rows are missing measurements, so the item runs again.
3. ``--max-model-calls N`` is the cap for the **whole** job, not per chunk: each chunk gets
   what the earlier chunks left (read from their ``summary.json``), and the job stops when
   nothing is left.
4. A later attempt of an item supersedes an earlier one, so a re-run that completes an
   item clears it even when an older chunk still holds its error rows.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: Same default as ``memspine_evals.cli.DEFAULT_OUT`` (asserted in the tests).
DEFAULT_RUNS = HERE / "runs"
DONE_STATUSES = ("completed", "truncated")


def runs_dir_from(rest: Sequence[str]) -> Path:
    """The ``--out`` the c0-1 args name, else the CLI default."""
    args = list(rest)
    for i, arg in enumerate(args):
        if arg == "--out" and i + 1 < len(args):
            return Path(args[i + 1])
        if arg.startswith("--out="):
            return Path(arg.split("=", 1)[1])
    return DEFAULT_RUNS


def split_cap(rest: Sequence[str]) -> tuple[list[str], int | None]:
    """Remove ``--max-model-calls N`` from the c0-1 args and return it as the job cap."""
    out: list[str] = []
    cap: int | None = None
    args = list(rest)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--max-model-calls" and i + 1 < len(args):
            cap = int(args[i + 1])
            i += 2
            continue
        if arg.startswith("--max-model-calls="):
            cap = int(arg.split("=", 1)[1])
        else:
            out.append(arg)
        i += 1
    return out, cap


def _run_dirs(runs: Path, run_prefix: str, system: str) -> list[Path]:
    # oldest first, so a later attempt of an item supersedes an earlier one
    return sorted(
        (d for d in runs.glob(f"{run_prefix}*--{system}") if (d / "results.jsonl").exists()),
        key=lambda d: (d / "results.jsonl").stat().st_mtime,
    )


def done_items(runs: Path, run_prefix: str, system: str) -> set[str]:
    """Items whose latest attempt has only COMPLETED/TRUNCATED rows."""
    latest: dict[str, list[str]] = {}
    for d in _run_dirs(runs, run_prefix, system):
        statuses: dict[str, list[str]] = {}
        for line in (d / "results.jsonl").read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "result":
                statuses.setdefault(str(row["item_id"]), []).append(str(row.get("status")))
        latest.update(statuses)
    return {item for item, statuses in latest.items() if all(s in DONE_STATUSES for s in statuses)}


def spent_calls(runs: Path, run_id: str, system: str) -> int:
    """Model calls a finished chunk made (judge + loop), 0 if it wrote no summary."""
    path = runs / f"{run_id}--{system}" / "summary.json"
    if not path.exists():
        return 0
    payload = json.loads(path.read_text("utf-8"))
    return int(payload.get("judge_model_calls", 0)) + int(payload.get("loop_model_calls", 0))


def chunk_command(
    rest: Sequence[str],
    system: str,
    chunk: Sequence[str],
    run_id: str,
    runs: Path,
    cap: int | None,
) -> list[str]:
    args = [a for a in rest if a != "--"]
    # --out is passed explicitly (rule 1), so drop any copy in the args
    cleaned: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--out":
            skip = True
            continue
        if arg.startswith("--out="):
            continue
        cleaned.append(arg)
    cmd = [
        sys.executable,
        "-m",
        "memspine_evals",
        "c0-1",
        *cleaned,
        "--out",
        str(runs),
        "--only-systems",
        system,
        "--item-ids",
        ",".join(chunk),
        "--run-id",
        run_id,
    ]
    if cap is not None:
        cmd += ["--max-model-calls", str(cap)]
    return cmd


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--done-run", required=True, help="run-id prefix whose finished items are skipped"
    )
    ap.add_argument("--system", default="memspine")
    ap.add_argument("--all-ids", required=True, help="file with one item id per line, in order")
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    rest, cap = split_cap([a for a in args.rest if a != "--"])
    runs = runs_dir_from(rest)
    ids = [
        line.strip() for line in Path(args.all_ids).read_text("utf-8").splitlines() if line.strip()
    ]
    done = done_items(runs, args.done_run, args.system)
    todo = [i for i in ids if i not in done]
    print(f"{len(todo)} items to run in chunks of {args.chunk} (runs dir {runs})", flush=True)
    remaining = cap
    for n, start in enumerate(range(0, len(todo), args.chunk)):
        if remaining is not None and remaining <= 0:
            print(f"model-call cap {cap} spent; stopping before chunk {n:02d}", flush=True)
            return 2
        chunk = todo[start : start + args.chunk]
        run_id = f"{args.done_run}-chunk{n:02d}"
        cmd = chunk_command(rest, args.system, chunk, run_id, runs, remaining)
        rc = subprocess.call(cmd, cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if remaining is not None:
            remaining -= spent_calls(runs, run_id, args.system)
        print(f"chunk {n:02d} ({len(chunk)} items) rc={rc} remaining={remaining}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
