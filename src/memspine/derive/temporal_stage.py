"""W2-14 (``write.temporal_v2``; W-04, M-10, R-07): temporal anchoring as an envelope stage, order 100.

For every envelope of a window this stage fills the event interval, its precision and its basis, by
the anchor ladder (S6-2: never invent a date, precision never finer than the evidence):

1. **explicit**: a date in the model's ``when`` / ``until`` text, else in the cited span, else in the
   assertion text (``2021-03-04``, ``3 March 2021``, ``March 2021``, ``summer 2019``, ``in 2019`` ...;
   English, German, French, Spanish). Basis ``explicit``; precision day, month, season or year as
   written. A year-only phrase is year precision, never a made-up day.
2. **relative**: a relative phrase (``last Tuesday``, ``letzte Woche``, ``il y a trois jours``,
   ``hace dos semanas``) resolved against the cited turn's ``source_at`` (W2-02 stamping), else the
   nearest stamped turn of the window (the window source time). Basis ``source_time`` (the "relative"
   basis; the anchor is kept), precision the phrase's own ("last week" = week).
3. **session anchor**: only when stamping is off (``trust_valid_from``): the window's
   ``valid_from`` is then the only clock there is. A relative phrase resolved against it is
   ``inferred`` and ``approximate``. With stamping on, ``valid_from`` is never read as a source time.
4. **unknown**: the phrase stays as text, ``basis="none"``. Never "now".

Typed relations without dates: ``before`` / ``after`` / ``during`` / ``since`` / ``until`` between an
assertion and another assertion of the same window ("the week before her trip") are written as a
relation ref (``basis="relation"``, no date) when no date resolved. ``since`` is stored as ``after``
and ``until`` as ``before`` (the schema's relation kinds), the spoken word is kept in the audit
extras. The target is the other envelope whose text shares a content word with the phrase's object;
none or several equal candidates give no relation (counted, never guessed). A target is referenced
by its ``occurrence_id``.

Envelope fields written: ``time.event`` (``Interval``; start-only for a point, ``end`` only when the
model gave ``until``), ``assessment.time_precision`` (kept equal to the event precision, S5-1), and the
audit extras key ``extras["temporal"] = {"rung", "basis_label", "evidence_precision", "relations"}``.
Fields read: ``when`` / ``until`` text on ``time.event.start|end`` (set by the compiler), ``evidence``
(span quote, turn reference), ``text``. Nothing else is touched. The graph edge-time view
(``derive/edges.py``) reads ``time.event`` unchanged. ``happened_label_for`` gives the stored
``happened:`` label at the written precision, so the read leg (``read.legs.temporal``) matches the
interval, not a made-up day.

Pure given its inputs; no model calls, no clock reads.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from memspine.core.event_date import happened_label
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.derive.temporal import (
    TemporalCandidate,
    anchor,
    normalise_relative_phrase,
    precision_interval,
)
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.temporal import Interval, TemporalRef, TemporalRelation

__all__ = [
    "EXTRAS_KEY",
    "STAGE_ORDER",
    "Found",
    "audit_precision",
    "build_stage",
    "find_explicit",
    "find_relation",
    "find_relative",
    "happened_label_for",
    "resolve_text",
]

STAGE_ORDER = 100
EXTRAS_KEY = "temporal"

#: coarse to fine; used by the precision audit
_FINE_TO_COARSE = ("instant", "minute", "hour", "day", "week", "month", "season", "year", "decade")


def _pidx(p: str) -> int:
    return _FINE_TO_COARSE.index(p)


def fold(s: str) -> str:
    """Lower-case, accents stripped, German umlauts kept readable (ä -> a), apostrophes plain."""
    s = unicodedata.normalize("NFKC", s).casefold().replace("ß", "ss").replace("\u2019", "'")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


# ---------------------------------------------------------------------------------------------------------
# vocabulary (en / de / fr / es)
# ---------------------------------------------------------------------------------------------------------
_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("january", "januar", "janvier", "enero"),
        ("february", "februar", "fevrier", "febrero"),
        ("march", "marz", "maerz", "mars", "marzo"),
        ("april", "avril", "abril"),
        ("may", "mai", "mayo"),
        ("june", "juni", "juin", "junio"),
        ("july", "juli", "juillet", "julio"),
        ("august", "aout", "agosto"),
        ("september", "sept", "septembre", "septiembre", "setiembre"),
        ("october", "oktober", "octobre", "octubre"),
        ("november", "novembre", "noviembre"),
        ("december", "dezember", "decembre", "diciembre"),
    ],
    1,
):
    for _n in _names:
        _MONTHS[_n] = _i
_SEASONS: dict[str, str] = {
    "winter": "winter", "hiver": "winter", "invierno": "winter",
    "spring": "spring", "fruhling": "spring", "printemps": "spring", "primavera": "spring",
    "summer": "summer", "sommer": "summer", "ete": "summer", "verano": "summer",
    "autumn": "autumn", "fall": "autumn", "herbst": "autumn", "automne": "autumn", "otono": "autumn",
}  # fmt: skip
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASON_RE = "|".join(sorted(_SEASONS, key=len, reverse=True))
_YEAR_PREP = r"(?:in|en|im jahr|im jahre|im|during|durante|pendant|wahrend|back in|de|del|of|since|until|till|from|seit|bis|depuis|desde|hasta|jusqu'en|jusqu'a)"

_P_ISO_DAY = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_P_ISO_MONTH = re.compile(r"(?<![\d-])(\d{4})-(\d{2})(?![\d-])")
_P_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\.?\s+(?:of\s+|de\s+)?({_MONTH_RE})\.?,?\s+(?:de\s+)?(\d{{4}})\b"
)
_P_MDY = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b")
_P_MY = re.compile(rf"\b({_MONTH_RE})\.?,?\s+(?:de\s+|of\s+)?(\d{{4}})\b")
_P_SY = re.compile(rf"\b({_SEASON_RE})\s+(?:of\s+|de\s+)?(\d{{4}})\b")
_P_Y = re.compile(rf"\b{_YEAR_PREP}\s+(?:the\s+year\s+)?(\d{{4}})\b")
_P_BARE_Y = re.compile(r"^\s*(\d{4})\s*$")


@dataclass(frozen=True)
class Found:
    """A temporal expression found in a text: the candidate, the matched text, and the finest precision
    the matched text supports (the evidence precision the audit checks against)."""

    kind: Literal["explicit", "relative"]
    candidate: TemporalCandidate
    matched: str
    evidence_precision: str
    start: int = 0


def _year_ok(y: int) -> bool:
    return 1900 <= y <= 2100


def find_explicit(text: str | None, *, whole: bool = False) -> Found | None:
    """The first explicit date in ``text`` (most specific form wins on overlap), or None.

    ``whole=True`` also accepts a bare four-digit year (the ``when`` field is exactly "2019")."""
    if not text:
        return None
    s = fold(text)
    hits: list[tuple[int, int, Found]] = []

    def add(m: re.Match[str], cand: TemporalCandidate | None, prec: str) -> None:
        if cand is not None:
            hits.append((m.start(), m.end(), Found("explicit", cand, m.group(0), prec, m.start())))

    for m in _P_ISO_DAY.finditer(s):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        add(m, TemporalCandidate(kind="absolute", text=m[0], date_value=d), "day")
    for m in _P_DMY.finditer(s):
        try:
            d = date(int(m[3]), _MONTHS[m[2]], int(m[1]))
        except ValueError:
            continue
        add(m, TemporalCandidate(kind="absolute", text=m[0], date_value=d), "day")
    for m in _P_MDY.finditer(s):
        try:
            d = date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        except ValueError:
            continue
        add(m, TemporalCandidate(kind="absolute", text=m[0], date_value=d), "day")
    for m in _P_ISO_MONTH.finditer(s):
        y, mo = int(m[1]), int(m[2])
        if _year_ok(y) and 1 <= mo <= 12:
            add(
                m,
                TemporalCandidate(
                    kind="partial", text=m[0], partial="year_month", year=y, month=mo
                ),
                "month",
            )
    for m in _P_MY.finditer(s):
        y = int(m[2])
        if _year_ok(y):
            add(
                m,
                TemporalCandidate(
                    kind="partial", text=m[0], partial="year_month", year=y, month=_MONTHS[m[1]]
                ),
                "month",
            )
    for m in _P_SY.finditer(s):
        y = int(m[2])
        if _year_ok(y):
            add(
                m,
                TemporalCandidate(
                    kind="partial", text=m[0], partial="season", year=y, season=_SEASONS[m[1]]
                ),
                "season",
            )  # type: ignore[arg-type]
    for m in _P_Y.finditer(s):
        y = int(m[1])
        if _year_ok(y):
            add(m, TemporalCandidate(kind="partial", text=m[0], partial="year", year=y), "year")
    if whole and (m := _P_BARE_Y.match(s)) and _year_ok(int(m[1])):
        add(
            m,
            TemporalCandidate(kind="partial", text=m[0].strip(), partial="year", year=int(m[1])),
            "year",
        )
    if not hits:
        return None
    # most specific first on overlap (longest match), then text order
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen: list[tuple[int, int, Found]] = []
    for h in sorted(hits, key=lambda h: -(h[1] - h[0])):
        if all(h[1] <= c[0] or h[0] >= c[1] for c in chosen):
            chosen.append(h)
    chosen.sort(key=lambda h: h[0])
    return chosen[0][2]


# --- relative phrases ------------------------------------------------------------------------------------
_RO_UNITS: dict[str, str] = {
    # fr
    "jour": "day", "jours": "day", "semaine": "week", "semaines": "week", "mois": "month",
    "an": "year", "ans": "year", "annee": "year", "annees": "year", "heure": "hour", "heures": "hour",
    # es
    "dia": "day", "dias": "day", "semana": "week", "semanas": "week", "mes": "month", "meses": "month",
    "ano": "year", "anos": "year", "hora": "hour", "horas": "hour",
}  # fmt: skip
_RO_WEEKDAYS: dict[str, int] = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6,
}  # fmt: skip
_RO_NUMBERS: dict[str, int] = {
    "un": 1, "une": 1, "una": 1, "deux": 2, "dos": 2, "trois": 3, "tres": 3, "quatre": 4, "cuatro": 4,
    "cinq": 5, "cinco": 5, "six": 6, "seis": 6, "sept": 7, "siete": 7, "huit": 8, "ocho": 8,
    "neuf": 9, "nueve": 9, "dix": 10, "diez": 10,
}  # fmt: skip
_RO_LAST = frozenset({"dernier", "derniere", "derniers", "pasado", "pasada"})
_RO_NEXT = frozenset({"prochain", "prochaine", "proximo", "proxima"})
_RO_THIS = frozenset({"cette", "ce", "esta", "este"})
_RO_SPAN: dict[str, str] = {
    "semaine": "week", "mois": "month", "an": "year", "annee": "year",
    "semana": "week", "mes": "month", "ano": "year",
}  # fmt: skip
#: tokens that are a relative phrase on their own (safe inside free text)
_SAFE_SINGLE = frozenset(
    {
        "yesterday",
        "tomorrow",
        "today",
        "gestern",
        "vorgestern",
        "heute",
        "hier",
        "demain",
        "ayer",
        "anteayer",
        "hoy",
    }
)
_RO_FIXED: dict[tuple[str, ...], tuple[str, str, int | None]] = {
    ("hier",): ("ago", "day", 1),
    ("avant-hier",): ("ago", "day", 2),
    ("avant", "hier"): ("ago", "day", 2),
    ("demain",): ("ahead", "day", 1),
    ("apres-demain",): ("ahead", "day", 2),
    ("apres", "demain"): ("ahead", "day", 2),
    ("aujourd'hui",): ("this", "day", None),
    ("ayer",): ("ago", "day", 1),
    ("anteayer",): ("ago", "day", 2),
    ("antes", "de", "ayer"): ("ago", "day", 2),
    ("manana",): ("ahead", "day", 1),
    ("pasado", "manana"): ("ahead", "day", 2),
    ("hoy",): ("this", "day", None),
}


def _rcand(rel: str, unit: str, n: int | None, weekday: int | None, text: str) -> TemporalCandidate:
    return TemporalCandidate(kind="relative", text=text, rel=rel, unit=unit, n=n, weekday=weekday)  # type: ignore[arg-type]


def _num(tok: str) -> int | None:
    if tok.isascii() and tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 100_000 else None
    return _RO_NUMBERS.get(tok)


def _romance(toks: Sequence[str], text: str) -> TemporalCandidate | None:
    """French / Spanish relative phrases; ``None`` if not sure."""
    key = tuple(toks)
    if key in _RO_FIXED:
        rel, unit, n = _RO_FIXED[key]
        return _rcand(rel, unit, n, None, text)
    if len(toks) == 2:
        a, b = toks
        if a in _RO_WEEKDAYS and (b in _RO_LAST or b in _RO_NEXT):
            return _rcand(
                "last" if b in _RO_LAST else "next", "weekday", None, _RO_WEEKDAYS[a], text
            )
        if a in _RO_SPAN and (b in _RO_LAST or b in _RO_NEXT):
            return _rcand("last" if b in _RO_LAST else "next", _RO_SPAN[a], None, None, text)
        if a in _RO_THIS and b in _RO_SPAN:
            return _rcand("this", _RO_SPAN[b], None, None, text)
    if (
        len(toks) == 3
        and toks[0] == "hace"
        and (n := _num(toks[1])) is not None
        and toks[2] in _RO_UNITS
    ):
        return _rcand("ago", _RO_UNITS[toks[2]], n, None, text)
    if (
        len(toks) == 3
        and toks[0] == "dans"
        and (n := _num(toks[1])) is not None
        and toks[2] in _RO_UNITS
    ):
        return _rcand("ahead", _RO_UNITS[toks[2]], n, None, text)
    if (
        len(toks) == 5
        and key[:3] == ("il", "y", "a")
        and (n := _num(toks[3])) is not None
        and toks[4] in _RO_UNITS
    ):
        return _rcand("ago", _RO_UNITS[toks[4]], n, None, text)
    if (
        len(toks) == 4
        and toks[0] == "dentro"
        and toks[1] == "de"
        and (n := _num(toks[2])) is not None
        and toks[3] in _RO_UNITS
    ):
        return _rcand("ahead", _RO_UNITS[toks[3]], n, None, text)
    return None


def _try(phrase_tokens: Sequence[str]) -> TemporalCandidate | None:
    text = " ".join(phrase_tokens)
    return _romance(phrase_tokens, text) or normalise_relative_phrase(text)


def _tok(s: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in re.finditer(r"[\w'-]+", fold(s))]


def find_relative(text: str | None, *, whole: bool = False) -> Found | None:
    """The first relative phrase in ``text`` (longest n-gram first), or None.

    ``whole=True`` (the ``when`` field) also accepts a single ambiguous word ("morgen", "manana")."""
    if not text:
        return None
    toks = _tok(text)
    words = [t for t, _ in toks]
    if not words:
        return None
    if whole and len(words) <= 6:
        c = _try(words)
        if c is not None:
            return Found("relative", c, " ".join(words), _rel_precision(c), 0)
    for n in range(min(5, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            chunk = words[i : i + n]
            if n == 1 and chunk[0] not in _SAFE_SINGLE:
                continue
            c = _try(chunk)
            if c is not None:
                return Found("relative", c, " ".join(chunk), _rel_precision(c), toks[i][1])
    return None


def _rel_precision(c: TemporalCandidate) -> str:
    unit = c.unit or "day"
    return {"minute": "minute", "hour": "hour", "day": "day", "weekday": "day", "weekend": "day",
            "week": "week", "month": "month", "season": "season", "year": "year"}.get(unit, "day")  # fmt: skip


# --- relations --------------------------------------------------------------------------------------------
_REL_WORDS: dict[str, tuple[str, str]] = {
    # folded word(s) -> (schema relation, spoken word)
    "before": ("before", "before"), "after": ("after", "after"), "during": ("during", "during"),
    "since": ("after", "since"), "until": ("before", "until"), "till": ("before", "until"),
    "vor": ("before", "before"), "nach": ("after", "after"), "wahrend": ("during", "during"),
    "seit": ("after", "since"), "bis": ("before", "until"),
    "avant": ("before", "before"), "apres": ("after", "after"), "pendant": ("during", "during"),
    "depuis": ("after", "since"), "jusqu'a": ("before", "until"), "jusqu'au": ("before", "until"),
    "jusqu'en": ("before", "until"),
    "antes": ("before", "before"), "despues": ("after", "after"), "durante": ("during", "during"),
    "desde": ("after", "since"), "hasta": ("before", "until"),
}  # fmt: skip
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "her",
        "his",
        "their",
        "my",
        "our",
        "your",
        "its",
        "that",
        "this",
        "these",
        "those",
        "of",
        "to",
        "at",
        "on",
        "in",
        "for",
        "with",
        "and",
        "or",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "seine",
        "ihre",
        "ihr",
        "sein",
        "mein",
        "dein",
        "unser",
        "le",
        "la",
        "les",
        "l",
        "un",
        "une",
        "du",
        "de",
        "des",
        "au",
        "aux",
        "sa",
        "son",
        "ses",
        "leur",
        "leurs",
        "mon",
        "ma",
        "mes",
        "el",
        "los",
        "las",
        "lo",
        "su",
        "sus",
        "un",
        "una",
        "unos",
        "unas",
        "del",
        "al",
        "mi",
        "tu",
        "nuestro",
    ]
)
_STOP_AFTER = frozenset(
    {"and", "but", "when", "while", "und", "aber", "et", "mais", "y", "pero", "because", "so"}
)


@dataclass(frozen=True)
class RelFound:
    relation: Literal["before", "after", "during"]
    word: str
    obj: tuple[str, ...]  # folded content words of the object phrase


def find_relation(text: str | None) -> RelFound | None:
    """A ``before|after|during|since|until`` phrase whose object is a thing, not a date or a number."""
    if not text:
        return None
    words = [t for t, _ in _tok(text)]
    for i, w in enumerate(words):
        if w == "antes" or w == "despues":
            if i + 1 < len(words) and words[i + 1] == "de":
                pass
            else:
                continue
        if w not in _REL_WORDS:
            continue
        j = i + (2 if w in ("antes", "despues") else 1)
        obj: list[str] = []
        for t in words[j : j + 6]:
            if t in _STOP_AFTER:
                break
            obj.append(t)
        if not obj:
            continue
        if obj[0].isdigit() or obj[0] in _MONTHS or obj[0] in _SEASONS or _num(obj[0]) is not None:
            continue  # "vor 3 Tagen", "until 2024", "since March": a date, not a relation
        if find_explicit(" ".join(obj), whole=True) or find_relative(" ".join(obj), whole=True):
            continue
        content = tuple(t for t in obj if t not in _STOP and len(t) > 2)
        if not content:
            continue
        rel, spoken = _REL_WORDS[w]
        return RelFound(rel, spoken, content)  # type: ignore[arg-type]
    return None


def _stem(w: str) -> str:
    return w[:5]


def _target(rel: RelFound, others: Sequence[AssertionEnvelope]) -> AssertionEnvelope | None:
    """The unique other envelope that best shares a content word with the relation's object."""
    want = {_stem(w) for w in rel.obj}
    scored: list[tuple[int, AssertionEnvelope]] = []
    for o in others:
        surface = " ".join(
            [o.text, o.object.surface if o.object else "", o.attribute or "", str(o.value or "")]
        )
        have = {_stem(t) for t, _ in _tok(surface) if t not in _STOP and len(t) > 2}
        score = len(want & have)
        if score:
            scored.append((score, o))
    if not scored:
        return None
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


