"""Judge audit: a stratified sample of verdicts for a human to mark, and its scoring.

    python judge_audit.py --run RUN [--run RESUME] --out audit.csv [--wrong 20] [--right 10]
    python judge_audit.py --score audit.csv

**Sample.** From the run's scored rows (status ``completed`` / ``truncated``; resume runs
merged by ``query_id``, the resume row replacing the original), it draws, per category,
``--wrong`` rows the judge marked wrong and ``--right`` rows it marked correct (all of them
when a stratum is smaller), with a fixed seed. The CSV carries the question, gold, answer
and verdict, the stratum size, and two empty columns for the human: ``human`` (``agree`` or
``disagree`` with the judge's verdict) and ``note``. Nothing is pre-filled.

**Score.** ``--score`` reads the marked CSV and reports, over the rows the human marked:

* raw agreement between judge and human, overall and per category and verdict;
* Cohen's kappa between the judge's verdict and the human's (the human's verdict is the
  judge's when ``agree``, its opposite when ``disagree``);
* a stratum-weighted agreement: each stratum (category x verdict) weighted by its size in
  the run, an estimate of agreement over the whole run rather than over the sample, which
  over-represents wrong verdicts by design.

No model calls.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["AuditScore", "cohen_kappa", "draw_sample", "load_rows", "score_rows", "write_csv"]

SEED = 20261005
FIELDS = (
    "audit_id",
    "item_id",
    "query_id",
    "category",
    "question",
    "gold",
    "answer",
    "judge_verdict",
    "judge_raw",
    "stratum_n",
    "human",
    "note",
)
AGREE = {"agree", "a", "y", "yes", "1"}
DISAGREE = {"disagree", "d", "n", "no", "0"}


def _is_resume(path: Path) -> bool:
    return "-resume--" in path.name or path.name.endswith("-resume")


def load_rows(runs: Sequence[Path]) -> list[dict[str, Any]]:
    """Scored result rows of ``runs``, merged by ``query_id`` (resumes last)."""
    merged: dict[str, dict[str, Any]] = {}
    for run in sorted(runs, key=_is_resume):
        for line in (run / "results.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") == "manifest" or "query_id" not in row:
                continue
            merged[str(row["query_id"])] = row
    return [
        r
        for r in merged.values()
        if r.get("status") in ("completed", "truncated") and r.get("score") is not None
    ]


def _verdict(row: dict[str, Any]) -> str:
    return "correct" if float(row["score"]) >= 1.0 else "wrong"


def draw_sample(
    rows: Sequence[dict[str, Any]], *, wrong: int, right: int, seed: int = SEED
) -> list[dict[str, str]]:
    """The stratified sample as CSV records (human columns empty)."""
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[(str(row.get("type_label") or "?"), _verdict(row))].append(row)
    rng = random.Random(seed)
    out: list[dict[str, str]] = []
    for (cat, verdict), pool in sorted(strata.items()):
        pool = sorted(pool, key=lambda r: (str(r.get("item_id")), str(r["query_id"])))
        k = min(len(pool), wrong if verdict == "wrong" else right)
        for row in sorted(rng.sample(pool, k), key=lambda r: str(r["query_id"])):
            meta = row.get("meta") or {}
            out.append(
                {
                    "item_id": str(row.get("item_id", "")),
                    "query_id": str(row["query_id"]),
                    "category": cat,
                    "question": str(row.get("question", "")),
                    "gold": str(row.get("gold", "")),
                    "answer": str(row.get("answer", "")),
                    "judge_verdict": verdict,
                    "judge_raw": str(meta.get("judge_raw", "")),
                    "stratum_n": str(len(pool)),
                    "human": "",
                    "note": "",
                }
            )
    rng.shuffle(out)  # mixed order, so the marker does not see the strata in blocks
    for i, record in enumerate(out, 1):
        record["audit_id"] = f"A{i:03d}"
    return sorted(out, key=lambda r: r["audit_id"])


def write_csv(records: Sequence[dict[str, str]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({k: record.get(k, "") for k in FIELDS})


def cohen_kappa(pairs: Sequence[tuple[str, str]]) -> float:
    """Cohen's kappa of two raters over ``pairs``; ``nan`` when undefined."""
    n = len(pairs)
    if n == 0:
        return float("nan")
    labels = sorted({x for pair in pairs for x in pair})
    observed = sum(a == b for a, b in pairs) / n
    expected = sum(
        (sum(a == lab for a, _ in pairs) / n) * (sum(b == lab for _, b in pairs) / n)
        for lab in labels
    )
    if expected >= 1.0:
        return float("nan")
    return (observed - expected) / (1 - expected)


