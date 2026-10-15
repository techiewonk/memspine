"""I16: measured noise floor from two same-config runs (replaces the 0.328*sqrt(n) estimate).

    python evals/noise_floor.py --a runs/ref-r1--memspine --b runs/ref-r2--memspine [--out nf.json]
    python evals/noise_floor.py --a runs/ref--memspine --b runs/ref--r1--memspine --by-item

Two runs of the SAME configuration differ only by run-to-run noise (sampling, GPU
non-determinism, judge variance). Per question the paired score difference is observed, so the
noise of a comparison on that slice can be measured instead of assumed:

* ``flips`` = discordant questions (right in one run, wrong in the other); ``flip_rate`` = flips / n.
* Under no real difference the net flip count ``b - c`` has variance ``b + c``; the estimator
  reports ``sd_net_flips`` as the bootstrap SD of the net count (resampling questions with
  replacement, or whole items with ``--by-item`` to respect within-conversation correlation).
* ``coef`` = sqrt(flip_rate): the measured replacement for the 0.328 in ``0.328*sqrt(n)``
  (noise in questions at size n' is about ``coef * sqrt(n')``; the 95% band is 1.96x that).
* ``band95_questions`` / ``band95_points`` = 1.96 * sd_net_flips (in questions / accuracy points):
  a difference between two configs smaller than this on this slice is not distinguishable
  from re-running the same config.

Scores are binary-ised as in ``significance.py`` (> 0.5 is right). Unscored rows count 0.
Only questions present in both runs are used; a category breakdown is included.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

LEGACY_COEF = 0.328  # the A16-based estimate this tool replaces
RESAMPLES = 10_000
SEED = 20261010


def paired_noise(
    a: Mapping[str, float],
    b: Mapping[str, float],
    *,
    groups: Mapping[str, str] | None = None,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> dict[str, Any]:
    """Noise-floor statistics for two same-config score maps (question id -> score)."""
    qids = sorted(set(a) & set(b))
    n = len(qids)
    if n == 0:
        return {"n": 0}
    ra = np.array([a[q] > 0.5 for q in qids], dtype=float)
    rb = np.array([b[q] > 0.5 for q in qids], dtype=float)
    d = rb - ra  # +1: only run b right, -1: only run a right
    flips = int(np.abs(d).sum())
    rng = np.random.default_rng(seed)
    if groups is not None:
        keys = sorted({groups.get(q, q) for q in qids})
        index = {k: i for i, k in enumerate(keys)}
        gid = np.array([index[groups.get(q, q)] for q in qids])
        sums = np.bincount(gid, weights=d, minlength=len(keys))
        draw = rng.integers(0, len(keys), size=(resamples, len(keys)))
        net = sums[draw].sum(axis=1)
    else:
        net = np.empty(resamples)
        chunk = max(1, 2_000_000 // n)
        for s in range(0, resamples, chunk):
            e = min(resamples, s + chunk)
            net[s:e] = d[rng.integers(0, n, size=(e - s, n))].sum(axis=1)
    sd_net = float(net.std(ddof=1))
    flip_rate = flips / n
    coef = math.sqrt(flip_rate)
    return {
        "n": n,
        "acc_a": float(ra.mean()),
        "acc_b": float(rb.mean()),
        "delta_points": float(100 * (rb.mean() - ra.mean())),
        "flips": flips,
        "only_a": int((d < 0).sum()),
        "only_b": int((d > 0).sum()),
        "flip_rate": flip_rate,
        "sd_net_flips": sd_net,
        "band95_questions": 1.96 * sd_net,
        "band95_points": 100 * 1.96 * sd_net / n,
        "coef": coef,
        "legacy_coef": LEGACY_COEF,
        "legacy_band_questions": LEGACY_COEF * math.sqrt(n),
        "cluster": "item" if groups is not None else "question",
        "n_clusters": len(keys) if groups is not None else n,
        "warning": ("fewer than 5 items: the item bootstrap is unreliable, use question resampling"
                    if groups is not None and len(keys) < 5 else None),
        "resamples": resamples,
        "note": "noise in questions at size n' ~ coef * sqrt(n'); 95% band = 1.96x",
    }


def load_scores(run_dirs: Sequence[Path]) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    """(score by key, category by key, item by key); key = ``item|query`` (query ids repeat across items)."""
    from significance import _rows, _scored  # type: ignore[import-not-found]

    score: dict[str, float] = {}
    cat: dict[str, str] = {}
    item: dict[str, str] = {}
    for d in run_dirs:
        for row in _rows(d):
            k = f"{row.get('item_id')}|{row['query_id']}"
            score[k] = float(row["score"]) if _scored(row) else 0.0
            cat[k] = str(row.get("type_label") or "?")
            item[k] = str(row.get("item_id"))
    return score, cat, item


def estimate(a_dirs: Sequence[Path], b_dirs: Sequence[Path], *, by_item: bool = False) -> dict[str, Any]:
    sa, cat, item = load_scores(a_dirs)
    sb, cat_b, item_b = load_scores(b_dirs)
    cat = {**cat_b, **cat}
    item = {**item_b, **item}
    out = {
        "a": [str(p) for p in a_dirs],
        "b": [str(p) for p in b_dirs],
        "overall": paired_noise(sa, sb, groups=item if by_item else None),
        "by_category": {},
    }
    by_cat: dict[str, list[str]] = defaultdict(list)
    for k in set(sa) & set(sb):
        by_cat[cat.get(k, "?")].append(k)
    for c, keys in sorted(by_cat.items()):
        out["by_category"][c] = paired_noise(
            {k: sa[k] for k in keys}, {k: sb[k] for k in keys}, groups={k: item[k] for k in keys} if by_item else None
        )
    return out


def render(res: Mapping[str, Any]) -> str:
    o = res["overall"]
    if not o.get("n"):
        return "no common questions"
    lines = [
        *([f"WARNING: {o['warning']}"] if o.get("warning") else []),
        f"n={o['n']}  acc A {o['acc_a']:.1%}  acc B {o['acc_b']:.1%}  delta {o['delta_points']:+.2f} pts",
        f"flips {o['flips']} ({o['flip_rate']:.1%}): only-A {o['only_a']}, only-B {o['only_b']}",
        f"measured coef sqrt(flip_rate) = {o['coef']:.3f}  (legacy {o['legacy_coef']})",
        f"net-flip SD {o['sd_net_flips']:.2f} q; 95% noise band +-{o['band95_questions']:.1f} q "
        f"(+-{o['band95_points']:.2f} pts); legacy band +-{o['legacy_band_questions']:.1f} q",
        "",
        "| category | n | flips | coef | 95% band (q) | 95% band (pts) |",
        "|---|---|---|---|---|---|",
    ]
    for c, v in res["by_category"].items():
        lines.append(f"| {c} | {v['n']} | {v['flips']} | {v['coef']:.3f} | {v['band95_questions']:.1f} | {v['band95_points']:.2f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", action="append", required=True, type=Path, help="run dir of the first run (repeat for chunks/resumes)")
    ap.add_argument("--b", action="append", required=True, type=Path, help="run dir of the repeat run")
    ap.add_argument("--by-item", action="store_true", help="bootstrap whole items (conversations/users), not questions")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    res = estimate(args.a, args.b, by_item=args.by_item)
    print(render(res))
    if args.out:
        args.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
