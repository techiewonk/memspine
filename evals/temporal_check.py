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

With ``--run`` (repeatable), each QA run's recorded cat-2 verdicts are split by those
groups (covered / exact-day hit / not covered), no model calls and no re-judging: does the
reader do better where the resolver agrees with the gold?

G13, ``--anchored``: resolve with ``anchored=True`` (``read.relative_dates_anchored``) and
also report **gold phrasing**: a relative gold ("The week before 9 June 2023", "A few days
before May 24, 2023") states a relation and an anchor day; the evidence context contains
the gold phrasing when some resolution states the same relation (``week before``,
``weekend before``, ``friday before``, ``few days before``, ...) to the same day. With
``--run``, the run's wrong absolute dates within 7 days of the gold
(``failure_buckets``) are listed with both renderings, and counted: how many get the gold
phrasing, and how many have a resolved span overlapping the gold interval
(``failure_buckets.parse_interval``), calendar versus anchored.

    python temporal_check.py --data data/locomo10.json [--anchored] [--run RUN_DIR ...]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import failure_buckets as fb
from memspine_evals.datasets import LoCoMoDataset

from memspine.core.temporal_resolve import Resolution, annotate, resolve

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


def relation_key(text: str) -> tuple[str, date] | None:
    """``("week before", 2023-06-09)`` for "The week before 9 June 2023" or a G13 relation
    "the week before 2023-06-09"; ``None`` without a relation word before the first day.

    Leading "on", "the", "a", "an" and "last" are dropped ("Last week before 13 October
    2022" and "a week before 24 August" are both ``week before``)."""
    clean = fb._clean(text)
    days = fb._day_dates(clean)
    if not days:
        return None
    pos, anchor, _ = days[0]
    words = re.findall(r"[a-z]+", clean[:pos].lower())
    while words and words[0] in ("on", "the", "a", "an", "last"):
        words.pop(0)
    if len(words) < 2 or words[-1] not in ("before", "after", "of"):
        return None
    return " ".join(words), anchor


def has_gold_phrasing(gold: str, resolutions: list[Resolution]) -> bool:
    """Some resolution states the gold's relation to the gold's anchor day."""
    key = relation_key(gold)
    return key is not None and any(relation_key(r.relation) == key for r in resolutions)


def gold_range(gold: str, anchored: bool = False) -> tuple[date, date] | None:
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
            rs = resolve(phrase, anchor, anchored=anchored)
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


def evidence_resolutions(
    q: Any, turns: dict[str, Any], anchored: bool = False
) -> list[tuple[Resolution, str]]:
    """Every resolution in ``q``'s evidence turns, each against its turn's session date."""
    res = []
    for tid in q.gold_turn_ids:
        t = turns.get(tid)
        anchor = session_date(t.timestamp) if t else None
        if t and anchor:
            res += [(r, t.text) for r in resolve(t.text, anchor, anchored=anchored)]
    return res


def evaluate(ds: LoCoMoDataset, show: int = 0, anchored: bool = False) -> dict[str, Any]:
    """Score the resolver on every cat-2 question of ``ds`` (R4-4: two agreement metrics).

    - ``coverage`` = covered / all cat-2 questions (not / parsed), the share the check speaks for;
    - ``agree_overlap``: some resolution overlaps the gold range (lenient);
    - ``agree_exact_day``: the gold is one day and some resolution is exactly that day, over
      the covered questions whose gold is a single day (``n_single_day``);
    - ``agree_exact_range``: some resolution equals the gold range exactly, over covered;
    - ``gold_phrasing`` (G13): over all cat-2 questions with a relative gold
      (``n_relative_gold``), the evidence states the gold's relation to its anchor day
      (:func:`has_gold_phrasing`; only ``anchored`` resolutions state relations).
    """
    n = parsed = covered = agree = single = exact = exact_range = relative = phrased = 0
    misses = []
    groups: dict[str, set[tuple[str, str]]] = {
        "covered": set(),
        "exact_day": set(),
        "all": set(),
        "phrased": set(),
    }
    for item in ds.items():
        turns = {t.turn_id: t for t in item.history}
        for q in item.queries:
            if q.type_label != "cat2":
                continue
            n += 1
            groups["all"].add((item.item_id, q.query_id))
            if relation_key(str(q.gold)) is not None:
                relative += 1
                found = [r for r, _ in evidence_resolutions(q, turns, anchored)]
                if has_gold_phrasing(str(q.gold), found):
                    phrased += 1
                    groups["phrased"].add((item.item_id, q.query_id))
            g = gold_range(str(q.gold), anchored)
            if g is None:
                continue
            parsed += 1
            res = evidence_resolutions(q, turns, anchored)
            if not res:
                continue
            covered += 1
            groups["covered"].add((item.item_id, q.query_id))
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
                    groups["exact_day"].add((item.item_id, q.query_id))
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
        "n_relative_gold": relative,
        "gold_phrasing": phrased,
        "misses": misses,
        "groups": groups,
    }


