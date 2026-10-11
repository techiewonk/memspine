"""I58: the judge convention layer (``--judge-conventions``).

The 9B LLM judge rejects answers that are right under the conventions the hand-labelled
calibration set uses (``analysis/judge_calibration_dev.jsonl``): a list that holds every gold
item plus other items, a one-letter typo in a proper noun, a number written as a word. Making
the judge lenient would inflate the metric silently, so the layer is explicit, documented and
deterministic, and it is *reported next to* the unchanged judge, never instead of it:

* the row's ``score`` is the LLM judge's verdict, exactly as without the flag;
* the layer adds ``meta["score_conventions"]`` (1.0 when the judge said correct or a
  convention applies) and ``meta["convention"]`` (the rule). Every run therefore has two
  columns: ``score`` and ``score_conventions`` (``evals/rescore_conventions.py`` computes both
  offline for runs made without the flag, ``forensics_report.py`` prints them).

The conventions (each can only turn a wrong verdict into a right one, never the reverse):

``numeral``
    The gold is a number (digits, a number word, ``twice``/``once``/``thrice``, ``N times``)
    and the first numeral of the answer is the same number ("five" = 5). Dates and years are
    ignored; ``at least``, ``about``, ``more than`` and the like block it. A gold that mixes
    words and a number ("four months") is credited when the answer holds the same words with
    the numerals normalised ("4 months"). Comparatives (``at least`` ...) block it too.
``typo``
    The gold is a proper noun phrase (every content word capitalised) and every gold word is
    in the answer, exactly or with one edit (insertion, deletion, transposition; a
    substitution only for words of 7 letters or more, so "Hanna" never matches "Hanno"). Words
    of fewer than 5 letters must match exactly, and an answer word that the question itself
    spells differently is never taken for a typo.
``list_superset``
    The gold is a list of two or more items and EVERY item is in the answer (an item is in when
    all its content words are, with plural/-ing/-ed folded and the ``typo`` rule for proper
    nouns), so the extras are what is left over. The extras must not be contradicted: any
    negation ("not", "never", "n't"), restriction ("only", "except", "but not") or refusal in the
    answer blocks the credit. A list that misses an item is never credited (partial lists stay
    wrong).

Not covered by design: synonyms and paraphrase (no deterministic test exists; the LLM judge
decides), and any gold that is a free-text sentence. The layer never reads the category, the
dataset or the run; it sees the question, the answer and the gold. Abstention golds
("Not mentioned...") are never touched.

False-positive risk, measured on the calibration set (``evals/judge_agreement.py
--conventions``): see ``analysis/GAP_REGISTER.md`` I58. The remaining risk is a superset whose
extras are wrong without a negation word (the layer cannot check an extra's truth); the
reported credits are listed so they can be read by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CONVENTIONS_VERSION",
    "ConventionResult",
    "CONVENTIONS_V2_VERSION",
    "check_conventions",
    "check_conventions_v2",
    "edit_close",
    "gold_items",
]

CONVENTIONS_VERSION = "conventions/v1"

_WORD = re.compile(r"[A-Za-z0-9']+")
_STOP = frozenset(
    "the a an of to in on at and or for with by from as is was were that this it its his her "
    "their my our be been being has have had".split()
)
_NEGATION = re.compile(
    r"\b(never|not|no|none|neither|nor|didn't|doesn't|wasn't|isn't|weren't|aren't|don't|"
    r"cannot|can't|couldn't|won't|unknown|only|just|except|excluding|without|other than|"
    r"in addition to|besides|apart from|aside from|rather than|instead of|beyond)\b|n't\b",
    re.I,
)
_COMPARATIVE = re.compile(
    r"\b(at least|at most|more than|fewer than|less than|over|under|up to|between|"
    r"nearly|almost)\b",
    re.I,
)
_HEDGE = re.compile(r"\b(about|around|approximately|roughly|some|several|many)\b", re.I)
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b|\b(?:19|20)\d\d\b",
    re.I,
)
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_NUMBER_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_NUMBER_WORDS.update({"once": 1, "twice": 2, "thrice": 3})
_NUM_TOKEN = re.compile(r"\b(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b", re.I)
_PURE_NUMBER = re.compile(
    r"^\W*(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")(?:\s+times?)?\W*$",
    re.I,
)
_REFUSAL = re.compile(
    r"\b(not mentioned|do(?:es)? not (?:know|mention|state)|no information|cannot be determined)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class ConventionResult:
    rule: str
    detail: str = ""


def _tokens(text: str) -> list[str]:
    return [t.lower().strip("'") for t in _WORD.findall(text)]


def _stem(w: str) -> str:
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def edit_close(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by one insertion, deletion or adjacent transposition, or
    by one substitution in words of 7 letters or more. Words under 5 letters never match fuzzily."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return len(a) >= 7
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _content(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _STOP]


def _proper_phrase(gold: str) -> bool:
    words = [w for w in _WORD.findall(gold) if w.lower() not in _STOP]
    return bool(words) and all(w[0].isupper() or w[0].isdigit() for w in words)


def _positions(
    word: str, tokens: list[str], proper: bool, question: set[str]
) -> tuple[str, list[int]] | None:
    """('exact' | 'fuzzy', token positions) when ``word`` is in the answer, else None."""
    stem = _stem(word)
    exact = [i for i, t in enumerate(tokens) if t == word or _stem(t) == stem]
    if exact:
        return "exact", exact
    if proper:
        fuzzy = [
            i
            for i, t in enumerate(tokens)
            # the question spells it this way: not a typo of the gold
            if not (t in question and t != word) and edit_close(word, t)
        ]
        if fuzzy:
            return "fuzzy", fuzzy
    return None


#: The words of one multi-word item must sit within this many tokens beyond their own count.
WINDOW_SLACK = 5


def _item_in(item: str, tokens: list[str], proper: bool, question: set[str]) -> str | None:
    """'exact' / 'fuzzy' when every content word of ``item`` is in the answer *close together*
    (the words of a phrase scattered over a long answer are not the phrase), else None."""
    words = _content(item)
    if not words:
        return None
    found = [_positions(w, tokens, proper, question) for w in words]
    if any(f is None for f in found):
        return None
    where = [f[1] for f in found if f is not None]
    kind = "fuzzy" if any(f[0] == "fuzzy" for f in found if f is not None) else "exact"
    if len(words) == 1:
        return kind
    best = None
    for start in where[0]:  # the smallest window holding one position of every word
        lo = hi = start
        for pos in where[1:]:
            near = min(pos, key=lambda p: abs(p - start))
            lo, hi = min(lo, near), max(hi, near)
        span = hi - lo + 1
        best = span if best is None else min(best, span)
    return kind if best is not None and best <= len(words) + WINDOW_SLACK else None


#: A gold that reads as a sentence (a subject pronoun or an auxiliary verb) is not a list.
_SENTENCE = re.compile(
    r"\b(?:she|he|they|it|we|i|had|has|have|was|were|is|are|did|does|got|went|took)\b", re.I
)


def gold_items(gold: str) -> list[str]:
    """The items of a list gold: split on commas, semicolons, "and", "&", "or"; two or more
    items, else ``[]``. A sentence, a gold holding a number ("two cats and a dog": the quantity
    would go unchecked) and a gold of over 24 words are not lists."""
    text = gold.strip().strip(".")
    if len(text.split()) > 24 or _SENTENCE.search(text) or _NUM_TOKEN.search(text):
        return []
    parts = re.split(r"\s*(?:,|;|&|\band\b|\bor\b)\s*", text, flags=re.I)
    items = [p.strip() for p in parts if _content(p)]
    return items if len(items) >= 2 else []


def _numbers(text: str) -> list[int]:
    text = _YEAR.sub(" ", _ISO.sub(" ", text))
    out = []
    for m in _NUM_TOKEN.finditer(text):
        tok = m.group(1).lower()
        out.append(int(tok) if tok.isdigit() else _NUMBER_WORDS[tok])
    return out


def _numerals_to_digits(text: str) -> str:
    def sub(m: re.Match[str]) -> str:
        tok = m.group(1).lower()
        return tok if tok.isdigit() else str(_NUMBER_WORDS[tok])

    return " ".join(_tokens(_NUM_TOKEN.sub(sub, text)))


def _numeral_rule(gold: str, answer: str) -> ConventionResult | None:
    if _COMPARATIVE.search(answer):
        return None
    m = _PURE_NUMBER.match(gold)
    if m:
        want = m.group(1).lower()
        want_n = int(want) if want.isdigit() else _NUMBER_WORDS[want]
        got = _numbers(answer)
        if got and got[0] == want_n and not _HEDGE.search(answer):
            return ConventionResult("numeral", f"{want_n}")
        return None
    if _NUM_TOKEN.search(gold):
        g, a = _numerals_to_digits(gold), _numerals_to_digits(answer)
        want, got = _numbers(gold), _numbers(answer)
        # the quantity the answer leads with must be the gold's ("about 3 weeks (... two weeks
        # before ...)" is not "two weeks")
        if g and f" {g} " in f" {a} " and want and got and got[0] == want[0]:
            return ConventionResult("numeral", g)
    return None


def check_conventions(question: str, answer: str, gold: str | None) -> ConventionResult | None:
    """The convention that makes ``answer`` right against ``gold``, or None. Pure and offline."""
    if not gold or not answer or not answer.strip():
        return None
    gold, answer = gold.strip(), answer.strip()
    if gold.lower().startswith("not mentioned") or _REFUSAL.search(answer):
        return None
    numeral = _numeral_rule(gold, answer)
    if numeral is not None:
        return numeral
    if _NEGATION.search(answer) or _MONTH.search(gold):
        return None  # a negated answer, or a date gold (the date check owns those)
    a_tokens = _tokens(answer)
    q_tokens = set(_tokens(question))
    items = gold_items(gold)
    if not items:
        words = _content(gold)
        if not words or not _proper_phrase(gold):
            return None
        if _item_in(gold, a_tokens, True, q_tokens) == "fuzzy":
            return ConventionResult("typo", " ".join(words))
        return None
    fuzzy = False
    for item in items:
        hit = _item_in(item, a_tokens, _proper_phrase(item), q_tokens)
        if hit is None:
            return None
        fuzzy = fuzzy or hit == "fuzzy"
    return ConventionResult("list_superset", f"{len(items)} items" + (" (typo)" if fuzzy else ""))


# =============================================================================================
# I77: conventions v2 (``--judge-conventions-v2``, column ``score_conventions_v2``)
# =============================================================================================
#
# Four more deterministic rules on top of v1. Same contract: they only turn a wrong verdict into a
# right one, they are reported as their OWN column (``score_conventions_v2`` / ``convention_v2``,
# v1 and the official ``score`` untouched), and they see only the question, the answer and the
# gold. No table holds a dataset answer: the alias groups below are everyday abbreviation and
# synonym sets (country names, family terms, appliances), the unit table is the SI / calendar
# definitions, the date rule is calendar arithmetic.
#
# ``date_format``  gold is ONE calendar date and nothing else; the first date in the answer is the
#                  same day written another way (ISO, "9 Oct 2022", "October 9th, 2022", "the
#                  ninth of October"). A range cue ("between", "week of", "before", "until"...)
#                  before the date, or any negation, blocks it. A gold without a year matches on
#                  month and day; with a year the answer must carry the same year.
# ``unit``         gold is ONE quantity ("2 hours", "a week", "$5"); the first quantity of the
#                  answer is the same amount in another unit of the same dimension, by exact
#                  definition (minutes/hours/days/weeks, months/years, mm/cm/m/km, g/kg, oz/lb,
#                  dollars/$/USD). Comparatives, hedges ("about", "at least") and negation block it.
#                  Same-unit numerals ("five" = 5) stay with the v1 ``numeral`` rule.
# ``ordinal``      "third" = "3rd" = "3d" on either side, through the v1 phrase matcher.
# ``alias``        gold and answer name the same thing by a listed alias ("mom"/"mother",
#                  "TV"/"television", "US"/"United States"), through the v1 phrase / list matcher
#                  on alias-folded text. Applies only when a fold actually changed something.
#
# False-positive risk: ``date_format`` and ``unit`` need an exact value match on a gold that is a
# bare date / quantity, so the residual risk is an answer whose FIRST date or quantity is the
# gold's but whose point is another ("on June 20 he booked, the trip was July 2"); range and
# hedge cues cover the common forms. ``alias`` and ``ordinal`` inherit v1's matcher (content words
# close together, negation blocks). Measured on the calibration set and read by hand on finished
# runs: ``evals/judge_agreement.py --conventions`` and ``evals/rescore_conventions.py``.

CONVENTIONS_V2_VERSION = "conventions/v2"

_MONTH_NUM = {
    m: i
    for i, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for m in names
}
_MONTH_RE = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))
_ORDINAL_WORDS = {
    w: i
    for i, w in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth "
        "thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth "
        "twenty-first twenty-second twenty-third twenty-fourth twenty-fifth twenty-sixth "
        "twenty-seventh twenty-eighth twenty-ninth thirtieth thirty-first".split(),
        start=1,
    )
}
_ORD_WORD_RE = "|".join(sorted((re.escape(w) for w in _ORDINAL_WORDS), key=len, reverse=True))
_DAY = rf"(\d{{1,2}})(?:st|nd|rd|th|d)?|({_ORD_WORD_RE})"
_DATE_PATTERNS = (
    ("iso", re.compile(r"\b((?:19|20)\d\d)-(\d{2})-(\d{2})\b")),
    (
        "dmy",
        re.compile(
            rf"\b(?:the )?(?:{_DAY})(?: of)?\s*,?\s*({_MONTH_RE})\b\.?(?:\s*,?\s*((?:19|20)\d\d)\b)?",
            re.I,
        ),
    ),
    (
        "mdy",
        re.compile(
            rf"\b({_MONTH_RE})\b\.?\s*(?:the )?(?:{_DAY})\b(?:\s*,?\s*((?:19|20)\d\d)\b)?",
            re.I,
        ),
    ),
)
_RANGE_CUE = re.compile(
    r"\b(between|from|until|till|through|since|before|after|around|about|approximately|week of|"
    r"weekend|early|late|mid|end of|start of|beginning of|to)\W+(?:\w+\W+){0,2}$",
    re.I,
)


def _day_value(num: str | None, word: str | None) -> int | None:
    if num:
        v = int(num)
    elif word:
        v = _ORDINAL_WORDS[word.lower()]
    else:
        return None
    return v if 1 <= v <= 31 else None


def _calendar_dates(text: str) -> list[tuple[int, tuple[int | None, int, int]]]:
    """``(start offset, (year or None, month, day))`` of every dated day in ``text``, in order."""
    found: list[tuple[int, tuple[int | None, int, int]]] = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(text):
            if kind == "iso":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if not (1 <= mo <= 12 and 1 <= d <= 31):
                    continue
            elif kind == "dmy":
                d = _day_value(m.group(1), m.group(2))
                mo = _MONTH_NUM.get(m.group(3).lower())
                y = int(m.group(4)) if m.group(4) else None
            else:
                mo = _MONTH_NUM.get(m.group(1).lower())
                d = _day_value(m.group(2), m.group(3))
                y = int(m.group(4)) if m.group(4) else None
            if d is None or mo is None:
                continue
            found.append((m.start(), (y, mo, d)))
    found.sort()
    out: list[tuple[int, tuple[int | None, int, int]]] = []
    for start, val in found:  # the same span matched by two patterns counts once
        if not out or start - out[-1][0] > 2 or val != out[-1][1]:
            out.append((start, val))
    return out


def _date_rule(gold: str, answer: str) -> ConventionResult | None:
    g_dates = _calendar_dates(gold)
    if len(g_dates) != 1:
        return None
    gy, gm, gd = g_dates[0][1]
    # the gold is the date and nothing else: "The Friday before 9 October" is a different thing
    residue = _content(
        re.sub(
            rf"\b(?:{_MONTH_RE})\b|\b\d+(?:st|nd|rd|th|d)?\b|\b(?:{_ORD_WORD_RE})\b|\bon\b|\bof\b",
            " ",
            gold,
            flags=re.I,
        )
    )
    if residue:
        return None
    a_dates = _calendar_dates(answer)
    if not a_dates:
        return None
    start, (ay, am, ad) = a_dates[0]
    if _RANGE_CUE.search(answer[:start]):
        return None
    if (am, ad) != (gm, gd) or (gy is not None and ay is not None and ay != gy):
        return None
    if gy is not None and ay is None:
        return None
    return ConventionResult("date_format", f"{gy or ''}-{gm:02d}-{gd:02d}".lstrip("-"))


# unit -> (dimension, factor to the dimension's base unit)
_UNITS: dict[str, tuple[str, float]] = {}
for _dim, _table in {
    "time": {
        "second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
        "week": 604800, "fortnight": 1209600,
    },
    "calendar_month": {"month": 1, "year": 12, "yr": 12, "decade": 120},
    "length": {
        "millimetre": 0.001, "millimeter": 0.001, "mm": 0.001, "centimetre": 0.01,
        "centimeter": 0.01, "cm": 0.01, "metre": 1, "meter": 1, "m": 1, "kilometre": 1000,
        "kilometer": 1000, "km": 1000, "inch": 0.0254, "in": 0.0254, "foot": 0.3048, "feet": 0.3048,
        "ft": 0.3048, "yard": 0.9144, "yd": 0.9144, "mile": 1609.344, "mi": 1609.344,
    },
    "mass": {
        "gram": 1, "g": 1, "kilogram": 1000, "kg": 1000, "kilo": 1000, "ounce": 28.349523125,
        "oz": 28.349523125, "pound": 453.59237, "lb": 453.59237, "lbs": 453.59237,
    },
    "usd": {"dollar": 1, "usd": 1, "buck": 1, "cent": 0.01},
}.items():
    for _name, _factor in _table.items():
        _UNITS[_name] = (_dim, float(_factor))
_UNIT_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
_QTY_WORDS = {**_NUMBER_WORDS, "a": 1, "an": 1, "half": 0.5}
_QTY_WORD_RE = "|".join(sorted(_QTY_WORDS, key=len, reverse=True))
_QTY = re.compile(
    rf"(?:(\$)\s*)?\b(\d+(?:[.,]\d+)?|{_QTY_WORD_RE})\b\s*(?:(?:-|\s)\s*)?(?:(half)\s+)?({_UNIT_RE})(?:e?s)?\b",
    re.I,
)
_DOLLAR = re.compile(r"\$\s*(\d+(?:[.,]\d+)?)", re.I)


def _quantities(text: str) -> list[tuple[str, float]]:
    """``(dimension, amount in the dimension's base unit)`` of every quantity, in order."""
    found: list[tuple[int, str, float]] = []
    for m in _QTY.finditer(text):
        raw = m.group(2).lower()
        amount = float(raw.replace(",", "")) if raw[0].isdigit() else float(_QTY_WORDS[raw])
        if m.group(3):
            amount += 0.5
        dim, factor = _UNITS[m.group(4).lower()]
        found.append((m.start(), dim, amount * factor))
    for m in _DOLLAR.finditer(text):
        if not any(abs(m.start() - s) <= 1 for s, d, _ in found if d == "usd"):
            found.append((m.start(), "usd", float(m.group(1).replace(",", ""))))
    found.sort()
    return [(d, a) for _, d, a in found]


def _unit_rule(gold: str, answer: str) -> ConventionResult | None:
    if _COMPARATIVE.search(answer) or _HEDGE.search(answer):
        return None
    g = _quantities(gold)
    if len(g) != 1:
        return None
    # the gold is the quantity and nothing else
    if _content(_QTY.sub(" ", _DOLLAR.sub(" ", gold))):
        return None
    a = _quantities(answer)
    if not a:
        return None
    (gd, gv), (ad, av) = g[0], a[0]
    if gd == ad and abs(gv - av) <= 1e-9 * max(1.0, abs(gv)):
        return ConventionResult("unit", f"{gv:g} {gd}")
    return None


#: Everyday alias groups (abbreviations and plain synonyms). No dataset answer is in here.
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("us", "u.s.", "usa", "u.s.a.", "united states", "united states of america", "america"),
    ("uk", "u.k.", "united kingdom", "great britain", "britain"),
    ("nyc", "new york city"),
    ("mom", "mum", "mother", "mommy", "mama"),
    ("dad", "father", "daddy", "papa"),
    ("grandma", "grandmother", "granny", "nana"),
    ("grandpa", "grandfather", "granddad"),
    ("tv", "television"),
    ("bike", "bicycle"),
    ("movie", "film"),
    ("photo", "photograph"),
    ("kid", "child"),
    ("puppy", "pup"),
    ("app", "application"),
    ("dr", "doctor"),
    ("vs", "versus"),
    ("ok", "okay"),
    ("sofa", "couch"),
    ("fridge", "refrigerator"),
    ("phone", "telephone"),
)
_ALIAS_ID: dict[str, str] = {}
for _i, _group in enumerate(ALIAS_GROUPS):
    for _alias in _group:
        _ALIAS_ID[_alias] = f"aliasgroup{_i}x"
