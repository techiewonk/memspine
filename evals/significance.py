"""Paired significance of an accuracy difference between two run groups.

    python significance.py --a RUN [--a RUN ...] --b RUN [--b RUN ...]
    python significance.py --runs runs --prefix aamas27-locomo-qwen3 \
        --pair H1 --pair combo-A:R-cohere [--base memspine-base] [--out TABLE.md]

A side (``--a`` or ``--b``) is one or more run directories. As in ``plan_report.py``,
rows are merged by ``query_id`` within one replicate (originals first, ``-resume``
runs last, so a resume row replaces the original's), and each ``--rN`` repeat is its
own replicate. A question's score on a side is the mean of its scored rows over the
replicates (a row is scored when its status is ``completed`` or ``truncated`` and it
has a score); a question no replicate scored counts as 0, as in ``plan_report.py``.

Only questions present on both sides are compared. Reported, overall and per category:

* the paired accuracy delta ``B - A`` (percentage points);
* a paired bootstrap 95% CI: questions resampled with replacement (per category for the
  category rows), 10,000 resamples, fixed seed, percentile interval;
* the exact (binomial) two-sided McNemar p-value on the discordant pairs. With repeats a
  side's per-question verdict is the majority over its replicates; a question tied at
  exactly 0.5 on either side is left out of McNemar (and counted in ``ties``).

``--pair X`` compares arm ``X`` against ``--base``; ``--pair X:Y`` compares ``Y``
(as B) against ``X`` (as A). Holm-adjusted overall p-values are given across the pairs.
No model calls; the run files are only read.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from plan_report import _DIR

__all__ = [
    "PairResult",
    "Side",
    "bootstrap_ci",
    "compare",
    "holm",
    "load_side",
    "mcnemar_exact",
    "render_markdown",
]

#: Default number of bootstrap resamples and the fixed seed.
RESAMPLES = 10_000
SEED = 20261005

#: LoCoMo category labels as recorded in ``type_label``.
CATEGORY_NAMES = {
    "cat1": "multi-hop",
    "cat2": "temporal",
    "cat3": "open-domain",
    "cat4": "single-hop",
    "cat5": "adversarial",
}


@dataclass
class Side:
    """One side of a comparison: per-question mean score and majority verdict."""

    label: str
    #: query_id -> mean score over the replicates that scored it (0.0 if none did)
    score: dict[str, float] = field(default_factory=dict)
    #: query_id -> category label (``type_label``)
    category: dict[str, str] = field(default_factory=dict)
    replicates: int = 0
    dirs: list[str] = field(default_factory=list)


def _rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") != "manifest" and "query_id" in row:
            out.append(row)
    return out


def _replicate_key(run_dir: Path) -> str:
    """The repeat a run directory belongs to (``""`` for the first run)."""
    m = _DIR.match(run_dir.name)
    return (m["rep"] or "") if m else ""


def _is_resume(run_dir: Path) -> bool:
    m = _DIR.match(run_dir.name)
    return bool(m and m["resume"])


def _scored(row: dict[str, Any]) -> bool:
    return row.get("status") in ("completed", "truncated") and row.get("score") is not None


def load_side(dirs: Sequence[Path], label: str | None = None) -> Side:
    """Merge ``dirs`` into one side (resumes into their replicate, repeats averaged)."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for d in dirs:
        groups[_replicate_key(d)].append(d)
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    category: dict[str, str] = {}
    for group in groups.values():
        merged: dict[str, dict[str, Any]] = {}
        for d in sorted(group, key=_is_resume):  # originals first, resumes last
            for row in _rows(d):
                merged[str(row["query_id"])] = row
        for qid, row in merged.items():
            category.setdefault(qid, str(row.get("type_label") or "?"))
            sums.setdefault(qid, 0.0)
            if _scored(row):
                sums[qid] += float(row["score"])
                counts[qid] += 1
    score = {q: (sums[q] / counts[q] if counts[q] else 0.0) for q in sums}
    return Side(
        label=label or (dirs[0].name if dirs else "?"),
        score=score,
        category=category,
        replicates=len(groups),
        dirs=[d.name for d in dirs],
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value for ``b`` and ``c`` discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, float(Fraction(2 * tail, 1 << n)))


