"""Rule-based sub-classification of wrong LoCoMo answers (temporal cat 2, single-hop cat 4).

    python failure_buckets.py --data data/locomo10.json --run runs/<prefix>--combo-A--memspine \
        [--categories 2,4] [--sample 15] [--seed 7] [--json out.json]

Every wrong answer is first given its ``error_analysis.classify`` outcome
(``retrieval_miss`` / ``partial`` / ``read_fail``), then exactly one **bucket**, by the
first rule that matches, in this order:

1. ``refusal``: the answer declines or denies (``REFUSAL`` patterns: "I do not know",
   "not mentioned", "the context does not ...", "X did not ..." as the opening clause).
2. ``date_arithmetic``: the question asks for a duration or count of time units
   ("how long", "how many days / weeks / months / years").
3. Date questions (the question asks *when* / *which year* / *what date*, and the gold
   parses to a date interval, see :func:`parse_interval`):

   * ``wrong_granularity``: the answer's date interval overlaps the gold's, but at another
     granularity (a day for "the week before 3 July", a year for "May 2023", ...);
   * ``date_matches_gold``: the intervals overlap at the same granularity, yet the verdict
     is wrong (a judge or gold question; also flagged ``answer_contains_gold``-style);
   * ``wrong_absolute_date``: no overlap; the gap in days is recorded;
   * ``no_date_in_answer``: the answer gives no parseable date.
4. ``list_missing_item`` / ``list_extra_item``: the gold is a short list (two or more
   items of at most four words each, split on commas, semicolons and "and"; the gold
   has a comma or semicolon, or the question a plural cue such as "what activities");
   an item whose content words are all absent from the answer is missing; with none
   missing, the answer is taken to add items.
5. ``wrong_fact_dated_question`` (the question names a date) or ``wrong_fact``: the rest.

Independently of the bucket, **gold flags** mark a gold label that *looks* wrong. They
are leads for a human, never a decision:

* ``anchor_not_session_date``: a relative gold ("the week before 9 June 2023") whose
  anchor date is not within a day of any of its evidence turns' session dates;
* ``gold_after_conversation``: a past-tense "when did" gold dated after the last session;
* ``answer_contains_gold``: every content word of the gold is in the answer;
* ``gold_not_in_evidence``: under half of a non-date gold's content words occur in its
  evidence turns;
* ``evidence_id_missing``: an evidence turn id that is not in the conversation.

No model calls; the run files and the dataset are only read.
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import error_analysis as ea
from memspine_evals.datasets import LoCoMoDataset

__all__ = [
    "BUCKETS",
    "Failure",
    "bucket_failure",
    "gold_flags",
    "parse_interval",
    "run_buckets",
]

BUCKETS = (
    "refusal",
    "date_arithmetic",
    "wrong_granularity",
    "date_matches_gold",
    "wrong_absolute_date",
    "no_date_in_answer",
    "list_missing_item",
    "list_extra_item",
    "wrong_fact_dated_question",
    "wrong_fact",
)

REFUSAL = re.compile(
    r"\b(?:i|you) do(?: not|n't) know\b"
    r"|\bnot (?:mentioned|specified|stated|provided|clear|known)\b"
    r"|\bno (?:mention|specific information|information|record)\b"
    r"|\b(?:does|do|did) not (?:mention|specify|contain|say|state|indicate|include|provide)\b"
    r"|\bcannot (?:be )?determine"
    r"|\bthere is no\b",
    re.IGNORECASE,
)
#: "Melanie did not ..." as the answer's opening clause: a denial of the premise.
DENIAL = re.compile(r"^[A-Z][\w'.]*(?: and [A-Z][\w']*)? (?:did|does|has|had|was) not\b")
DURATION_Q = re.compile(
    r"\bhow long\b|\bhow many (?:days|weeks|months|years|hours)\b|\bhow old\b", re.IGNORECASE
)
DATE_Q = re.compile(
    r"^\s*when\b|\bwhat (?:date|day|year|month|time)\b|\bwhich (?:year|month|day|date)\b"
    r"|\bin which (?:year|month)\b",
    re.IGNORECASE,
)
PLURAL_Q = re.compile(
    r"\b(?:what|which) (?:\w+ ){0,2}(?:activities|books|games|habits|hobbies|things|items|"
    r"places|foods|dishes|sports|pets|movies|events|ways|types|kinds|instruments|countries|"
    r"cities|names|songs|bands|artists|classes|projects)\b",
    re.IGNORECASE,
)
PAST_Q = re.compile(r"^\s*when (?:did|was|were|had)\b", re.IGNORECASE)

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name} | {
    name.lower(): i for i, name in enumerate(calendar.month_abbr) if name
}
MONTHS["sept"] = 9
_MONTH = r"(?P<{0}>" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?"
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}

_ISO_DAY = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")
_ISO_MONTH = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})\b(?!-\d)")
_DMY = re.compile(r"\b(?P<d>\d{1,2})\s+" + _MONTH.format("mon") + r",?\s+(?P<y>\d{4})\b", re.I)
_MDY = re.compile(
    r"\b"
    + _MONTH.format("mon")
    + r"\s+(?P<d>\d{1,2})(?:\s*(?:-|\N{EN DASH}|and|to)\s*(?P<d2>\d{1,2}))?,?\s+(?P<y>\d{4})\b",
    re.I,
)
_MY = re.compile(r"\b" + _MONTH.format("mon") + r",?\s+(?P<y>\d{4})\b", re.I)
_YEAR = re.compile(r"\b(?P<y>(?:19|20)\d{2})\b")

#: Words that carry no content for the overlap rules.
STOP = frozenset(
    re.findall(
        r"\w+",
        "a an the of to in on at for and or but with by from as is are was were be been "
        "it its his her their them they he she i you we my your our this that these "
        "those some any about into over after before during up out not no yes do did "
        "does has have had very just also then than so",
    )
)


@dataclass(frozen=True)
class Interval:
    lo: date
    hi: date
    #: day | relative_day | range | month | year
    granularity: str

    @property
    def rank(self) -> int:
        span = (self.hi - self.lo).days
        if self.granularity == "year":
            return 3
        if self.granularity == "month":
            return 2
        return 0 if span == 0 else 1


_MONTH_TYPO = re.compile(
    r"\b((?:" + "|".join(n for n in calendar.month_abbr if n) + r")[a-z]{0,7})\s+(?=\d)", re.I
)


def _month_typo(match: re.Match[str]) -> str:
    word = match[1]
    full = calendar.month_name[MONTHS[word[:3].lower()]]
    prefix = min(4, len(full))
    if word.lower() in MONTHS or word[:prefix].lower() != full[:prefix].lower():
        return match[0]  # a real month, or a word that only starts like one ("Decided")
    return full + " "


def _clean(text: str) -> str:
    """Normalise the gold's typing slips: ``15April`` -> ``15 April``, a stray backtick,
    ordinals, and a misspelt month before a number (``Januarty 5`` -> ``January 5``)."""
    text = text.replace("`", " ")
    text = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    text = _MONTH_TYPO.sub(_month_typo, text)
    return re.sub(r"\s+", " ", text)


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _day_dates(text: str) -> list[tuple[int, date, date | None]]:
    """(position, date, optional range end) for every day-level date in ``text``."""
    out: list[tuple[int, date, date | None]] = []
    for m in _ISO_DAY.finditer(text):
        if (d := _safe(int(m["y"]), int(m["m"]), int(m["d"]))) is not None:
            out.append((m.start(), d, None))
    for m in _DMY.finditer(text):
        if (d := _safe(int(m["y"]), MONTHS[m["mon"].lower()], int(m["d"]))) is not None:
            out.append((m.start(), d, None))
    for m in _MDY.finditer(text):
        month = MONTHS[m["mon"].lower()]
        if (d := _safe(int(m["y"]), month, int(m["d"]))) is not None:
            end = _safe(int(m["y"]), month, int(m["d2"])) if m["d2"] else None
            out.append((m.start(), d, end))
    return sorted(out, key=lambda t: t[0])


def _before(text: str, pos: int) -> str:
    return text[max(0, pos - 40) : pos].lower()


def parse_interval(raw: str) -> Interval | None:
    """The date interval a gold label or an answer names, or ``None``.

    Day-level dates (ISO, ``7 May 2023``, ``May 7, 2023``) with modifiers in the 40
    characters before them: "between X and Y" / "from X to Y" (range), "few days before"
    (6 days before), "week(end) before" / "last week" (9 days before), "<weekday>
    before/after" (that weekday), "week after" / "few days after". Otherwise month-level
    (``May 2023``, ``2023-05``; "first / last week of", "end of", "beginning of") or a
    bare year.
    """
    text = _clean(raw)
    days = _day_dates(text)
    low = text.lower()
    if days:
        pos, anchor, end = days[0]
        if len(days) >= 2 and re.search(r"\b(?:between|from)\b", low[: days[1][0]]):
            lo, hi = sorted((anchor, days[1][1]))
            return Interval(lo, hi, "range")
        if end is not None:
            lo, hi = sorted((anchor, end))
            return Interval(lo, hi, "range")
        ctx = _before(text, pos)
        for name, wd in WEEKDAYS.items():
            if hit := re.search(rf"\b{name}\s+(before|after)\s*$", ctx):
                step = -1 if hit[1] == "before" else 1
                day = anchor + timedelta(days=step)
                while day.weekday() != wd:
                    day += timedelta(days=step)
                return Interval(day, day, "relative_day")
        if re.search(r"few days (?:before|prior to)\s*$", ctx):
            return Interval(anchor - timedelta(days=6), anchor - timedelta(days=1), "range")
        if re.search(r"(?:week|weekend)\s+(?:before|prior to)\s*$|last (?:week|weekend)", ctx):
            return Interval(anchor - timedelta(days=9), anchor - timedelta(days=1), "range")
        if re.search(r"(?:week|weekend|few days)\s+after\s*$", ctx):
            return Interval(anchor + timedelta(days=1), anchor + timedelta(days=9), "range")
        return Interval(anchor, anchor, "day")
    months = [(m.start(), int(m["y"]), MONTHS[m["mon"].lower()]) for m in _MY.finditer(text)]
    months += [(m.start(), int(m["y"]), int(m["m"])) for m in _ISO_MONTH.finditer(text)]
    months = [t for t in sorted(months) if 1 <= t[2] <= 12]
    if months:
        pos, y, m = months[0]
        last = calendar.monthrange(y, m)[1]
        ctx = _before(text, pos)
        if re.search(r"first week of\s*$", ctx):
            return Interval(date(y, m, 1), date(y, m, 7), "range")
        if re.search(r"last two weeks of\s*$", ctx):
            return Interval(date(y, m, last - 14), date(y, m, last), "range")
        if re.search(r"(?:last week|end) of\s*$", ctx):
            return Interval(date(y, m, last - 7), date(y, m, last), "range")
        if re.search(r"(?:beginning|start|early)(?: of)?\s*$", ctx):
            return Interval(date(y, m, 1), date(y, m, 10), "range")
        if re.search(r"(?:few days|week|weekend)\s+before\s*$", ctx):
            first = date(y, m, 1)
            return Interval(first - timedelta(days=9), first - timedelta(days=1), "range")
        return Interval(date(y, m, 1), date(y, m, last), "month")
    years = _YEAR.search(text)
    if years:
        y = int(years["y"])
        return Interval(date(y, 1, 1), date(y, 12, 31), "year")
    return None


def _overlap(a: Interval, b: Interval) -> bool:
    return a.lo <= b.hi and b.lo <= a.hi


def _midpoint_gap(a: Interval, b: Interval) -> int:
    """Days between the intervals' midpoints (2021 vs 2022 is 365, not 1)."""
    mid_a = a.lo + (a.hi - a.lo) / 2
    mid_b = b.lo + (b.hi - b.lo) / 2
    return abs((mid_a - mid_b).days)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1}


