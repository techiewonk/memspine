"""Paired significance of an accuracy difference between two run groups.

    python significance.py --a RUN [--a RUN ...] --b RUN [--b RUN ...]
    python significance.py --runs runs --prefix aamas27-locomo-qwen3 \
        --pair H1 --pair combo-A:R-cohere [--base memspine-base] [--out TABLE.md]

A side (``--a`` or ``--b``) is one or more run directories. As in ``plan_report.py``,
rows are merged by ``query_id`` within one replicate (originals first, then
``-chunkNN`` parts, then ``-resume`` runs, so a later row replaces an earlier one),
and each ``--rN`` repeat is its own replicate. A question's score on a side is the
mean of its rows over the replicates it appears in. One rule for error rows, single
run or repeated: a row that is not scored (status other than ``completed`` /
``truncated``, or no score) counts as 0, as in ``plan_report.py``, and the number of
such rows is reported per side.

Only questions present on both sides are compared. Reported, overall and per category:

* the paired accuracy delta ``B - A`` (percentage points);
* a paired bootstrap 95% CI: questions resampled with replacement (per category for the
  category rows), 10,000 resamples, fixed seed, percentile interval;
* the p-value of a paired sign-flip permutation test on the per-question mean-score
  differences (the same estimand as the delta): 10,000 random sign flips, fixed seed,
  two-sided, ``p = (1 + #{|flipped sum| >= |observed sum|}) / (1 + flips)``. It is valid
  whatever the number of replicates on either side;
* McNemar (exact binomial, two-sided, on the discordant questions) as a cross-check.
  When both sides have exactly one run per question this is the classic exact McNemar.
  With repeats it is computed per matched repeat pair (a single-run side is paired with
  every replicate of the other; two repeated sides are paired replicate by replicate)
  and reported as a range. (Correction, 2026-10-05: an earlier version majority-voted
  the repeats into one verdict per question, which rejects a true null about 18% of the
  time at the 5% level when one side has 3 repeats and the other 1.)

``--pair X`` compares arm ``X`` against ``--base``; ``--pair X:Y`` compares ``Y``
(as B) against ``X`` (as A). Holm-adjusted overall permutation p-values are given
across the pairs.
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
from plan_report import _DIR, part_order

__all__ = [
    "PairResult",
    "Side",
    "bootstrap_ci",
    "compare",
    "holm",
    "load_side",
    "mcnemar_exact",
    "permutation_p",
    "render_markdown",
    "repeat_pairs",
]

#: Default number of bootstrap resamples / sign-flip permutations and the fixed seed.
RESAMPLES = 10_000
PERMUTATIONS = 10_000
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
    """One side of a comparison: per-question mean score, and per-replicate scores."""

    label: str
    #: query_id -> mean score over the replicates it appears in (an unscored row is 0)
    score: dict[str, float] = field(default_factory=dict)
    #: query_id -> category label (``type_label``)
    category: dict[str, str] = field(default_factory=dict)
    replicates: int = 0
    dirs: list[str] = field(default_factory=list)
    #: replicate key -> query_id -> that replicate's score (an unscored row is 0)
    per_rep: dict[str, dict[str, float]] = field(default_factory=dict)
    #: rows (over all replicates, after resume merging) that were not scored
    errors: int = 0


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


def _part_order(run_dir: Path) -> tuple[int, str]:
    """Merge order within a replicate: the original, then chunks, then resumes."""
    return part_order(run_dir.name)


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
    per_rep: dict[str, dict[str, float]] = {}
    errors = 0
    for key, group in sorted(groups.items()):
        merged: dict[str, dict[str, Any]] = {}
        for d in sorted(group, key=_part_order):  # original, then chunks, then resumes
            for row in _rows(d):
                merged[str(row["query_id"])] = row
        rep_scores: dict[str, float] = {}
        for qid, row in merged.items():
            category.setdefault(qid, str(row.get("type_label") or "?"))
            scored = _scored(row)
            value = float(row["score"]) if scored else 0.0
            errors += 0 if scored else 1
            rep_scores[qid] = value
            sums[qid] += value
            counts[qid] += 1
        per_rep[key] = rep_scores
    score = {q: sums[q] / counts[q] for q in sums}
    return Side(
        label=label or (dirs[0].name if dirs else "?"),
        score=score,
        category=category,
        replicates=len(groups),
        dirs=[d.name for d in dirs],
        per_rep=per_rep,
        errors=errors,
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value for ``b`` and ``c`` discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, float(Fraction(2 * tail, 1 << n)))


def permutation_p(
    diffs: Sequence[float],
    *,
    permutations: int = PERMUTATIONS,
    seed: int = SEED,
) -> float:
    """Two-sided paired sign-flip permutation p-value for the mean of ``diffs``.

    Under the null of no difference each paired difference is as likely to carry
    either sign, so the observed ``|sum|`` is compared with the ``|sum|`` of
    ``permutations`` random sign flips: ``(1 + #{flipped >= observed}) / (1 + flips)``.
    """
    values = np.asarray(diffs, dtype=float)
    n = len(values)
    if n == 0 or not np.any(values):
        return 1.0
    observed = abs(float(values.sum()))
    rng = np.random.default_rng(seed)
    hits = 0
    chunk = max(1, 2_000_000 // n)  # bound memory: chunk x n signs at a time
    for start in range(0, permutations, chunk):
        stop = min(permutations, start + chunk)
        signs = rng.integers(0, 2, size=(stop - start, n)) * 2 - 1
        flipped = np.abs(signs @ values)
        hits += int(np.count_nonzero(flipped >= observed - 1e-9))
    return (1 + hits) / (1 + permutations)


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


def _right(score: float) -> bool:
    return score > 0.5


@dataclass
class Cell:
    """One comparison (overall or one category)."""

    n: int
    acc_a: float
    acc_b: float
    delta: float
    ci: tuple[float, float]
    #: discordant counts (A right & B wrong, A wrong & B right): the McNemar counts for
    #: single-run sides; with repeats, the (min, max) over the matched repeat pairs
    b: int | tuple[int, int]
    c: int | tuple[int, int]
    #: sign-flip permutation p-value (the reported test)
    p: float
    #: exact McNemar p when both sides have one run per question, else None
    p_mcnemar: float | None = None
    #: with repeats: (min, max) exact McNemar p over the matched repeat pairs
    mcnemar_range: tuple[float, float] | None = None
    #: matched repeat pairs behind the McNemar figures
    n_pairs: int = 1

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
    #: unscored (error / unattempted) rows on each side, scored as 0
    errors: tuple[int, int] = (0, 0)


def repeat_pairs(a: Side, b: Side) -> list[tuple[str, str]]:
    """Matched (A replicate, B replicate) pairs for the per-pair McNemar.

    A single-run side is paired with every replicate of the other; two repeated
    sides are paired replicate by replicate in key order (the shorter list wins).
    """
    keys_a, keys_b = sorted(a.per_rep), sorted(b.per_rep)
    if not keys_a or not keys_b:
        return []
    if len(keys_a) == 1 or len(keys_b) == 1:
        return [(ka, kb) for ka in keys_a for kb in keys_b]
    return list(zip(keys_a, keys_b, strict=False))


def _discordant(ra: dict[str, float], rb: dict[str, float], qids: Sequence[str]) -> tuple[int, int]:
    n_b = n_c = 0
    for q in qids:
        if q not in ra or q not in rb:
            continue
        right_a, right_b = _right(ra[q]), _right(rb[q])
        if right_a and not right_b:
            n_b += 1
        elif right_b and not right_a:
            n_c += 1
    return n_b, n_c


def _cell(
    a: Side, b: Side, qids: Sequence[str], resamples: int, seed: int, permutations: int
) -> Cell:
    diffs = [b.score[q] - a.score[q] for q in qids]
    n = len(qids)
    low, high = bootstrap_ci(diffs, resamples=resamples, seed=seed)
    pairs = repeat_pairs(a, b)
    counts = [_discordant(a.per_rep[ka], b.per_rep[kb], qids) for ka, kb in pairs]
    pvals = [mcnemar_exact(nb, nc) for nb, nc in counts]
    single = a.replicates == 1 and b.replicates == 1 and len(counts) == 1
    disc_b: int | tuple[int, int]
    disc_c: int | tuple[int, int]
    if single:
        disc_b, disc_c = counts[0]
    elif counts:
        disc_b = (min(x for x, _ in counts), max(x for x, _ in counts))
        disc_c = (min(y for _, y in counts), max(y for _, y in counts))
    else:
        disc_b = disc_c = 0
    return Cell(
        n=n,
        acc_a=100 * sum(a.score[q] for q in qids) / max(1, n),
        acc_b=100 * sum(b.score[q] for q in qids) / max(1, n),
        delta=100 * sum(diffs) / max(1, n),
        ci=(100 * low, 100 * high),
        b=disc_b,
        c=disc_c,
        p=permutation_p(diffs, permutations=permutations, seed=seed),
        p_mcnemar=pvals[0] if single else None,
        mcnemar_range=None if single or not pvals else (min(pvals), max(pvals)),
        n_pairs=len(pairs),
    )


def compare(
    a: Side,
    b: Side,
    *,
    resamples: int = RESAMPLES,
    seed: int = SEED,
    permutations: int = PERMUTATIONS,
) -> PairResult:
    """Paired comparison of ``b`` against ``a`` on their shared questions."""
    shared = sorted(set(a.score) & set(b.score))
    by_cat: dict[str, list[str]] = defaultdict(list)
    for q in shared:
        by_cat[a.category.get(q) or b.category.get(q) or "?"].append(q)
    return PairResult(
        a=a.label,
        b=b.label,
        overall=_cell(a, b, shared, resamples, seed, permutations),
        by_category={
            c: _cell(a, b, qs, resamples, seed, permutations) for c, qs in sorted(by_cat.items())
        },
        replicates=(a.replicates, b.replicates),
        n_only_a=len(set(a.score) - set(b.score)),
        n_only_b=len(set(b.score) - set(a.score)),
        errors=(a.errors, b.errors),
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


def _count(value: int | tuple[int, int]) -> str:
    if isinstance(value, tuple):
        return str(value[0]) if value[0] == value[1] else f"{value[0]}-{value[1]}"
    return str(value)


def _mcnemar(cell: Cell) -> str:
    if cell.p_mcnemar is not None:
        return _p(cell.p_mcnemar)
    if cell.mcnemar_range is None:
        return ""
    low, high = cell.mcnemar_range
    return f"{_p(low)}-{_p(high)} ({cell.n_pairs} pairs)"


def render_markdown(
    results: Sequence[PairResult],
    *,
    resamples: int,
    seed: int,
    permutations: int = PERMUTATIONS,
) -> str:
    lines = [
        f"Paired bootstrap: {resamples:,} resamples, seed {seed}, percentile 95% CI. "
        f"p: two-sided paired sign-flip permutation test on the per-question mean-score "
        f"differences ({permutations:,} flips, seed {seed}). McNemar: exact two-sided "
        "binomial on discordant questions; with repeats, per matched repeat pair, as a "
        "range (discordant counts likewise). Error rows score 0 on every side. "
        "Δ = B - A in points. **Bold** = CI excludes 0 and p < 0.05; "
        "p (Holm) adjusts the overall permutation p across the pairs in this table.",
        "",
        "| A | B | reps A/B | n | acc A | acc B | Δ | 95% CI | A✓B✗ / A✗B✓ | p | p (Holm) "
        "| McNemar p |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        o = r.overall
        delta = f"**{o.delta:+.1f}**" if o.significant else f"{o.delta:+.1f}"
        holm_p = _p(r.p_holm) if r.p_holm is not None else ""
        lines.append(
            f"| {r.a} | {r.b} | {r.replicates[0]}/{r.replicates[1]} | {o.n} "
            f"| {o.acc_a:.1f} | {o.acc_b:.1f} | {delta} | {_ci(o)} "
            f"| {_count(o.b)} / {_count(o.c)} | {_p(o.p)} | {holm_p} | {_mcnemar(o)} |"
        )
    cats = sorted({c for r in results for c in r.by_category})
    lines += [
        "",
        "Per category: Δ [95% CI] (permutation p). Not multiplicity-adjusted.",
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
    notes = [r for r in results if r.n_only_a or r.n_only_b or any(r.errors)]
    if notes:
        lines += ["", "Coverage notes:"]
        for r in notes:
            lines.append(
                f"- {r.a} vs {r.b}: {r.n_only_a} question(s) only in A, {r.n_only_b} only in B "
                f"(not compared); error rows scored 0: {r.errors[0]} in A, {r.errors[1]} in B "
                "(over all replicates)."
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
    ap.add_argument("--permutations", type=int, default=PERMUTATIONS)
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

    results = [
        compare(a, b, resamples=args.resamples, seed=args.seed, permutations=args.permutations)
        for a, b in comparisons
    ]
    for r, p in zip(results, holm([r.overall.p for r in results]), strict=True):
        r.p_holm = p
    table = render_markdown(
        results, resamples=args.resamples, seed=args.seed, permutations=args.permutations
    )
    print(table)
    if args.out:
        Path(args.out).write_text(table + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