_ALIAS_RE = re.compile(
    r"(?<![\w.])(" + "|".join(sorted((re.escape(a) for a in _ALIAS_ID), key=len, reverse=True)) + r")(?![\w])",
    re.I,
)
_ORD_DIGIT_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th|d)\b", re.I)


def _fold_aliases(text: str) -> str:
    return _ALIAS_RE.sub(lambda m: _ALIAS_ID[m.group(1).lower()], text)


def _fold_ordinals(text: str) -> str:
    text = _ORD_DIGIT_RE.sub(lambda m: f"ordinal{int(m.group(1))}x", text)
    return re.sub(
        rf"\b({_ORD_WORD_RE})\b",
        lambda m: f"ordinal{_ORDINAL_WORDS[m.group(1).lower()]}x",
        text,
        flags=re.I,
    )


def _folded_rule(question: str, answer: str, gold: str) -> ConventionResult | None:
    """Re-run the v1 phrase / list matcher on alias- and ordinal-folded text, when a fold changed
    something. The folded token keeps a leading capital when the original did."""
    for name, fold in (("ordinal", _fold_ordinals), ("alias", _fold_aliases)):
        g2, a2 = fold(gold), fold(answer)
        if g2 == gold and a2 == answer:
            continue
        q2 = fold(question)
        hit = check_conventions(q2, a2, g2)
        if hit is not None:
            return ConventionResult(name, hit.rule)
        words = _content(g2)
        if (
            not gold_items(g2)
            and 1 <= len(words) <= 4
            and not _MONTH.search(g2)
            and _item_in(g2, _tokens(a2), False, set(_tokens(q2))) == "exact"
            # the match must rest on the fold: the unfolded text did not already hold the phrase
            and _item_in(gold, _tokens(answer), False, set(_tokens(question))) is None
        ):
            return ConventionResult(name, " ".join(words))
    return None


def check_conventions_v2(question: str, answer: str, gold: str | None) -> ConventionResult | None:
    """v1 first (its rule name is kept), then ``date_format``, ``unit``, ``ordinal``, ``alias``.
    Pure and offline; never reads the category, the dataset or the run."""
    v1 = check_conventions(question, answer, gold)
    if v1 is not None:
        # v1's numeral rule reads a date gold ("3 May 2023") as a quantity and ignores a negation
        # ("It was not on 3 May 2023."); v2 does not carry that credit over
        if not (_NEGATION.search(answer) and gold and _MONTH.search(gold)):
            return v1
    if not gold or not answer or not answer.strip():
        return None
    gold, answer = gold.strip(), answer.strip()
    if gold.lower().startswith("not mentioned") or _REFUSAL.search(answer):
        return None
    if _NEGATION.search(answer):
        return None
    for rule in (_date_rule, _unit_rule):
        hit = rule(gold, answer)
        if hit is not None:
            return hit
    return _folded_rule(question, answer, gold)
