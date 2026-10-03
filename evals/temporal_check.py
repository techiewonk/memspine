"""Offline validation of H1 (relative-date resolution) on LoCoMo temporal questions. No model calls.

For each cat-2 question: the gold answer is turned into a date range. It is either absolute ("7 May
2023", "May 2023", "2022") or relative to a stated date ("The Friday before 15 July 2023", which is
resolved as "last Friday" on 15 July). Each evidence turn's relative phrases are resolved
against the turn's session date. Reported (R4-4):

- coverage: questions whose evidence contains at least one resolved phrase, over ALL cat-2;
- overlap agreement: among covered questions, some resolution overlaps the gold range;
- exact-day agreement: among covered single-day golds, some resolution is exactly that day;
- exact-range agreement: among covered questions, some resolution equals the gold range.

The check is partly circular (gold ranges for relative golds are resolved with the same
resolver), so overlap agreement is an upper bound and exact-day the stricter number.

    python temporal_check.py --data data/locomo10.json
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from typing import Any

from memspine_evals.datasets import LoCoMoDataset

from memspine.core.temporal_resolve import resolve

_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        1,
    )
}
_DMY = re.compile(r"(\d{1,2})\s+([A-Za-z]+),?\s+(\d{4})")
_MY = re.compile(r"\b([A-Za-z]+),?\s+(\d{4})\b")
_Y = re.compile(r"\b(19|20)\d{2}\b")
_REL_GOLD = re.compile(
    r"^\s*(?:on\s+)?(?:the\s+|a\s+|last\s+)?(?P<what>.+?)\s+(?P<dir>before|prior to|after)\s+"
    r"(?P<date>\d{1,2}\s+[A-Za-z]+,?\s+\d{4})",
    re.I,
)
_REL_MAP = {
    "day": "yesterday",
    "week": "last week",
    "weekend": "last weekend",
    "month": "last month",
    "year": "last year",
}


def parse_date(text: str) -> date | None:
    m = _DMY.search(text)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


def gold_range(gold: str) -> tuple[date, date] | None:
    rel = _REL_GOLD.search(gold)
    if rel:
        anchor = parse_date(rel.group("date"))
        what = rel.group("what").lower().strip()
        after = rel.group("dir").lower() == "after"
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        if after:
            phrase = (
                f"next {what}"
                if what in weekdays or what in ("week", "weekend", "month")
                else "tomorrow"
                if what == "day"
                else None
            )
        else:
            phrase = _REL_MAP.get(what) or (
                f"last {what}"
                if what in weekdays
                else f"{what} ago"
                if re.match(r"^(\w+) (day|week|month|year)s?$", what)
                else None
            )
        if anchor and phrase:
            rs = resolve(phrase, anchor)
            if rs:
                return rs[0].first, rs[0].last
        return None
    d = parse_date(gold)
    if d:
        return d, d
    m = _MY.search(gold)
    if m and m.group(1).lower() in _MONTHS:
        y, mo = int(m.group(2)), _MONTHS[m.group(1).lower()]
        last = date(y + (mo == 12), mo % 12 + 1, 1) - date.resolution
        return date(y, mo, 1), last
    y = _Y.search(gold)
    if y and gold.strip().isdigit():
        return date(int(y.group(0)), 1, 1), date(int(y.group(0)), 12, 31)
    return None


def session_date(ts: str | None) -> date | None:
    return parse_date(ts or "")


def evaluate(ds: LoCoMoDataset, show: int = 0) -> dict[str, Any]:
    """Score the resolver on every cat-2 question of ``ds`` (R4-4: two agreement metrics).

    - ``coverage`` = covered / all cat-2 questions (not / parsed), the share the check speaks for;
    - ``agree_overlap``: some resolution overlaps the gold range (lenient);
    - ``agree_exact_day``: the gold is one day and some resolution is exactly that day, over
      the covered questions whose gold is a single day (``n_single_day``);
    - ``agree_exact_range``: some resolution equals the gold range exactly, over covered.
    """
    n = parsed = covered = agree = single = exact = exact_range = 0
    misses = []
    for item in ds.items():
        turns = {t.turn_id: t for t in item.history}
        for q in item.queries:
            if q.type_label != "cat2":
                continue
            n += 1
            g = gold_range(str(q.gold))
            if g is None:
                continue
            parsed += 1
            res = []
            for tid in q.gold_turn_ids:
                t = turns.get(tid)
                anchor = session_date(t.timestamp) if t else None
                if t and anchor:
                    res += [(r, t.text) for r in resolve(t.text, anchor)]
            if not res:
                continue
            covered += 1
            if any(r.first <= g[1] and g[0] <= r.last for r, _ in res):
                agree += 1
            elif len(misses) < show:
                misses.append((q.text, q.gold, [(r.phrase, r.label) for r, _ in res]))
            if any(r.first == g[0] and r.last == g[1] for r, _ in res):
                exact_range += 1
            if g[0] == g[1]:
                single += 1
                if any(r.first == r.last == g[0] for r, _ in res):
                    exact += 1
    return {
        "n_cat2": n,
        "gold_parsed": parsed,
        "covered": covered,
        "coverage": covered / n if n else 0.0,
        "agree_overlap": agree,
        "agree_overlap_rate": agree / covered if covered else 0.0,
        "n_single_day": single,
        "agree_exact_day": exact,
        "agree_exact_day_rate": exact / single if single else 0.0,
        "agree_exact_range": exact_range,
        "agree_exact_range_rate": exact_range / covered if covered else 0.0,
        "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--show", type=int, default=0, help="print N disagreements")
    args = ap.parse_args()
    r = evaluate(LoCoMoDataset(args.data, revision_id="auto"), show=args.show)
    print(
        f"cat2 questions {r['n_cat2']}; gold parsed {r['gold_parsed']}; evidence has a "
        f"resolvable phrase {r['covered']} (coverage {100 * r['coverage']:.1f}% of all cat-2); "
        f"overlap agreement {r['agree_overlap']}/{r['covered']} = "
        f"{100 * r['agree_overlap_rate']:.1f}%; exact-day agreement {r['agree_exact_day']}/"
        f"{r['n_single_day']} single-day golds = {100 * r['agree_exact_day_rate']:.1f}%; "
        f"exact-range agreement {r['agree_exact_range']}/{r['covered']} = "
        f"{100 * r['agree_exact_range_rate']:.1f}%"
    )
    for q, gold, rs in r["misses"]:
        print(f"  Q: {q[:70]} | gold: {gold} | resolved: {rs}")


if __name__ == "__main__":
    main()