# ---------------------------------------------------------------------------------------------------------
# ladder
# ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Resolved:
    ref: TemporalRef
    rung: int
    basis_label: Literal["explicit", "relative", "inferred"]
    evidence_precision: str


def resolve_text(
    texts: Sequence[tuple[str, bool]],
    *,
    turn_time: datetime | None,
    window_time: datetime | None,
    session_anchor: datetime | None,
    tz: str = "UTC",
) -> Resolved | None:
    """Run the ladder over ``texts`` (``(text, is_field)`` in priority order). None = rung 4, unknown.

    Rung 1 is tried on every text first (an explicit date beats a relative phrase anywhere), then rung 2
    on every text, then rung 3."""
    for text, whole in texts:
        f = find_explicit(text, whole=whole)
        if f is not None:
            ref = anchor(f.candidate, source_time=None, tz=tz)
            if ref.at is not None:
                return Resolved(ref, 1, "explicit", f.evidence_precision)
    rels = [
        (f, text) for text, whole in texts if (f := find_relative(text, whole=whole)) is not None
    ]
    if not rels:
        return None
    f = rels[0][0]
    for src in (turn_time, window_time):
        if src is not None:
            ref = anchor(f.candidate, source_time=src, tz=tz)
            if ref.at is not None:
                return Resolved(ref, 2, "relative", f.evidence_precision)
    if session_anchor is not None:
        ref = anchor(f.candidate, source_time=session_anchor, tz=tz)
        if ref.at is not None:
            ref = ref.model_copy(update={"basis": "inferred", "approximate": True})
            return Resolved(ref, 3, "inferred", f.evidence_precision)
    return None