@dataclass
class AuditScore:
    n_marked: int
    n_unmarked: int
    agreement: float
    kappa: float
    weighted_agreement: float
    #: (category, judge verdict) -> (agreed, marked)
    by_stratum: dict[tuple[str, str], tuple[int, int]]
    invalid: list[str]


def score_rows(records: Sequence[dict[str, str]]) -> AuditScore:
    pairs: list[tuple[str, str]] = []
    by: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    sizes: dict[tuple[str, str], int] = {}
    unmarked = 0
    invalid: list[str] = []
    for r in records:
        mark = (r.get("human") or "").strip().lower()
        if not mark:
            unmarked += 1
            continue
        if mark not in AGREE | DISAGREE:
            invalid.append(f"{r.get('audit_id')}: {mark!r}")
            continue
        judge = r["judge_verdict"]
        agree = mark in AGREE
        human = judge if agree else ("wrong" if judge == "correct" else "correct")
        pairs.append((judge, human))
        key = (r["category"], judge)
        by[key][0] += int(agree)
        by[key][1] += 1
        sizes[key] = int(r.get("stratum_n") or 0)
    n = len(pairs)
    total = sum(sizes.values())
    weighted = (
        sum(sizes[k] * a / m for k, (a, m) in by.items() if m) / total if total else float("nan")
    )
    return AuditScore(
        n_marked=n,
        n_unmarked=unmarked,
        agreement=sum(a == b for a, b in pairs) / n if n else float("nan"),
        kappa=cohen_kappa(pairs),
        weighted_agreement=weighted,
        by_stratum={k: (v[0], v[1]) for k, v in sorted(by.items())},
        invalid=invalid,
    )


def render_score(score: AuditScore) -> str:
    lines = [
        f"marked {score.n_marked}, unmarked {score.n_unmarked}"
        + (f", invalid marks: {score.invalid}" if score.invalid else ""),
        f"raw agreement {score.agreement:.3f}; Cohen's kappa {score.kappa:.3f}; "
        f"stratum-weighted agreement {score.weighted_agreement:.3f}",
        "",
        "| category | judge verdict | agreed / marked |",
        "|---|---|---|",
    ]
    for (cat, verdict), (agreed, marked) in score.by_stratum.items():
        lines.append(f"| {cat} | {verdict} | {agreed} / {marked} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="append", default=[], help="run dir (repeat for resumes)")
    ap.add_argument("--out", default=None, help="CSV to write the sample to")
    ap.add_argument("--wrong", type=int, default=20, help="judged-wrong rows per category")
    ap.add_argument("--right", type=int, default=10, help="judged-right rows per category")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--score", default=None, metavar="CSV", help="score a marked CSV")
    args = ap.parse_args(argv)
    if args.score:
        with Path(args.score).open(encoding="utf-8", newline="") as fh:
            score = score_rows(list(csv.DictReader(fh)))
        print(render_score(score))
        return 0
    if not args.run or not args.out:
        ap.error("give --run and --out to draw a sample, or --score CSV")
    rows = load_rows([Path(r) for r in args.run])
    records = draw_sample(rows, wrong=args.wrong, right=args.right, seed=args.seed)
    write_csv(records, Path(args.out))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for r in records:
        counts[(r["category"], r["judge_verdict"])] += 1
    print(f"wrote {len(records)} rows to {args.out}", file=sys.stderr)
    for key, n in sorted(counts.items()):
        print(f"  {key[0]} {key[1]}: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
