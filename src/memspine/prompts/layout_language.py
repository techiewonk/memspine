"""``prompts.layout.language``: which language a prompt's source text is in (W2-07; P-10, W-18).

Pure, offline, no model and no third-party detector. Non-Latin scripts are told apart by
character ranges; Latin-script text by counting very common function words of a small fixed
set of languages. The answer is a lower-case ISO 639-1 code, and ``"en"`` whenever the
evidence is thin: English is the default, so an undecided short line never switches on the
same-language partial. The partial itself is :data:`SAME_LANGUAGE_PARTIAL`; it is injected by
``Prompt.render`` for the base layout (``prompts.layout``) when the code is not ``"en"``.
"""

from __future__ import annotations

import re

__all__ = ["COARSE_DATE_PARTIAL", "SAME_LANGUAGE_PARTIAL", "detect_language", "source_text"]

#: Names of the shipped partials under ``defaults/_partials/`` (W2-07).
SAME_LANGUAGE_PARTIAL = "same_language.j2"
COARSE_DATE_PARTIAL = "coarse_date.j2"

_KANA = re.compile("[぀-ヿ]")
#: (code, regex of the script's letters), checked in order on the share of all letters.
_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ko", re.compile("[가-힯ᄀ-ᇿ]")),
    ("zh", re.compile("[一-鿿㐀-䶿]")),
    ("ru", re.compile("[Ѐ-ӿ]")),
    ("ar", re.compile("[؀-ۿ]")),
    ("he", re.compile("[֐-׿]")),
    ("hi", re.compile("[ऀ-ॿ]")),
    ("th", re.compile("[฀-๿]")),
    ("el", re.compile("[Ͱ-Ͽ]")),
)
_LETTER = re.compile(r"[^\W\d_]")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
#: Share of letters in one non-Latin script that decides the language.
_SCRIPT_SHARE = 0.25


def _w(text: str) -> frozenset[str]:
    return frozenset(text.split())


_STOP: dict[str, frozenset[str]] = {
    "en": _w(
        "the and is are was were have has had with that this for not you but they from what "
        "when where which would there their about just been will can"
    ),
    "de": _w(
        "der die das und ist nicht ich ein eine mit zu den dem auf für es sie wir war haben "
        "hat aber auch von dass wie noch nach bei sich"
    ),
    "fr": _w(
        "le la les des est une un et que pour pas dans qui sur avec nous vous je il elle mais "
        "ont sont être avoir ce cette au aux très"
    ),
    "es": _w(
        "el la los las es una un y que para por con del en no se su lo como pero más fue son "
        "está muy yo mi tengo"
    ),
    "it": _w(
        "il lo gli le che di una un è non per con sono ho hai ha ma come più anche nel della "
        "dei questo molto"
    ),
    "pt": _w(
        "o os as uma um e que para com não em do da dos das foi são tem mais como mas muito "
        "você eu meu minha"
    ),
    "nl": _w(
        "de het een en is niet ik je met van op voor dat die zijn heb ook maar naar wat dit nog"
    ),
}
#: A Latin-script language needs at least this many function-word hits to beat English.
_MIN_HITS = 3


def source_text(context: dict[str, object]) -> str:
    """The text to classify from a render context: the transcript/content variable when
    present (``content``, ``transcript``, ``text``), else every string value joined."""
    for key in ("content", "transcript", "text"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "\n".join(v for v in context.values() if isinstance(v, str))


def detect_language(text: str) -> str:
    """ISO 639-1 code of ``text``'s language, ``"en"`` when undecided."""
    total = len(_LETTER.findall(text))
    if not total:
        return "en"
    # Japanese is mostly han with some kana: a small kana share wins over zh.
    kana = len(_KANA.findall(text))
    if kana >= 3 and kana / total >= 0.05:
        return "ja"
    for code, pat in _SCRIPTS:
        n = len(pat.findall(text))
        if n and n / total >= _SCRIPT_SHARE:
            return code
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < 4:
        return "en"
    scores = {code: sum(1 for w in words if w in stop) for code, stop in _STOP.items()}
    best = max((c for c in scores if c != "en"), key=lambda c: scores[c])
    if scores[best] >= _MIN_HITS and scores[best] > scores["en"]:
        return best
    return "en"