def _list_items(question: str, gold: str) -> list[str]:
    """The gold's items when it is a short list, else ``[]``.

    A list needs a comma or semicolon in the gold, or a plural cue in the question
    (``PLURAL_Q``) for "a and b". "City, Country" (two parts, the second one capitalised
    word) is a place, not a list.
    """
    if not (re.search(r"[,;]", gold) or PLURAL_Q.search(question)):
        return []
    parts = [p.strip(" .\"'") for p in re.split(r",|;|&|\band\b", gold)]
    parts = [p for p in parts if p]
    if len(parts) == 2 and gold.count(",") == 1 and re.fullmatch(r"[A-Z][\w'-]*", parts[1]):
        return []
    if len(parts) >= 2 and all(len(p.split()) <= 4 for p in parts):
        return parts
    return []


@dataclass
class Failure:
    item_id: str
    query_id: str
    category: str
    outcome: str
    bucket: str
    question: str
    gold: str
    answer: str
    flags: list[str] = field(default_factory=list)
    detail: str = ""


def bucket_failure(question: str, gold: str, answer: str) -> tuple[str, str]:
    """``(bucket, detail)`` for one wrong answer (the rules in the module docstring)."""
    if REFUSAL.search(answer) or DENIAL.search(answer.strip()):
        return "refusal", ""
    if DURATION_Q.search(question):
        return "date_arithmetic", ""
    gold_iv = parse_interval(gold)
    if gold_iv is not None and DATE_Q.search(question):
        ans_iv = parse_interval(answer)
        if ans_iv is None:
            return "no_date_in_answer", f"gold {gold_iv.granularity}"
        if _overlap(gold_iv, ans_iv):
            if gold_iv.rank != ans_iv.rank:
                return (
                    "wrong_granularity",
                    f"gold {gold_iv.granularity}, answer {ans_iv.granularity}",
                )
            return "date_matches_gold", f"{gold_iv.granularity}"
        return "wrong_absolute_date", f"off by {_midpoint_gap(gold_iv, ans_iv)} d"
    items = _list_items(question, gold)
    if items:
        have = _words(answer)
        missing = [i for i in items if _words(i) and not (_words(i) & have)]
        if missing:
            return "list_missing_item", f"missing {missing}"
        return "list_extra_item", ""
    if parse_interval(question) is not None:
        return "wrong_fact_dated_question", ""
    return "wrong_fact", ""


