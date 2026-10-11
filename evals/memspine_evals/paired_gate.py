"""G-E1: one paired adoption gate shared by the focus and blind scorers.

A verdict compares an ARM run with a REFERENCE run question by question (never with labels stored in a slice):

* every expected question must have a row in both runs, otherwise the verdict is ``INCOMPLETE``;
* no question flips -> ``NO CHANGE`` (an arm identical to its reference can never be promoted);
* ``net`` = questions gained - questions lost; ``band`` = 0.328 * sqrt(n) (the noise rule used since round 3);
* an exact two-sided sign test on the flipped questions, and a 95% bootstrap interval of ``net`` that resamples whole
  conversations (clusters), so one conversation cannot carry a result;
* every category is held to its own band: a category losing more than ``0.328 * sqrt(n_cat)`` rejects the arm.

``PROMOTE`` needs all of: net > band, sign-test p < 0.05, bootstrap lower bound > 0, no category beyond its band.
``REJECT``: net < -band or any category beyond its band. Anything else is ``HOLD``.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

BAND_K = 0.328
P_MAX = 0.05
BOOT_REPS = 2000
BOOT_SEED = 7


def band(n: int) -> float:
    return BAND_K * math.sqrt(max(n, 0))


def sign_test_p(gained: int, lost: int) -> float:
    """Exact two-sided binomial test of gained vs lost flips under p = 0.5."""
    n = gained + lost
    if n == 0:
        return 1.0
    k = min(gained, lost)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass
class PairedResult:
    n_expected: int
    n: int
    missing_arm: int
    missing_ref: int
    acc_arm: float
    acc_ref: float
    gained: int
    lost: int
    net: int
    band: float
    p_sign: float
    ci_low: float
    ci_high: float
    by_category: dict[str, dict] = field(default_factory=dict)
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)


def paired(
    arm: Mapping[str, bool],
    ref: Mapping[str, bool],
    expected: Iterable[str],
    *,
    cluster_of: Callable[[str], str] = lambda q: q.split("/", 1)[0],
    category_of: Callable[[str], str] | None = None,
) -> PairedResult:
    """Compare two runs given per-question correctness (``question id -> correct``) over ``expected`` ids."""
    exp = list(dict.fromkeys(expected))
    miss_a = [q for q in exp if q not in arm]
    miss_r = [q for q in exp if q not in ref]
    shared = [q for q in exp if q in arm and q in ref]
    n = len(shared)
    gained = [q for q in shared if arm[q] and not ref[q]]
    lost = [q for q in shared if ref[q] and not arm[q]]
    net = len(gained) - len(lost)

    # cluster bootstrap of net over conversations
    per_cluster: dict[str, int] = defaultdict(int)
    for q in gained:
        per_cluster[cluster_of(q)] += 1
    for q in lost:
        per_cluster[cluster_of(q)] -= 1
    clusters = sorted({cluster_of(q) for q in shared})
    lo = hi = float(net)
    if clusters:
        rng = random.Random(BOOT_SEED)
        draws = sorted(sum(per_cluster[rng.choice(clusters)] for _ in clusters) for _ in range(BOOT_REPS))
        lo, hi = float(draws[int(0.025 * BOOT_REPS)]), float(draws[int(0.975 * BOOT_REPS) - 1])

    by: dict[str, dict] = {}
    if category_of is not None:
        acc: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # n, gained, lost
        for q in shared:
            c = category_of(q)
            acc[c][0] += 1
        for q in gained:
            acc[category_of(q)][1] += 1
        for q in lost:
            acc[category_of(q)][2] += 1
        for c, (nc, g, lo_c) in sorted(acc.items()):
            by[c] = {"n": nc, "gained": g, "lost": lo_c, "net": g - lo_c, "band": band(nc),
                     "beyond_band": (g - lo_c) < -band(nc)}

    r = PairedResult(
        n_expected=len(exp), n=n, missing_arm=len(miss_a), missing_ref=len(miss_r),
        acc_arm=100 * sum(arm[q] for q in shared) / n if n else 0.0,
        acc_ref=100 * sum(ref[q] for q in shared) / n if n else 0.0,
        gained=len(gained), lost=len(lost), net=net, band=band(n),
        p_sign=sign_test_p(len(gained), len(lost)), ci_low=lo, ci_high=hi, by_category=by,
    )
    r.verdict, r.reasons = decide(r)
    return r


def decide(r: PairedResult) -> tuple[str, list[str]]:
    if r.missing_arm or r.missing_ref or r.n < r.n_expected or r.n == 0:
        return "INCOMPLETE", [f"rows missing: arm {r.missing_arm}, reference {r.missing_ref} of {r.n_expected}"]
    if r.gained == 0 and r.lost == 0:
        return "NO CHANGE", ["no question flipped"]
    bad_cats = [c for c, d in r.by_category.items() if d["beyond_band"]]
    if r.net < -r.band or bad_cats:
        why = [f"net {r.net:+d} below -{r.band:.1f}"] if r.net < -r.band else []
        return "REJECT", why + [f"category {c} lost beyond its band" for c in bad_cats]
    checks = {
        f"net {r.net:+d} > band {r.band:.1f}": r.net > r.band,
        f"sign test p {r.p_sign:.3f} < {P_MAX}": r.p_sign < P_MAX,
        f"cluster bootstrap low {r.ci_low:+.0f} > 0": r.ci_low > 0,
    }
    failed = [k for k, ok in checks.items() if not ok]
    return ("PROMOTE", list(checks)) if not failed else ("HOLD", [f"not met: {k}" for k in failed])


def format_result(name: str, r: PairedResult) -> list[str]:
    out = [f"{name}: n={r.n}/{r.n_expected}  arm {r.acc_arm:.1f}% vs ref {r.acc_ref:.1f}%  "
           f"+{r.gained}/-{r.lost} net {r.net:+d} (band +-{r.band:.1f})  sign p={r.p_sign:.3f}  "
           f"cluster 95% [{r.ci_low:+.0f}, {r.ci_high:+.0f}]"]
    for c, d in r.by_category.items():
        flag = "  BEYOND BAND" if d["beyond_band"] else ""
        out.append(f"   {c:6s} n={d['n']:4d} +{d['gained']}/-{d['lost']} net {d['net']:+d} (band +-{d['band']:.1f}){flag}")
    out.append(f"   verdict: {r.verdict}  ({'; '.join(r.reasons)})")
    return out