def audit_precision(ref: TemporalRef, evidence_precision: str) -> bool:
    """True when ``ref`` claims no finer precision than the evidence supports."""
    if ref.at is None:
        return ref.precision == "unknown"
    return _pidx(ref.precision) >= _pidx(evidence_precision)


def happened_label_for(start: TemporalRef | None, end: TemporalRef | None = None) -> str | None:
    """The ``happened:`` label (day, month, year or ``first..last``) at the written precision, or None.

    A day-precision ref gives a day; a year ref gives the year; a week gives its seven days. An interval
    runs from the start of the start period to the last day of the end period."""
    if start is None or start.at is None or start.precision == "unknown":
        return None
    try:
        lo, _ = precision_interval(start.at, start.precision)
        last_ref = (
            end if end is not None and end.at is not None and end.precision != "unknown" else start
        )
        _, hi = precision_interval(last_ref.at, last_ref.precision)  # type: ignore[arg-type]
        first, last = lo.date(), (hi - timedelta(microseconds=1)).date()
    except (ValueError, OverflowError):
        return None
    if last < first:
        return None
    return happened_label(first, last)


# ---------------------------------------------------------------------------------------------------------
# the stage
# ---------------------------------------------------------------------------------------------------------
_OPEN_START = frozenset({"since", "seit", "depuis", "desde", "from", "ab", "ever"})