def _session_date(stamp: str | None) -> date | None:
    if not stamp:
        return None
    iv = parse_interval(stamp)
    return iv.lo if iv is not None and iv.granularity == "day" else None


def gold_flags(
    question: str,
    gold: str,
    answer: str,
    evidence: Sequence[str],
    turns: Mapping[str, Any],
    last_session: date | None,
) -> list[str]:
    """Leads that the gold label may be wrong (see the module docstring); never a verdict."""
    flags: list[str] = []
    missing = [e for e in evidence if e not in turns]
    if missing:
        flags.append("evidence_id_missing")
    gold_iv = parse_interval(gold)
    days = _day_dates(_clean(gold))
    relative = bool(re.search(r"\b(?:before|after|week|weekend|few days)\b", gold, re.I))
    if gold_iv is not None and days and relative:
        anchor = days[0][1]
        sessions = {
            d for e in evidence if e in turns
            if (d := _session_date(getattr(turns[e], "timestamp", None))) is not None
        }  # fmt: skip
        if sessions and all(abs((anchor - s).days) > 1 for s in sessions):
            flags.append("anchor_not_session_date")
    if (
        gold_iv is not None
        and last_session is not None
        and PAST_Q.search(question)
        and gold_iv.lo > last_session + timedelta(days=1)
    ):
        flags.append("gold_after_conversation")
    gold_words = _words(gold)
    if gold_words and gold_words <= _words(answer):
        flags.append("answer_contains_gold")
    if gold_iv is None and gold_words:
        text = " ".join(str(getattr(turns[e], "text", "")) for e in evidence if e in turns)
        if text and len(gold_words & _words(text)) < len(gold_words) / 2:
            flags.append("gold_not_in_evidence")
    return flags


