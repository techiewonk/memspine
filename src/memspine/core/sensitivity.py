"""I52: graded sensitivity (none / low / medium / high) with a category, and a read-time bar.

``core/sensitive.py`` (W16) says *whether* a text touches an art. 9-style topic. This
module adds the two things the peer engines have and it lacked (MIRIX low/medium/high with
a read-time filter, Cognee severity):

* a **grade** per record, set at write time from a fixed category-to-grade table (a decider
  ``sensitivity`` task may only raise it, see ``Engine._grade_sensitivity``);
* a **read-time bar** (:func:`passes_gate`): a graded record is injected only when the
  question is about its topic (a category cue in the question), names its subject, or shares
  enough content words with it. The bar rises with the grade. No weights to tune.

The record carries two tags, ``sens:<grade>`` and ``sensc:<category>`` (labels only; the text
is never copied into a tag, a log line or forensics). Cue-based and English, like W16. The
tag names live here; if the perspective layer (``core/perspective.py``) defines its own
``sensitivity`` axis constants, this module is the merge point: re-export from there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from memspine.core.query_shape import content_words
from memspine.core.sensitive import SENSITIVE_CATEGORIES

__all__ = [
    "CATEGORY_GRADE",
    "CATEGORY_TAG_PREFIX",
    "GRADES",
    "GRADE_TAG_PREFIX",
    "SensitivityLabel",
    "grade_rank",
    "grade_text",
    "label_of",
    "label_tags",
    "passes_gate",
    "query_topics",
]

GRADES: tuple[str, ...] = ("none", "low", "medium", "high")
GRADE_TAG_PREFIX = "sens:"
CATEGORY_TAG_PREFIX = "sensc:"

#: Canonical category -> grade. Fixed, not tuned. Health, identity, belief, ethnicity and
#: credentials are the categories whose disclosure to the wrong audience does the most harm
#: (GDPR art. 9 plus secrets); politics, finance, legal and location are personal but
#: routinely discussed, so medium.
CATEGORY_GRADE: dict[str, str] = {
    "health": "high",
    "sexuality_gender": "high",
    "religion": "high",
    "ethnicity": "high",
    "credentials": "high",
    "politics": "medium",
    "finance": "medium",
    "legal": "medium",
    "location": "medium",
}

#: W16 lexicon name -> canonical category.
_LEGACY = {
    "health": "health",
    "religion": "religion",
    "sexual_orientation": "sexuality_gender",
    "political": "politics",
    "ethnicity": "ethnicity",
    "legal": "legal",
    "financial": "finance",
}

#: Categories the W16 lexicon does not cover (gender identity beyond the first-person
#: cues, home address, credentials).
_EXTRA: dict[str, re.Pattern[str]] = {
    "sexuality_gender": re.compile(
        r"\b(?:my gender(?: identity)?|gender identity|gender dysphoria|sexual orientation|"
        r"i identify as|identif(?:y|ies) as (?:a )?(?:man|woman|male|female|non-?binary|"
        r"trans\w*)|(?:he|she|they)/(?:him|her|them)|my (?:boyfriend|girlfriend) and i are "
        r"(?:both )?(?:men|women))\b",
        re.IGNORECASE,
    ),
    "location": re.compile(
        r"\b(?:my (?:home )?address|i live (?:at|on|in)|home address|my apartment|"
        r"my zip ?code|my postcode|\d{1,5} [A-Z][a-z]+ (?:street|st|avenue|ave|road|rd|lane|"
        r"drive|dr|boulevard|blvd)\b)",
        re.IGNORECASE,
    ),
    "credentials": re.compile(
        r"\b(?:my (?:password|passcode|pin|login|api key|ssh key|secret key|access token)|"
        r"password is|passcode is|api[_ -]?key|secret[_ -]?key|private key|recovery phrase|"
        r"seed phrase)\b",
        re.IGNORECASE,
    ),
}

#: What a question has to say to be "about" a category. Deliberately narrower than the record
#: lexicon: a generic "where"/"what"/"recommend" must never open a high-grade memory.
_TOPIC_CUES: dict[str, re.Pattern[str]] = {
    "health": re.compile(
        r"\b(?:health\w*|doctor\w*|medic\w*|illness\w*|ill|sick\w*|diagnos\w*|therap\w*|"
        r"symptom\w*|hospital\w*|allerg\w*|pain\w*|disease\w*|condition\w*|surger\w*|"
        r"prescri\w*|pregnan\w*|disabilit\w*|mental|anxiety|depress\w*|diet\w*)\b",
        re.IGNORECASE,
    ),
    "sexuality_gender": re.compile(
        r"\b(?:gender\w*|sexual\w*|orientation|pronoun\w*|trans|transgender|lgbt\w*|gay|"
        r"lesbian|bisexual|queer|non-?binary|identity|identif\w*|came out|coming out)\b",
        re.IGNORECASE,
    ),
    "religion": re.compile(
        r"\b(?:relig\w*|faith|church\w*|pray\w*|god|belie\w+|worship\w*|temple|mosque|"
        r"synagogue|ramadan|bible|atheis\w*|spiritual\w*|holiday\w*)\b",
        re.IGNORECASE,
    ),
    "ethnicity": re.compile(
        r"\b(?:ethnic\w*|race|racial|immigra\w*|visa|citizen\w*|asylum|refugee|nationality|"
        r"heritage|ancestry)\b",
        re.IGNORECASE,
    ),
    "credentials": re.compile(
        r"\b(?:password\w*|passcode|pin|login|log in|credential\w*|api key|token|ssh|"
        r"secret\w*|recovery|sign in)\b",
        re.IGNORECASE,
    ),
    "politics": re.compile(
        r"\b(?:politic\w*|vote\w*|voting|election\w*|part(?:y|ies)|government|campaign\w*|"
        r"democrat\w*|republican\w*|union)\b",
        re.IGNORECASE,
    ),
    "finance": re.compile(
        r"\b(?:money|financ\w*|salary|income|debt\w*|loan\w*|mortgage\w*|budget\w*|bank\w*|"
        r"tax\w*|afford\w*|invest\w*|spend\w*|saving\w*|credit|pay|paid|rent)\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b(?:legal\w*|law\w*|lawyer\w*|attorney\w*|court\w*|sue|sued|police|arrest\w*|"
        r"ticket\w*|fine|fined|probation|custody|criminal|visa)\b",
        re.IGNORECASE,
    ),
    "location": re.compile(
        r"\b(?:address\w*|live|lives|living|lived|home|hometown|neighbou?rhood\w*|city|"
        r"street|moved|move|commute\w*|located|zip|postcode|resid\w*)\b",
        re.IGNORECASE,
    ),
}

#: Content words every question/record pair shares by accident.
_MIN_OVERLAP = {"low": 0, "medium": 1, "high": 2}


def grade_rank(grade: str) -> int:
    """0 none ... 3 high; an unknown grade ranks as none."""
    try:
        return GRADES.index(grade)
    except ValueError:
        return 0


class SensitivityLabel(NamedTuple):
    """A grade and the categories behind it."""

    grade: str
    categories: tuple[str, ...]


def grade_text(text: str) -> SensitivityLabel:
    """The grade and categories of ``text`` (deterministic; ``none`` when nothing matches).

    The grade is the highest grade among the matched categories."""
    found: list[str] = []
    for legacy, pattern in SENSITIVE_CATEGORIES.items():
        name = _LEGACY[legacy]
        if pattern.search(text) and name not in found:
            found.append(name)
    for name, pattern in _EXTRA.items():
        if name not in found and pattern.search(text):
            found.append(name)
    found.sort(key=list(CATEGORY_GRADE).index)
    if not found:
        return SensitivityLabel("none", ())
    grade = max((CATEGORY_GRADE[c] for c in found), key=grade_rank)
    return SensitivityLabel(grade, tuple(found))


def label_tags(label: SensitivityLabel | tuple[str, Sequence[str]]) -> list[str]:
    """``sens:<grade>`` plus one ``sensc:<category>`` per category; nothing for ``none``."""
    grade, categories = label[0], label[1]
    if grade == "none":
        return []
    return [f"{GRADE_TAG_PREFIX}{grade}", *(f"{CATEGORY_TAG_PREFIX}{c}" for c in categories)]


def label_of(tags: Iterable[str]) -> SensitivityLabel:
    """The label a record's tags carry. A record that only has the W16 ``sensitive:<topic>``
    tags (written before this module) is graded from those; no tag at all is ``none``."""
    grade = "none"
    categories: list[str] = []
    legacy: list[str] = []
    for tag in tags:
        if tag.startswith(GRADE_TAG_PREFIX):
            value = tag[len(GRADE_TAG_PREFIX) :]
            if value in GRADES and grade_rank(value) > grade_rank(grade):
                grade = value
        elif tag.startswith(CATEGORY_TAG_PREFIX):
            categories.append(tag[len(CATEGORY_TAG_PREFIX) :])
        elif tag.startswith("sensitive:"):
            name = _LEGACY.get(tag[len("sensitive:") :])
            if name:
                legacy.append(name)
    if grade == "none" and not categories and legacy:
        grade = max((CATEGORY_GRADE[c] for c in legacy), key=grade_rank)
        categories = legacy
    return SensitivityLabel(grade, tuple(categories))


def query_topics(query: str) -> frozenset[str]:
    """The sensitive categories the question is about (a category cue in the question)."""
    return frozenset(name for name, pattern in _TOPIC_CUES.items() if pattern.search(query))


def passes_gate(
    label: SensitivityLabel | tuple[str, Sequence[str]],
    content: str,
    entity: str | None,
    query: str,
    topics: frozenset[str] | None = None,
) -> bool:
    """May a record with ``label`` be injected for ``query``?

    ``none``/``low``: always. ``medium``: the question is about one of its categories, or
    names its subject (the record's entity), or shares one content word with it. ``high``:
    the question is about one of its categories, or shares two content words with it (naming
    the subject alone is not enough: "what does Sam like to eat?" must not open Sam's
    diagnosis). A graded record with no category passes only on the overlap test.
    """
    grade, categories = label[0], tuple(label[1])
    need = _MIN_OVERLAP.get(grade)
    if need is None or grade == "low":
        return True
    about = topics if topics is not None else query_topics(query)
    if about.intersection(categories):
        return True
    if grade == "medium" and entity and re.search(rf"\b{re.escape(entity)}\b", query, re.I):
        return True
    return len(content_words(query) & content_words(content)) >= need