def _cited_texts(env: AssertionEnvelope) -> list[str]:
    out: list[str] = []
    for e in env.evidence:
        if e.role == "support" and e.span is not None and e.span.quote:
            out.append(e.span.quote)
    out.append(env.text)
    return out


def _turn_record(env: AssertionEnvelope, by_id: dict[str, Any]) -> Any:
    for e in env.evidence:
        if e.role == "support" and e.turn is not None and e.turn.source_id in by_id:
            return by_id[e.turn.source_id]
    return None


def build_stage(*, trust_valid_from: bool = False) -> EnvelopeStage:
    """The stage. ``trust_valid_from``: stamping (W2-02) is off, so a record's ``valid_from`` is the only
    clock and serves as the session anchor (rung 3); with stamping on it is never a source time."""

    async def run(
        envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        return apply_temporal(envelopes, ctx, trust_valid_from=trust_valid_from)

    return EnvelopeStage("temporal", STAGE_ORDER, run)


def _stamped(records: Sequence[Any]) -> list[datetime]:
    return [r.source_at for r in records if getattr(r, "source_at", None) is not None]


def apply_temporal(
    envelopes: list[AssertionEnvelope],
    ctx: EnvelopeStageContext,
    *,
    trust_valid_from: bool = False,
) -> list[AssertionEnvelope]:
    by_id = {getattr(r, "record_id", None): r for r in ctx.records}
    stamped = _stamped(ctx.records)
    window_time = stamped[0] if stamped else None
    session_anchor = None
    if trust_valid_from:
        cc = getattr(ctx.compile_ctx, "source_time", None)
        session_anchor = (
            cc if cc is not None else (ctx.records[0].valid_from if ctx.records else None)
        )
    out: list[AssertionEnvelope] = []
    for env in envelopes:
        out.append(_one(env, envelopes, by_id, window_time, session_anchor, ctx.notes))
    if tap.enabled():
        tap.event("write.temporal", window=ctx.window_id, **dict(ctx.notes))
    return out


def _one(
    env: AssertionEnvelope,
    window: Sequence[AssertionEnvelope],
    by_id: dict[str, Any],
    window_time: datetime | None,
    session_anchor: datetime | None,
    notes: Counter[str],
) -> AssertionEnvelope:
    ev = env.time.event
    start0 = ev.start if ev is not None else None
    end0 = ev.end if ev is not None else None
    if start0 is not None and start0.at is not None:  # already anchored upstream: leave it
        notes["temporal_kept"] += 1
        return env
    rec = _turn_record(env, by_id)
    turn_time = getattr(rec, "source_at", None) if rec is not None else None
    tz = (getattr(rec, "source_tz", None) or "UTC") if rec is not None else "UTC"
    if tz in ("unknown", ""):
        tz = "UTC"
    when = start0.text if start0 is not None else None
    until = end0.text if end0 is not None else None
    cited = _cited_texts(env)
    ladder_in = ([(when, True)] if when else []) + [(t, False) for t in cited]

    try:
        res = resolve_text(
            ladder_in,
            turn_time=turn_time,
            window_time=window_time,
            session_anchor=session_anchor,
            tz=tz,
        )
    except Exception:  # a parser fault must never lose the fact (S6-2: leave the time unknown)
        notes["temporal_error"] += 1
        return env
    end_res = None
    if until:
        try:
            end_res = resolve_text(
                [(until, True)],
                turn_time=turn_time,
                window_time=window_time,
                session_anchor=session_anchor,
                tz=tz,
            )
        except Exception:
            notes["temporal_error"] += 1

    # relations: the phrase in the model's time text, else the cited span
    rel_ref: TemporalRef | None = None
    rel_found: RelFound | None = None
    for t in ([when] if when else []) + ([until] if until else []) + cited:
        rel_found = find_relation(t)
        if rel_found is not None:
            break
    audit: dict[str, Any] = {}
    relations: list[dict[str, str]] = []
    if rel_found is not None:
        tgt = _target(rel_found, [o for o in window if o is not env])
        if tgt is None:
            notes["temporal_relation_no_target"] += 1
        else:
            relations.append(
                {
                    "relation": rel_found.relation,
                    "word": rel_found.word,
                    "target": tgt.occurrence_id,
                }
            )
            rel_ref = TemporalRef(
                text=when or until or rel_found.word,
                basis="relation",
                relation=TemporalRelation(
                    relation=rel_found.relation, target_assertion_id=tgt.occurrence_id
                ),
            )
            notes["temporal_relation"] += 1

    start_ref: TemporalRef | None
    if res is not None:
        start_ref = res.ref.model_copy(update={"text": when or res.ref.text})
        audit.update(
            rung=res.rung, basis_label=res.basis_label, evidence_precision=res.evidence_precision
        )
        notes[f"temporal_rung{res.rung}"] += 1
    elif rel_ref is not None:
        start_ref = rel_ref
        audit.update(rung=4, basis_label="relation", evidence_precision="unknown")
    else:
        start_ref = start0
        if start0 is not None:
            notes["temporal_unknown"] += 1
    end_ref = (
        end_res.ref.model_copy(update={"text": until or end_res.ref.text})
        if end_res is not None
        else end0
    )
    if relations:
        audit["relations"] = relations
    if start_ref is None and end_ref is None:
        return env
    if start_ref is not None and start_ref is start0 and end_ref is end0:
        return env  # nothing new
    first_word = _tok(when)[0][0] if when and _tok(when) else ""
    open_start = first_word in _OPEN_START
    interval = Interval(start=start_ref, end=end_ref)
    if start_ref is None and end_ref is not None:
        interval = Interval(start=None, end=end_ref)
    elif open_start:
        interval = Interval(start=start_ref, end=None)
    time = env.time.model_copy(update={"event": interval})
    assess = env.assessment.model_copy(update={"time_precision": time.event_precision})
    extras = dict(env.extras)
    if audit:
        extras[EXTRAS_KEY] = audit
    return env.model_copy(update={"time": time, "assessment": assess, "extras": extras})