def run_buckets(run: Path, ds: LoCoMoDataset, categories: Iterable[int] = (2, 4)) -> list[Failure]:
    """Every wrong answer of ``run`` in ``categories``, bucketed and flagged."""
    labels = {f"cat{c}" for c in categories}
    gold_ids: dict[tuple[str, str], tuple[str, ...]] = {}
    turns: dict[str, dict[str, Any]] = {}
    last: dict[str, date | None] = {}
    session_days: dict[str, set[date]] = {}
    for item in ds.items():
        turns[item.item_id] = {t.turn_id: t for t in item.history}
        stamps = [d for t in item.history if (d := _session_date(t.timestamp)) is not None]
        last[item.item_id] = max(stamps) if stamps else None
        session_days[item.item_id] = set(stamps)
        for q in item.queries:
            gold_ids[(item.item_id, q.query_id)] = tuple(q.gold_turn_ids)
    out: list[Failure] = []
    for row in ea.load_rows(run):
        if row.get("type_label") not in labels or float(row["score"]) >= 1.0:
            continue
        key = (str(row["item_id"]), str(row["query_id"]))
        gold_turns = gold_ids.get(key, ())
        outcome = ea.classify(row, gold_turns)
        question, gold, answer = str(row["question"]), str(row["gold"]), str(row["answer"])
        bucket, detail = bucket_failure(question, gold, answer)
        if bucket == "wrong_absolute_date":
            ans_iv = parse_interval(answer)
            if ans_iv is not None and ans_iv.rank == 0 and ans_iv.lo in session_days[key[0]]:
                detail += "; answer is a session date"
        flags = gold_flags(question, gold, answer, gold_turns, turns[key[0]], last[key[0]])
        out.append(
            Failure(
                item_id=key[0],
                query_id=key[1],
                category=str(row["type_label"]),
                outcome=outcome,
                bucket=bucket,
                question=question,
                gold=gold,
                answer=answer,
                flags=flags,
                detail=detail,
            )
        )
    return out