def bootstrap_ci(
    diffs: Sequence[float],
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    level: float = 0.95,
) -> tuple[float, float]:
    """Percentile CI of the mean of ``diffs`` (paired differences), resampling items."""
    if not diffs:
        return (math.nan, math.nan)
    values = np.asarray(diffs, dtype=float)
    rng = np.random.default_rng(seed)
    n = len(values)
    means = np.empty(resamples)
    chunk = max(1, 2_000_000 // n)  # bound memory: chunk x n indices at a time
    for start in range(0, resamples, chunk):
        stop = min(resamples, start + chunk)
        idx = rng.integers(0, n, size=(stop - start, n))
        means[start:stop] = values[idx].mean(axis=1)
    alpha = (1 - level) / 2
    low, high = np.quantile(means, [alpha, 1 - alpha])
    return (float(low), float(high))


def _verdict(score: float) -> int | None:
    if score > 0.5:
        return 1
    if score < 0.5:
        return 0
    return None


@dataclass
class Cell:
    """One comparison (overall or one category)."""

    n: int
    acc_a: float
    acc_b: float
    delta: float
    ci: tuple[float, float]
    b: int  # A right, B wrong
    c: int  # A wrong, B right
    ties: int
    p: float

    @property
    def significant(self) -> bool:
        return self.p < 0.05 and (self.ci[0] > 0 or self.ci[1] < 0)


@dataclass
class PairResult:
    a: str
    b: str
    overall: Cell
    by_category: dict[str, Cell]
    replicates: tuple[int, int]
    n_only_a: int
    n_only_b: int
    p_holm: float | None = None


def _cell(a: Side, b: Side, qids: Sequence[str], resamples: int, seed: int) -> Cell:
    diffs = [b.score[q] - a.score[q] for q in qids]
    n_b = n_c = ties = 0
    for q in qids:
        va, vb = _verdict(a.score[q]), _verdict(b.score[q])
        if va is None or vb is None:
            ties += 1
        elif va == 1 and vb == 0:
            n_b += 1
        elif va == 0 and vb == 1:
            n_c += 1
    n = len(qids)
    low, high = bootstrap_ci(diffs, resamples=resamples, seed=seed)
    return Cell(
        n=n,
        acc_a=100 * sum(a.score[q] for q in qids) / max(1, n),
        acc_b=100 * sum(b.score[q] for q in qids) / max(1, n),
        delta=100 * sum(diffs) / max(1, n),
        ci=(100 * low, 100 * high),
        b=n_b,
        c=n_c,
        ties=ties,
        p=mcnemar_exact(n_b, n_c),
    )


def compare(a: Side, b: Side, *, resamples: int = RESAMPLES, seed: int = SEED) -> PairResult:
    """Paired comparison of ``b`` against ``a`` on their shared questions."""
    shared = sorted(set(a.score) & set(b.score))
    by_cat: dict[str, list[str]] = defaultdict(list)
    for q in shared:
        by_cat[a.category.get(q) or b.category.get(q) or "?"].append(q)
    return PairResult(
        a=a.label,
        b=b.label,
        overall=_cell(a, b, shared, resamples, seed),
        by_category={c: _cell(a, b, qs, resamples, seed) for c, qs in sorted(by_cat.items())},
        replicates=(a.replicates, b.replicates),
        n_only_a=len(set(a.score) - set(b.score)),
        n_only_b=len(set(b.score) - set(a.score)),
    )


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, in input order."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 0.0
    m = len(pvalues)
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


# -- arms by name --------------------------------------------------------------------


def arm_dirs(runs: Path, prefix: str, arm: str, system: str) -> list[Path]:
    """Every run directory of ``arm`` / ``system`` under ``runs`` (originals, resumes, repeats)."""
    out = []
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or not d.name.startswith(prefix + "--"):
            continue
        m = _DIR.match(d.name)
        if m and m["arm"] == arm and m["system"] == system and (d / "results.jsonl").exists():
            out.append(d)
    return out


def parse_pairs(specs: Iterable[str], base: str) -> list[tuple[str, str]]:
    """``X`` -> (base, X); ``X:Y`` -> (X, Y), as (A, B)."""
    pairs = []
    for spec in specs:
        if ":" in spec:
            left, right = spec.split(":", 1)
            pairs.append((left, right))
        else:
            pairs.append((base, spec))
    return pairs


# -- output ---------------------------------------------------------------------------


def _p(p: float) -> str:
    return f"{p:.2g}" if p >= 1e-4 else f"{p:.1e}"


def _ci(cell: Cell) -> str:
    return f"[{cell.ci[0]:+.1f}, {cell.ci[1]:+.1f}]"


def render_markdown(results: Sequence[PairResult], *, resamples: int, seed: int) -> str:
    lines = [
        f"Paired bootstrap: {resamples:,} resamples, seed {seed}, percentile 95% CI. "
        "McNemar: exact two-sided binomial on discordant questions. "
        "Δ = B - A in points. **Bold** = CI excludes 0 and p < 0.05; "
        "p (Holm) adjusts the overall p across the pairs in this table.",
        "",
        "| A | B | reps A/B | n | acc A | acc B | Δ | 95% CI | A✓B✗ / A✗B✓ | p | p (Holm) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        o = r.overall
        delta = f"**{o.delta:+.1f}**" if o.significant else f"{o.delta:+.1f}"
        holm_p = _p(r.p_holm) if r.p_holm is not None else ""
        lines.append(
            f"| {r.a} | {r.b} | {r.replicates[0]}/{r.replicates[1]} | {o.n} "
            f"| {o.acc_a:.1f} | {o.acc_b:.1f} | {delta} | {_ci(o)} | {o.b} / {o.c} "
            f"| {_p(o.p)} | {holm_p} |"
        )
    cats = sorted({c for r in results for c in r.by_category})
    lines += [
        "",
        "Per category: Δ [95% CI] (McNemar p). Not multiplicity-adjusted.",
        "",
        "| A | B | " + " | ".join(f"{c} {CATEGORY_NAMES.get(c, '')}".strip() for c in cats) + " |",
        "|---|---|" + "---|" * len(cats),
    ]
    for r in results:
        cells = []
        for c in cats:
            cell = r.by_category.get(c)
            if cell is None:
                cells.append("")
                continue
            delta = f"**{cell.delta:+.1f}**" if cell.significant else f"{cell.delta:+.1f}"
            cells.append(f"{delta} {_ci(cell)} (p {_p(cell.p)}, n {cell.n})")
        lines.append(f"| {r.a} | {r.b} | " + " | ".join(cells) + " |")
    notes = [r for r in results if r.n_only_a or r.n_only_b or r.overall.ties]
    if notes:
        lines += ["", "Coverage notes:"]
        for r in notes:
            lines.append(
                f"- {r.a} vs {r.b}: {r.n_only_a} question(s) only in A, {r.n_only_b} only in B "
                f"(not compared); {r.overall.ties} question(s) tied at 0.5 on a side "
                "(left out of McNemar)."
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--a", action="append", default=[], metavar="RUN", help="run dir of side A")
    ap.add_argument("--b", action="append", default=[], metavar="RUN", help="run dir of side B")
    ap.add_argument("--runs", default=str(Path(__file__).resolve().parent / "runs"))
    ap.add_argument("--prefix", default=None, help="run-id prefix, for --pair")
    ap.add_argument("--pair", action="append", default=[], help="ARM (vs --base) or A:B")
    ap.add_argument("--base", default="memspine-base")
    ap.add_argument("--system", default="memspine", help="system id of the arms, for --pair")
    ap.add_argument("--resamples", type=int, default=RESAMPLES)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", default=None, help="also write the Markdown table here")
    args = ap.parse_args(argv)

    comparisons: list[tuple[Side, Side]] = []
    if args.a or args.b:
        if not (args.a and args.b):
            ap.error("--a and --b go together")
        comparisons.append(
            (load_side([Path(p) for p in args.a]), load_side([Path(p) for p in args.b]))
        )
    if args.pair:
        if not args.prefix:
            ap.error("--pair needs --prefix")
        runs = Path(args.runs)
        for arm_a, arm_b in parse_pairs(args.pair, args.base):
            sides = []
            for arm in (arm_a, arm_b):
                dirs = arm_dirs(runs, args.prefix, arm, args.system)
                if not dirs:
                    ap.error(f"no run directories for arm {arm!r} under {runs}")
                sides.append(load_side(dirs, label=arm))
            comparisons.append((sides[0], sides[1]))
    if not comparisons:
        ap.error("give --a/--b or --pair")

    results = [compare(a, b, resamples=args.resamples, seed=args.seed) for a, b in comparisons]
    for r, p in zip(results, holm([r.overall.p for r in results]), strict=True):
        r.p_holm = p
    table = render_markdown(results, resamples=args.resamples, seed=args.seed)
    print(table)
    if args.out:
        Path(args.out).write_text(table + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
