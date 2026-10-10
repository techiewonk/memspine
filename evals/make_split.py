"""I15: cut a dev / held-out split for ANY benchmark, by conversation / user / persona, with a content hash.

The output has the layout of ``analysis/locomo_split.json`` (``dataset_id``, ``content_sha256``,
``dev_items``, ``heldout_items``, ``seed``) plus ``unit`` and ``split_sha256`` (a hash of the id
lists, so a hand edit is caught by ``--check``). The unit is the whole cluster, never a question.

Where the units come from (pick one):

    --units-file ids.txt|ids.json            one id per line, or a JSON list
    --units-jsonl runs/<run>/results.jsonl   the distinct ``item_id`` of a finished run
                                             (any benchmark; ``--unit-field`` to change it)
    --units locomo-first-speakers            ``<conv>:<first speaker>`` of locomo10.json: the OP-Bench
                                             persona ids (needs ``--source``); with
                                             ``--opbench-tasks FILE`` the first speaker is read from
                                             the OP-Bench task file's first key instead of speaker_a

Which units are development (pick one):

    --dev ID [ID ...]            name them (held-out = every other unit)
    --n-dev N [--seed S]         the hash picks N clusters (stable on every machine)

Examples:

    # OP-Bench personas: dev = the four dev first speakers, held-out = the other six
    python evals/make_split.py --dataset-id op_bench --unit persona \\
        --units locomo-first-speakers --source data/locomo10.json \\
        --dev conv-26:Caroline conv-30:Jon conv-41:John conv-42:Joanna \\
        --out evals/analysis/opbench_persona_split.json

    # any benchmark with items in a finished run: 2 of its users, chosen by hash
    python evals/make_split.py --dataset-id prefeval --unit user --units-jsonl runs/x--memspine/results.jsonl \\
        --source data/prefeval.json --n-dev 2 --seed 7 --out evals/analysis/prefeval_split.json

    # verify a saved split against the data it was cut from
    python evals/make_split.py --check evals/analysis/opbench_persona_split.json --source data/locomo10.json

``--group-regex '^(conv-\\d+)'`` keeps every unit with the same first group together (two
personas of one conversation never straddle the split; the default is one cluster per unit).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

from memspine_evals.split import Split, file_sha256, split_ids  # noqa: E402


def locomo_first_speakers(source: Path, opbench_tasks: Path | None = None) -> list[str]:
    """``<sample_id>:<first speaker>`` for each LoCoMo conversation (the OP-Bench persona ids)."""
    convs = json.loads(source.read_text(encoding="utf-8"))
    tasks = json.loads(opbench_tasks.read_text(encoding="utf-8")) if opbench_tasks else None
    out = []
    for i, c in enumerate(convs):
        if tasks is not None and i < len(tasks) and isinstance(tasks[i], dict) and tasks[i]:
            first = next(iter(tasks[i]))
        else:
            first = c["conversation"]["speaker_a"]
        out.append(f"{c['sample_id']}:{first}")
    return out


def read_units(args: argparse.Namespace) -> list[str]:
    if args.units == "locomo-first-speakers":
        if not args.source:
            raise SystemExit("--units locomo-first-speakers needs --source locomo10.json")
        return locomo_first_speakers(Path(args.source[0]), Path(args.opbench_tasks) if args.opbench_tasks else None)
    if args.units_file:
        text = Path(args.units_file).read_text(encoding="utf-8")
        if text.lstrip().startswith("["):
            return [str(x) for x in json.loads(text)]
        return [ln.strip() for ln in text.splitlines() if ln.strip()]
    if args.units_jsonl:
        seen: dict[str, None] = {}
        for ln in Path(args.units_jsonl).read_text(encoding="utf-8").splitlines():
            if ln.strip():
                row = json.loads(ln)
                if row.get("kind", "result") == "result" and args.unit_field in row:
                    seen[str(row[args.unit_field])] = None
        return list(seen)
    raise SystemExit("give --units, --units-file or --units-jsonl")


def _group(regex: str | None) -> Callable[[str], str] | None:
    if not regex:
        return None
    rx = re.compile(regex)

    def group(u: str) -> str:
        m = rx.search(u)
        return m.group(1) if m and m.groups() else u

    return group


def check(path: Path, source: list[str] | None) -> int:
    split = Split.load(path)
    split.verify_ids()
    if source:
        actual = file_sha256(*source)
        if actual != split.content_sha256:
            print(f"MISMATCH: split content_sha256 {split.content_sha256[:12]} != source {actual[:12]}")
            return 1
    print(f"ok: {split.dataset_id} unit={split.unit or '?'} dev={len(split.dev_items)} "
          f"heldout={len(split.heldout_items)} hash={'verified' if split.split_sha256 else 'none recorded'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset-id")
    ap.add_argument("--unit", default="item", help="what one unit is: conversation | user | persona | ...")
    ap.add_argument("--source", nargs="+", help="data file(s) hashed into content_sha256")
    ap.add_argument("--content-sha256", help="use this hash instead of hashing --source")
    ap.add_argument("--units", choices=["locomo-first-speakers"])
    ap.add_argument("--opbench-tasks")
    ap.add_argument("--units-file")
    ap.add_argument("--units-jsonl")
    ap.add_argument("--unit-field", default="item_id")
    ap.add_argument("--dev", nargs="+")
    ap.add_argument("--n-dev", type=int)
    ap.add_argument("--seed", type=int, default=None, help="with --n-dev (default 0); recorded null for --dev")
    ap.add_argument("--group-regex")
    ap.add_argument("--note", default="")
    ap.add_argument("--out")
    ap.add_argument("--check", help="verify this split file (ids vs hash, and --source bytes)")
    args = ap.parse_args(argv)
    if args.check:
        return check(Path(args.check), args.source)
    if not (args.dataset_id and args.out):
        ap.error("--dataset-id and --out are required")
    sha = args.content_sha256 or (file_sha256(*args.source) if args.source else None)
    if not sha:
        ap.error("give --source (hashed) or --content-sha256")
    split = split_ids(
        read_units(args), dataset_id=args.dataset_id, content_sha256=sha, unit=args.unit,
        dev=args.dev, n_dev=args.n_dev,
        seed=None if args.dev else (args.seed or 0), group_of=_group(args.group_regex), note=args.note,
    )
    path = split.save(args.out)
    print(f"{path}: {len(split.dev_items)} dev, {len(split.heldout_items)} held-out, sha {sha[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
