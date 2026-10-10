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
    substitution only for words of 7 letters or more, so "Maria" never matches "Mario"). Words
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
    "check_conventions",
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