# -- report ---------------------------------------------------------------------------


def _gap_band(detail: str) -> str:
    m = re.search(r"off by (\d+) d", detail)
    days = int(m[1]) if m else -1
    if days in (365, 366, 730, 731):
        return "whole years"
    if days <= 7:
        return "<=7 d"
    if days <= 31:
        return "8-31 d"
    return ">31 d"


def _short(text: str, n: int = 110) -> str:
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def render(failures: Sequence[Failure], *, sample: int, seed: int) -> str:
    lines: list[str] = []
    outcomes = ("read_fail", "retrieval_miss", "partial")
    for cat in sorted({f.category for f in failures}):
        rows = [f for f in failures if f.category == cat]
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for f in rows:
            counts[f.bucket][f.outcome] += 1
        lines += [
            f"### {cat}: {len(rows)} wrong answers",
            "",
            "| bucket | read failure | retrieval miss | partial | total |",
            "|---|---|---|---|---|",
        ]
        for b in BUCKETS:
            if counts.get(b):
                cells = [str(counts[b].get(o, 0)) for o in outcomes]
                lines.append(f"| {b} | " + " | ".join(cells) + f" | {sum(counts[b].values())} |")
        totals = [str(sum(1 for f in rows if f.outcome == o)) for o in outcomes]
        lines.append("| **all** | " + " | ".join(totals) + f" | {len(rows)} |")
        gaps = Counter(_gap_band(f.detail) for f in rows if f.bucket == "wrong_absolute_date")
        if gaps:
            on_session = sum("session date" in f.detail for f in rows)
            lines += ["", "wrong_absolute_date by gap between gold and answer midpoints: "
                      + ", ".join(f"{k} {v}" for k, v in sorted(gaps.items()))
                      + f"; the answer is a session date in {on_session}"]  # fmt: skip
        flags = Counter(flag for f in rows for flag in f.flags)
        if flags:
            lines += ["", "Gold flags (leads, not decisions): "
                      + ", ".join(f"`{k}` {v}" for k, v in flags.most_common())]  # fmt: skip
        lines.append("")
    rng = random.Random(seed)
    lines.append(f"### Samples (up to {sample} per bucket, seed {seed})")
    for cat in sorted({f.category for f in failures}):
        for b in BUCKETS:
            pool = [f for f in failures if f.category == cat and f.bucket == b]
            if not pool:
                continue
            picked = sorted(rng.sample(pool, min(sample, len(pool))), key=lambda f: f.query_id)
            lines += [
                "",
                f"**{cat} / {b}** ({len(pool)})",
                "",
                "| q | outcome | question | gold | answer | detail / flags |",
                "|---|---|---|---|---|---|",
            ]
            for f in picked:
                note = "; ".join(x for x in (f.detail, ", ".join(f.flags)) if x)
                lines.append(
                    f"| {f.item_id}/{f.query_id} | {f.outcome} | {_short(f.question, 80)} "
                    f"| {_short(f.gold, 60)} | {_short(f.answer)} | {_short(note, 60)} |"
                )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, help="run dir with results.jsonl")
    ap.add_argument("--data", required=True, help="locomo10.json")
    ap.add_argument("--categories", default="2,4")
    ap.add_argument("--sample", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", default=None, help="also write every bucketed failure here")
    ap.add_argument("--out", default=None, help="also write the Markdown report here")
    args = ap.parse_args(argv)
    ds = LoCoMoDataset(args.data, revision_id="auto")
    cats = tuple(int(c) for c in args.categories.split(","))
    failures = run_buckets(Path(args.run), ds, cats)
    report = render(failures, sample=args.sample, seed=args.seed)
    print(report)
    if args.out:
        Path(args.out).write_text(report + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(
            json.dumps([asdict(f) for f in failures], indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