def near_misses(run: Path, ds: LoCoMoDataset) -> dict[str, Any]:
    """G13: the run's wrong absolute dates within 7 days of the gold, re-resolved offline.

    For each, the evidence turns are resolved calendar-style and anchored. Counted: the
    near misses whose anchored evidence has the gold phrasing (:func:`has_gold_phrasing`),
    and, per mode, those with a resolved span overlapping the gold interval
    (``failure_buckets.parse_interval``). Nothing is re-scored.
    """
    items = {item.item_id: item for item in ds.items()}
    rows = []
    for f in fb.run_buckets(run, ds, (2,)):
        if f.bucket != "wrong_absolute_date" or fb._gap_band(f.detail) != "<=7 d":
            continue
        item = items[f.item_id]
        q = next(q for q in item.queries if q.query_id == f.query_id)
        turns = {t.turn_id: t for t in item.history}
        gold_iv = fb.parse_interval(f.gold)
        row: dict[str, Any] = {"query": f"{f.item_id}/{f.query_id}", "question": f.question}
        row.update(gold=f.gold, answer=f.answer, detail=f.detail)
        for mode, anchored in (("calendar", False), ("anchored", True)):
            res = evidence_resolutions(q, turns, anchored)
            row[f"{mode}_resolved"] = [f"{r.phrase} [= {r.label}]" for r, _ in res]
            row[f"{mode}_overlap"] = gold_iv is not None and any(
                r.first <= gold_iv.hi and gold_iv.lo <= r.last for r, _ in res
            )
        found = [r for r, _ in evidence_resolutions(q, turns, True)]
        row["gold_phrasing"] = has_gold_phrasing(f.gold, found)
        row["evidence"] = [
            annotate(turns[t].text, d, anchored=True)
            for t in q.gold_turn_ids
            if t in turns and (d := session_date(turns[t].timestamp)) is not None
        ]
        rows.append(row)
    return {
        "n": len(rows),
        "with_resolution": sum(bool(r["anchored_resolved"]) for r in rows),
        "gold_phrasing": sum(r["gold_phrasing"] for r in rows),
        "overlap_calendar": sum(r["calendar_overlap"] for r in rows),
        "overlap_anchored": sum(r["anchored_overlap"] for r in rows),
        "rows": rows,
    }


def run_split(run: Path, groups: dict[str, set[tuple[str, str]]]) -> dict[str, tuple[float, int]]:
    """A QA run's recorded cat-2 accuracy on resolver-covered, exact-day and uncovered questions."""
    scores: dict[tuple[str, str], float] = {}
    for line in (run / "results.jsonl").read_text("utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") == "result" and row.get("status") in ("completed", "truncated"):
            scores[(row["item_id"], row["query_id"])] = float(row["score"])
    split = {
        "covered": groups["covered"],
        "exact_day": groups["exact_day"],
        "not_covered": groups["all"] - groups["covered"],
    }
    out = {}
    for name, keys in split.items():
        vals = [scores[k] for k in keys if k in scores]
        out[name] = (sum(vals) / len(vals) if vals else 0.0, len(vals))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--show", type=int, default=0, help="print N disagreements")
    ap.add_argument("--run", action="append", default=[], help="QA run dir (repeatable)")
    ap.add_argument(
        "--anchored", action="store_true", help="G13: resolve as read.relative_dates_anchored"
    )
    args = ap.parse_args()
    ds = LoCoMoDataset(args.data, revision_id="auto")
    r = evaluate(ds, show=args.show, anchored=args.anchored)
    print(
        f"cat2 questions {r['n_cat2']}; gold parsed {r['gold_parsed']}; evidence has a "
        f"resolvable phrase {r['covered']} (coverage {100 * r['coverage']:.1f}% of all cat-2); "
        f"overlap agreement {r['agree_overlap']}/{r['covered']} = "
        f"{100 * r['agree_overlap_rate']:.1f}%; exact-day agreement {r['agree_exact_day']}/"
        f"{r['n_single_day']} single-day golds = {100 * r['agree_exact_day_rate']:.1f}%; "
        f"exact-range agreement {r['agree_exact_range']}/{r['covered']} = "
        f"{100 * r['agree_exact_range_rate']:.1f}%"
    )
    print(
        f"relative golds {r['n_relative_gold']}; evidence has the gold phrasing "
        f"{r['gold_phrasing']}/{r['n_relative_gold']}"
        + ("" if args.anchored else " (calendar mode states no relations; see --anchored)")
    )
    for q, gold, rs in r["misses"]:
        print(f"  Q: {q[:70]} | gold: {gold} | resolved: {rs}")
    for run in args.run:
        split = run_split(Path(run), r["groups"])
        cells = "; ".join(f"{k} {100 * a:.1f}% (n={n})" for k, (a, n) in split.items())
        print(f"{Path(run).name}: cat-2 accuracy {cells}")
        if args.anchored:
            nm = near_misses(Path(run), ds)
            print(
                f"  wrong dates within 7 d: {nm['n']}; evidence has a resolvable phrase "
                f"{nm['with_resolution']}; anchored gives the gold phrasing {nm['gold_phrasing']}; "
                f"a span overlaps the gold: calendar {nm['overlap_calendar']}, "
                f"anchored {nm['overlap_anchored']}"
            )
            for row in nm["rows"]:
                print(
                    f"  - {row['query']} | {row['question'][:60]} | gold: {row['gold']} | "
                    f"calendar: {row['calendar_resolved']} | anchored: {row['anchored_resolved']}"
                )


if __name__ == "__main__":
    main()
