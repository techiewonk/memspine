# ruff: noqa: RUF001
"""Deterministic grounding rule checks (LIB-03, W-17).

Decides whether an extracted assertion is supported by the turns it cites, *before* any model
adjudicator. Pure and unwired: no I/O, no model call, no engine import (wiring is W2-08). Only
stdlib, pydantic and ``memspine.schemas`` are used.

What the rules can and cannot do
--------------------------------
The rules check citations (turns exist, quoted spans occur), names, numbers and units, months,
weekdays and relative dates, negation, modality cues, the speaker or pronoun binding of the subject,
and (when the assertion and the source share a language) whether every content word is covered.
They never guess: when the evidence is not enough (different languages for the free text, an
ambiguous pronoun, mixed polarity in the cited clauses, unmatched paraphrase) the label is
``undecided`` and a later local entailment model or the decider takes over (W-17).

Label policy
------------
* ``unsupported``: a *hard* failure (a cited turn does not exist, flipped polarity, a modality cue that
  contradicts the claimed modality on every anchor, an unresolvable or wrong relative date, a wrong
  speaker) or a local failure with nothing verified beside it.
* ``partly``: a local failure (number, unit, name, weekday/month, a coordinated extra item, a modality
  cue on only some anchors) next to at least one verified element or an untouched conjunct.
* ``supported``: every check that ran passed and none is undecided.
* ``undecided``: no failure, but at least one check could not be decided.

Known limits (documented, not hidden): cross-language exonyms (``Cologne`` vs ``Köln``) that are not
close in spelling count as a name mismatch; present-progressive plans ("we are booking") carry no cue;
a negation that scopes over only one conjunct of the assertion is not tracked; Japanese has cue lists
and numerals but no morphology.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Literal

from pydantic import ConfigDict, Field

from memspine.schemas import InternalModel
from memspine.schemas.durable.assertion import AssertionEnvelope, GroundingChecks
from memspine.schemas.durable.evidence import TurnRef

__all__ = [
    "CheckOutcome",
    "CheckStatus",
    "Cite",
    "CitedSpan",
    "Claim",
    "FoundSpan",
    "GroundingLabel",
    "GroundingResult",
    "WindowTurn",
    "check_grounding",
    "claim_from_envelope",
    "find_normalised",
    "normalise",
    "turn_key",
    "window_from_turns",
]

CheckStatus = Literal["pass", "fail", "undecided", "skipped"]
GroundingLabel = Literal["supported", "partly", "unsupported", "undecided"]
Modality = Literal["asserted", "negated", "hypothetical", "planned", "reported", "question"]
Severity = Literal["hard", "local", "soft"]


# --------------------------------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------------------------------


class _Frozen(InternalModel):
    model_config = ConfigDict(frozen=True)


class WindowTurn(_Frozen):
    """One turn of the extraction window: a stable key, its text and (when known) its speaker id."""

    key: str = Field(min_length=1)
    text: str
    speaker: str | None = None


class CitedSpan(_Frozen):
    """A span claimed for a cited turn: character offsets plus the verbatim quote when kept."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str | None = None


class Cite(_Frozen):
    """A citation: evidence role (S4-1), the cited turn key and an optional span."""

    role: Literal["support", "binding", "context"]
    turn: str = Field(min_length=1)
    span: CitedSpan | None = None


class Claim(_Frozen):
    """The content of one assertion as the rules see it (model-authored fields plus citations)."""

    text: str = Field(min_length=1)
    modality: Modality = "asserted"
    subject: str | None = None
    object: str | None = None
    value: str | int | float | bool | None = None
    source_time: date | None = None
    cites: tuple[Cite, ...] = ()


class CheckOutcome(_Frozen):
    """The result of one named check."""

    name: str
    status: CheckStatus
    detail: str = ""
    elements: tuple[str, ...] = ()


class FoundSpan(_Frozen):
    """A span located in a cited turn. ``how`` says how it was found."""

    turn: str
    start: int
    end: int
    text: str
    how: Literal["exact", "normalised", "bounds", "clause"]


class GroundingResult(_Frozen):
    """Label, per-check outcomes, failing element names and the spans found."""

    label: GroundingLabel
    outcomes: tuple[CheckOutcome, ...] = ()
    failing: tuple[str, ...] = ()
    undecided: tuple[str, ...] = ()
    spans: tuple[FoundSpan, ...] = ()
    reason: str = ""

    def outcome(self, name: str) -> CheckOutcome | None:
        return next((o for o in self.outcomes if o.name == name), None)

    def to_checks(self) -> GroundingChecks:
        """Project onto the stored per-check booleans (``None`` = not run or undecided)."""

        def flag(*names: str) -> bool | None:
            got = [o.status for n in names if (o := self.outcome(n)) is not None]
            if "fail" in got:
                return False
            if got and all(s == "pass" for s in got):
                return True
            return None

        return GroundingChecks(
            span=flag("span"),
            numbers=flag("numbers"),
            polarity=flag("polarity"),
            names=flag("names"),
            endpoints=flag("subject"),
        )

    def to_grounding(self) -> Literal["grounded", "uncertain", "rejected"]:
        """Map onto the envelope's ``grounding`` field: only ``supported`` is grounded (W-17)."""
        return {
            "supported": "grounded",
            "unsupported": "rejected",
        }.get(self.label, "uncertain")  # type: ignore[return-value]


def turn_key(ref: TurnRef) -> str:
    """Stable window key for a pinned turn reference."""
    return f"{ref.source_id}@{ref.source_version}#{ref.seg_index}"


def window_from_turns(turns: Iterable[tuple[TurnRef, str, str | None]]) -> dict[str, WindowTurn]:
    """Build a window from ``(TurnRef, text, speaker)`` triples."""
    out: dict[str, WindowTurn] = {}
    for ref, text, speaker in turns:
        k = turn_key(ref)
        out[k] = WindowTurn(key=k, text=text, speaker=speaker)
    return out


def claim_from_envelope(env: AssertionEnvelope, *, source_time: date | None = None) -> Claim:
    """View an :class:`AssertionEnvelope` as a :class:`Claim` (citations keyed by :func:`turn_key`)."""
    cites: list[Cite] = []
    for ev in env.evidence:
        if ev.turn is None:
            continue
        sp = None
        if ev.span is not None:
            sp = CitedSpan(start=ev.span.start, end=ev.span.end, quote=ev.span.quote)
        cites.append(Cite(role=ev.role, turn=turn_key(ev.turn), span=sp))
    return Claim(
        text=env.text,
        modality=env.assessment.modality,
        subject=env.subject.surface if env.subject else None,
        object=env.object.surface if env.object else None,
        value=env.value,
        source_time=source_time,
        cites=tuple(cites),
    )


# --------------------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------------------


def _norm_chars(ch: str) -> str:
    """Normalise one character: NFKC, casefold, accent fold; separators and punctuation become ' '."""
    out: list[str] = []
    for c in unicodedata.normalize("NFKC", ch).casefold():
        for d in unicodedata.normalize("NFD", c):
            cat = unicodedata.category(d)
            if cat == "Mn":
                continue
            if d.isspace() or cat[0] in "PZC":
                out.append(" ")
            else:
                out.append(d)
    return "".join(out)


def normalise(text: str) -> str:
    """Case, whitespace, Unicode NFKC, accent and punctuation normalisation (single spaces)."""
    return " ".join("".join(_norm_chars(c) for c in text).split())


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        for c in _norm_chars(ch):
            if c == " ":
                if prev_space:
                    continue
                prev_space = True
            else:
                prev_space = False
            chars.append(c)
            idx.append(i)
    while chars and chars[-1] == " ":
        chars.pop()
        idx.pop()
    return "".join(chars), idx


def find_normalised(haystack: str, needle: str) -> tuple[int, int] | None:
    """Locate ``needle`` in ``haystack`` after normalisation; offsets refer to the original text."""
    nn = normalise(needle)
    if not nn:
        return None
    nh, idx = _norm_with_map(haystack)
    p = nh.find(nn)
    if p < 0:
        return None
    return idx[p], idx[p + len(nn) - 1] + 1


# --------------------------------------------------------------------------------------------------
# Lexicons (folded unless noted)
# --------------------------------------------------------------------------------------------------


def _words(spec: str) -> frozenset[str]:
    return frozenset(spec.split())


_STOP = _words(
    # en
    "a an the and or but of to in on at for with by from as is are was were be been being am do does "
    "did done has have had having it its this that these those there here he she they them his her "
    "their him we us our i me my you your will would can could may might shall should also then than "
    "so such very too just not no s t d ll re ve about into over under per via while when if "
    # de
    "der die das den dem des ein eine einen einem einer eines und oder aber von zu im am an auf bei "
    "mit nach fur aus ist sind war waren hat haben hatte ich du er sie es wir ihr mein meine meinen "
    "mir mich sich dort dann auch nur noch schon wenn als wie so dass "
    # nl
    "het een en maar van naar bij met voor uit is zijn was waren heeft hebben had ik jij hij zij ze "
    "wij we mijn onze dat dit die deze er ook dan nog als "
    # es
    "el la los las un una unos unas y o pero de del al en con por para es son era fue ha han he yo tu "
    "el ella nosotros mi mis su sus que se lo le ya muy tambien si"
)

_NAME_STOP = _STOP | _words(
    "according mr mrs ms dr today yesterday tomorrow last next after before once now ok yes monday "
    "tuesday wednesday thursday friday saturday sunday and however although because since until "
    "heute gestern morgen vandaag gisteren hoy ayer"
)

_CUE_WORDS = _words(
    # words dropped from content tokens because the cue checks handle them
    "plan plans planned planning going intend intends says say said told tells claims claim claimed "
    "asked ask asks whether maybe perhaps probably possibly likely"
)

_NEG = _words(
    "not no never none nobody nothing neither nor without cannot nicht kein keine keinen keinem "
    "keiner keines nie niemals ohne niet geen nooit zonder niemand nunca ni sin ningun ninguno "
    "ninguna nadie nada nadie"
)
_NEG_SUBSTR = ("ない", "ません", "なかった", "無い", "ず、")
_HYP = _words(
    "if would could might wenn falls würde würden könnte könnten hätte hätten zou zouden zoude "
    "si sería serian podría ojalá"
)
_HYP_SUBSTR = ("もし", "たら", "れば", "ならば")
_UNC = _words(
    "maybe perhaps probably possibly vielleicht wahrscheinlich eventuell misschien waarschijnlijk "
    "quizá quizás probablemente"
)
_UNC_SUBSTR = ("多分", "たぶん", "かも", "tal vez")
_PLAN = _words("will wollen werde werden zal zullen shall")
_PLAN_SUBSTR = ("going to", "voy a", "vas a", "va a", "予定", "つもり", "plan to", "plans to")
_REP = _words(
    "says said told tells claims claimed according sagt sagte sagen erzählt erzählte laut zegt zei "
    "vertelde volgens dice dijo dicen según"
)
_REP_SUBSTR = ("と言って", "と言っ", "によると", "らしい")
_Q_SUBSTR = ("?", "？", "whether")

_FIRST_PERSON = _words(
    "i me my mine we us our ich mir mich mein meine meinen wir unser ik mij mijn we ons onze yo mi "
    "mis nosotros nuestro 私 わたし 僕 ぼく 俺 わたくし"
)
_FIRST_SUBJ = _words("i we ich wir ik wij yo nosotros 私 わたし 僕 ぼく 俺")
_THIRD_PRON = _words(
    "he she they him her his hers them er sie ihm ihr hij zij ze hem haar él ella ellos ellas 彼 彼女"
)

_RELATION = _words(
    "brother sister mother father mom dad wife husband partner friend neighbour neighbor colleague "
    "cousin uncle aunt boss son daughter grandmother grandfather bruder schwester mutter vater frau "
    "mann freund freundin nachbar nachbarin kollege kollegin cousine onkel tante oma opa sohn tochter "
    "broer zus moeder vader vriend vriendin buurman buurvrouw collega neef nicht oom tante hermano "
    "hermana madre padre esposa esposo amigo amiga vecino vecina colega primo prima tio tia hijo hija"
)
_PRODROP = _words("de el la los las en con que mi mis por para una un del al su sus")

_CONJ = _words("and und en y e & et")

_MONTHS: dict[str, int] = {}
for _n, _names in enumerate(
    [
        "january januar januari enero",
        "february februar februari febrero",
        "march marz maart marzo",
        "april abril",
        "mai mei mayo",
        "june juni junio",
        "july juli julio",
        "august augustus agosto",
        "september septiembre setiembre",
        "october oktober octubre",
        "november noviembre",
        "december dezember diciembre",
    ],
    start=1,
):
    for _w in _names.split():
        _MONTHS[_w] = _n

_WEEKDAYS: dict[str, int] = {}
for _n, _names in enumerate(
    [
        "monday montag maandag lunes",
        "tuesday dienstag dinsdag martes",
        "wednesday mittwoch woensdag miercoles",
        "thursday donnerstag donderdag jueves",
        "friday freitag vrijdag viernes",
        "saturday samstag sonnabend zaterdag sabado",
        "sunday sonntag zondag domingo",
    ]
):
    for _w in _names.split():
        _WEEKDAYS[_w] = _n

_UNITS: dict[str, str] = {}
for _u, _names in {
    "km": "km kilometer kilometre kilometres kilometers kilometern kilometros キロ",
    "m": "meter metre metres meters metros",
    "mile": "mile miles meilen meile mijl mijlen milla millas",
    "minute": "minute minutes minuten minuut minutos min 分",
    "hour": "hour hours stunde stunden uur uren hora horas",
    "day": "day days tag tage tagen dag dagen dia dias 日",
    "week": "week weeks woche wochen weken semana semanas 週",
    "month": "month months monat monate monaten maand maanden mes meses",
    "year": "year years jahr jahre jahren jaar jaren ano anos 年 歳",
    "night": "night nights nacht nachte nachten noche noches",
    "yen": "yen 円",
    "euro": "euro euros eur €",
    "dollar": "dollar dollars usd $",
    "kg": "kg kilogram kilogramm kilo kilos",
}.items():
    for _w in _names.split():
        _UNITS[_w] = _u
_DURATION_UNITS = frozenset({"minute", "hour", "day", "week", "month", "year", "night"})
_ARTICLES = _words("a an ein eine einen een un una")

_NUMWORDS: dict[str, int] = {}
for _vals, _spec in {
    0: "zero null nul cero",
    1: "eins",
    2: "two zwei twee dos",
    3: "three drei drie tres",
    4: "four vier vier cuatro",
    5: "five funf vijf cinco",
    6: "six sechs zes seis",
    7: "seven sieben zeven siete",
    8: "eight acht acht ocho",
    9: "nine neun negen nueve",
    10: "ten zehn tien diez",
    11: "eleven elf elf once",
    12: "twelve zwolf twaalf doce",
    13: "thirteen dreizehn dertien trece",
    14: "fourteen vierzehn veertien catorce",
    15: "fifteen funfzehn vijftien quince",
    16: "sixteen sechzehn zestien dieciseis",
    17: "seventeen siebzehn zeventien diecisiete",
    18: "eighteen achtzehn achttien dieciocho",
    19: "nineteen neunzehn negentien diecinueve",
    20: "twenty zwanzig twintig veinte",
    30: "thirty dreissig dertig treinta",
    40: "forty vierzig veertig cuarenta",
    50: "fifty funfzig vijftig cincuenta",
    60: "sixty sechzig zestig sesenta",
    70: "seventy siebzig zeventig setenta",
    80: "eighty achtzig tachtig ochenta",
    90: "ninety neunzig negentig noventa",
    100: "hundred hundert honderd cien",
}.items():
    for _w in _spec.split():
        _NUMWORDS[_w] = _vals
_KANJI_DIGITS = {
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_KANJI_SMALL = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG = {"万": 10_000}

_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_JA_MONTH = re.compile(r"(\d{1,2})月")
_NUM_TOKEN = re.compile(
    r"\d{1,3}(?:[,.]\d{3})+(?!\d)|\d+(?:[.,]\d+)?|[〇一二三四五六七八九十百千万]+|[^\W\d_]+|\S",
    re.UNICODE,
)
_WORD = re.compile(r"\w+(?:[’'\-]\w+)*", re.UNICODE)

# relative-date words: (resolved offset in days) and direction words for weekdays
_REL_DAY = {
    "yesterday": -1, "gestern": -1, "gisteren": -1, "ayer": -1, "昨日": -1, "きのう": -1,
    "today": 0, "heute": 0, "vandaag": 0, "hoy": 0, "今日": 0, "きょう": 0,
    "tomorrow": 1, "morgen": 1, "manana": 1, "明日": 1, "あした": 1,
}  # fmt: skip
_REL_PAST = _words(
    "last previous past letzten letzte letzter letztes vergangenen vergangene vorigen vorige "
    "afgelopen pasado pasada"
)
_REL_NEXT = _words(
    "next coming nachsten nachste nachster kommenden kommende volgende volgend proximo proxima"
)


# --------------------------------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------------------------------


def _cjk_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    cjk = sum(1 for c in letters if "぀" <= c <= "ヿ" or "一" <= c <= "鿿")
    return cjk / len(letters)


def _latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if c.isascii() or "À" <= c <= "ɏ") / len(letters)


_DE_HINT = _words(
    "ich nicht und der die das ein eine mein meine ist sind habe haben wir wenn nach nächsten "
    "seit auf mit bei wohnt arbeite arbeiten gestern würde werde"
)


def _looks_german(text: str) -> bool:
    toks = {w.casefold() for w in _WORD.findall(text)}
    return len(toks & _DE_HINT) >= 2


def _prodrop(text: str) -> bool:
    """Languages without an explicit first-person pronoun (Spanish, Japanese): skip speaker rules."""
    if _cjk_ratio(text) > 0.3:
        return True
    return len(set(_lower_tokens(text)) & _PRODROP) >= 2


def _person_names(text: str, speakers: set[str]) -> list[str]:
    """Names in ``text`` that are probably people: a window speaker or a name after a relation word."""
    words = [m.group(0) for m in _WORD.finditer(text)]
    out: list[str] = []
    for i, w in enumerate(words):
        w2 = re.sub(r"[’']s$", "", w)
        if not w2[:1].isupper() or normalise(w2) in _NAME_STOP:
            continue
        prev = normalise(words[i - 1]) if i else ""
        if normalise(w2) in speakers or prev in _RELATION:
            out.append(w2)
    return out


def _has_first_subj(text: str) -> bool:
    low = unicodedata.normalize("NFKC", text).casefold()
    toks = set(_lower_tokens(text))
    return bool(toks & _FIRST_SUBJ) or any(not p.isascii() and p in low for p in _FIRST_SUBJ)


def _stem(tok: str) -> str:
    for _ in range(2):
        for suf in ("ing", "ed", "es", "s", "en", "e", "n", "t"):
            if tok.endswith(suf) and len(tok) - len(suf) >= 3:
                tok = tok[: -len(suf)]
                break
    return tok


def _similar(a: str, b: str, floor: float = 0.75) -> bool:
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or a[0] != b[0]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= floor


def _clauses(text: str) -> list[tuple[int, int]]:
    """Clause offsets: split after , ; : . ! ? (not inside numbers) and before but/aber/maar/pero."""
    seps = ",;:.!?、。，！？"
    segs: list[tuple[int, int]] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in seps and not (
            ch in ",." and 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit()
        ):
            j = i + 1
            while j < n and text[j] in seps:
                j += 1
            segs.append((start, j))
            start = i = j
            continue
        i += 1
    if start < n:
        segs.append((start, n))
    out: list[tuple[int, int]] = []
    for s, e in segs:
        seg = text[s:e]
        cuts = [s]
        for cm in re.finditer(r"(?<!\w)(?:but|aber|maar|pero)(?!\w)", seg, flags=re.IGNORECASE):
            cuts.append(s + cm.start())
        cuts.append(e)
        for a, b in pairwise(cuts):
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            if b > a:
                out.append((a, b))
    return out or ([(0, len(text))] if text.strip() else [])


def _lower_tokens(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(unicodedata.normalize("NFKC", text))]


_SUBORDINATE = re.compile(
    r"(?<!\w)(?:if|when|unless|while|because|although|wenn|falls|solange|weil|als|si|cuando|"
    r"als|toen|omdat)(?!\w)|もし|ので|から",
    re.IGNORECASE,
)


def _main_part(text: str) -> str:
    """The assertion without its conditional or temporal subordinate clause (negation there does
    not flip the main polarity)."""
    m = _SUBORDINATE.search(text)
    if m is None:
        return text
    head = text[: m.start()].strip()
    return head if len(head) >= 3 else text


def _cue_kinds(clause: str) -> set[str]:
    """Modality/negation cue kinds present in one clause: neg, hyp, unc, plan, rep, q."""
    low = unicodedata.normalize("NFKC", clause).casefold()
    toks = set(_lower_tokens(clause))
    kinds: set[str] = set()
    if toks & _NEG or re.search(r"n['’]t\b", low) or any(s in low for s in _NEG_SUBSTR):
        kinds.add("neg")
    if toks & _HYP or any(s in low for s in _HYP_SUBSTR):
        kinds.add("hyp")
    if toks & _UNC or any(s in low for s in _UNC_SUBSTR):
        kinds.add("unc")
    if toks & _PLAN or any(s in low for s in _PLAN_SUBSTR):
        kinds.add("plan")
    if toks & _REP or any(s in low for s in _REP_SUBSTR):
        kinds.add("rep")
    if any(s in low for s in _Q_SUBSTR):
        kinds.add("q")
    return kinds


# --------------------------------------------------------------------------------------------------
# Quantities, dates, names
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Qty:
    value: float
    unit: str | None
    raw: str


def _kanji_value(s: str) -> int | None:
    total, block, big = 0, 0, 0
    digit: int | None = None
    for ch in s:
        if ch in _KANJI_DIGITS:
            digit = _KANJI_DIGITS[ch] if digit is None else digit * 10 + _KANJI_DIGITS[ch]
        elif ch in _KANJI_SMALL:
            block += (digit if digit is not None else 1) * _KANJI_SMALL[ch]
            digit = None
        elif ch in _KANJI_BIG:
            big = (total + block + (digit or 0)) or 1
            total = big * _KANJI_BIG[ch]
            block, digit = 0, None
        else:
            return None
    return total + block + (digit or 0)


def _parse_digits(tok: str) -> float | None:
    try:
        if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", tok):
            return float(re.sub(r"[,.]", "", tok))
        return float(tok.replace(",", "."))
    except ValueError:
        return None


_KANJI_RUN_COUNTER = re.compile(r"([〇一二三四五六七八九十百千万]+)(円|年|日|週|分|歳|キロ)")


def _strip_dates(text: str) -> str:
    return _JA_MONTH.sub(" ", _DATE_ISO.sub(" ", text))


def _split_kanji_quantities(text: str) -> str:
    """Kanji numerals are quantities only when a counter follows (一緒 'together' is not a 1)."""
    return _KANJI_RUN_COUNTER.sub(r" \1 \2 ", text)


def _quantities(text: str) -> list[_Qty]:
    """Numbers (digits, number words 0-20 and tens in en/de/nl/es, kanji) with an optional unit."""
    raw = _strip_dates(text)
    toks = _NUM_TOKEN.findall(_split_kanji_quantities(unicodedata.normalize("NFKC", raw)))
    folded = [normalise(t) or t for t in toks]
    out: list[_Qty] = []
    i = 0
    while i < len(toks):
        tok, f = toks[i], folded[i]
        val: float | None = None
        used = 1
        if tok[0].isdigit():
            val = _parse_digits(tok)
        elif tok[0] in _KANJI_DIGITS or tok[0] in _KANJI_SMALL or tok[0] in _KANJI_BIG:
            kv = _kanji_value(tok)
            val = float(kv) if kv is not None and (kv > 0 or tok == "〇") else None
        elif f in _NUMWORDS:
            val = float(_NUMWORDS[f])
            nxt = folded[i + 1] if i + 1 < len(toks) else ""
            if val >= 20 and val % 10 == 0 and nxt in _NUMWORDS and 0 < _NUMWORDS[nxt] < 10:
                val += _NUMWORDS[nxt]
                used = 2
        elif f in _ARTICLES and i + 1 < len(toks) and _UNITS.get(folded[i + 1]) in _DURATION_UNITS:
            out.append(_Qty(1.0, _UNITS[folded[i + 1]], f"{tok} {toks[i + 1]}"))
            i += 2
            continue
        if val is not None:
            nxt = folded[i + used] if i + used < len(toks) else ""
            unit = _UNITS.get(nxt)
            if f == "one" and unit is None:  # "no one", "someone": not a quantity
                i += used
                continue
            out.append(_Qty(val, unit, " ".join(toks[i : i + used + (1 if unit else 0)])))
            i += used + (1 if unit else 0)
            continue
        i += 1
    return out


def _iso_dates(text: str) -> set[date]:
    out: set[date] = set()
    for y, m, d in _DATE_ISO.findall(text):
        try:
            out.add(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def _lookup_plural(table: Mapping[str, int], tok: str) -> int | None:
    if tok in table:
        return table[tok]
    if tok.endswith("s") and tok[:-1] in table:
        return table[tok[:-1]]
    return None


def _months_weekdays(text: str) -> tuple[set[int], set[int]]:
    months = {int(m) for m in _JA_MONTH.findall(text) if 1 <= int(m) <= 12}
    weekdays: set[int] = set()
    for tok in normalise(_strip_iso_only(text)).split():
        mo = _MONTHS.get(tok)
        if mo:
            months.add(mo)
        wd = _lookup_plural(_WEEKDAYS, tok)
        if wd is not None:
            weekdays.add(wd)
    return months, weekdays


def _strip_iso_only(text: str) -> str:
    return _DATE_ISO.sub(" ", text)


def _relative_dates(text: str, anchor: date | None) -> tuple[bool, set[date]]:
    """Relative-date cues in ``text``: (any cue found, resolved dates given ``anchor``)."""
    toks = normalise(text).split()
    raw_toks = unicodedata.normalize("NFKC", text).split()
    found = False
    resolved: set[date] = set()
    for tok in raw_toks:  # ja words are not space separated
        for w, off in _REL_DAY.items():
            if "぀" <= w[0] <= "鿿" and w in tok:
                found = True
                if anchor:
                    resolved.add(anchor + timedelta(days=off))
    for i, tok in enumerate(toks):
        if tok in _REL_DAY and tok != "morgen" and tok.isascii():
            found = True
            if anchor:
                resolved.add(anchor + timedelta(days=_REL_DAY[tok]))
        elif tok == "morgen" and (i == 0 or toks[i - 1] not in {"guten", "am", "heute"}):
            found = True
            if anchor:
                resolved.add(anchor + timedelta(days=1))
        wd = _lookup_plural(_WEEKDAYS, tok)
        if wd is None:
            continue
        near = set(toks[max(0, i - 2) : i]) | set(toks[i + 1 : i + 2])
        direction = 0
        if near & _REL_PAST:
            direction = -1
        elif near & _REL_NEXT:
            direction = 1
        if direction == 0:
            continue
        found = True
        if anchor:
            delta = (wd - anchor.weekday()) % 7
            if direction > 0:
                resolved.add(anchor + timedelta(days=delta or 7))
            else:
                resolved.add(anchor - timedelta(days=(anchor.weekday() - wd) % 7 or 7))
    return found, resolved


def _name_candidates(text: str, *, lenient: bool) -> list[tuple[str, int]]:
    """Capitalised words that are probably names: ``(surface, word index)``."""
    out: list[tuple[str, int]] = []
    for i, m in enumerate(_WORD.finditer(text)):
        w = m.group(0)
        w = re.sub(r"[’']s$", "", w)
        if not w or not w[0].isalpha() or not w[0].isupper():
            continue
        f = normalise(w)
        if not f or f in _NAME_STOP or f in _MONTHS or _lookup_plural(_WEEKDAYS, f) is not None:
            continue
        if lenient and i > 0:  # German capitalises nouns: only the first word is a safe candidate
            continue
        out.append((w, i))
    return out


# --------------------------------------------------------------------------------------------------
# Support index
# --------------------------------------------------------------------------------------------------


@dataclass
class _Clause:
    turn: WindowTurn
    start: int
    end: int
    text: str
    words: set[str] = field(default_factory=set)
    stems: set[str] = field(default_factory=set)
    cues: set[str] = field(default_factory=set)
    months: set[int] = field(default_factory=set)
    weekdays: set[int] = field(default_factory=set)
    quantities: list[_Qty] = field(default_factory=list)


def _build_clauses(turns: Sequence[WindowTurn]) -> list[_Clause]:
    out: list[_Clause] = []
    for t in turns:
        for s, e in _clauses(t.text):
            seg = t.text[s:e]
            norm = normalise(seg)
            words = set(norm.split())
            mo, wd = _months_weekdays(seg)
            out.append(
                _Clause(
                    turn=t,
                    start=s,
                    end=e,
                    text=seg,
                    words=words | _cjk_bigrams(seg),
                    stems={_stem(w) for w in words},
                    cues=_cue_kinds(seg),
                    months=mo,
                    weekdays=wd,
                    quantities=_quantities(seg),
                )
            )
    return out


def _cjk_bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for run in re.findall(r"[぀-ヿ一-鿿]+", unicodedata.normalize("NFKC", text)):
        out.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            out.add(run)
    return out


def _word_match(name_f: str, words: set[str]) -> bool:
    return any(_similar(name_f, w) for w in words)


# --------------------------------------------------------------------------------------------------
# The checker
# --------------------------------------------------------------------------------------------------

_HARD = {"citation", "polarity", "binding", "subject", "modality_all"}


@dataclass
class _Acc:
    outcomes: list[CheckOutcome] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    undecided: list[str] = field(default_factory=list)
    severities: list[Severity] = field(default_factory=list)
    positives: int = 0
    failing_text: list[str] = field(default_factory=list)
    probe: str = ""

    def add(
        self,
        name: str,
        status: CheckStatus,
        detail: str = "",
        elements: Sequence[str] = (),
        severity: Severity = "local",
    ) -> None:
        self.outcomes.append(
            CheckOutcome(name=name, status=status, detail=detail, elements=tuple(elements))
        )
        if status == "fail":
            self.failing.extend(f"{name}:{e}" if e else name for e in (elements or ("",)))
            self.failing_text.extend(elements)
            self.severities.append(severity)
        elif status == "undecided":
            self.undecided.append(name)


def check_grounding(
    claim: Claim, window: Mapping[str, WindowTurn] | Iterable[WindowTurn]
) -> GroundingResult:
    """Run every rule check for ``claim`` against its cited turns in ``window``."""
    turns: dict[str, WindowTurn] = (
        dict(window) if isinstance(window, Mapping) else {t.key: t for t in window}
    )
    acc = _Acc()
    spans: list[FoundSpan] = []

    # ---- 1. citations ------------------------------------------------------------------------
    cited = [c for c in claim.cites if c.role in ("support", "binding")]
    missing = sorted({c.turn for c in cited if c.turn not in turns})
    if not cited:
        acc.add("citation", "fail", "no support or binding evidence is cited", ["none"], "hard")
        return _finish(acc, spans, "no cited evidence")
    if missing:
        acc.add("citation", "fail", "a cited turn is not in the window", missing, "hard")
        return _finish(acc, spans, "cited turn missing")
    acc.add("citation", "pass", f"{len(cited)} cited turn(s) found")

    support_keys = _dedupe(c.turn for c in cited if c.role == "support")
    binding_keys = _dedupe(c.turn for c in cited if c.role == "binding")
    sup_turns = [turns[k] for k in support_keys] or [turns[k] for k in binding_keys]
    bind_turns = [turns[k] for k in binding_keys]

    # ---- 1b. spans ---------------------------------------------------------------------------
    _check_spans(claim, turns, acc, spans)

    # ---- 2. content features -----------------------------------------------------------------
    clauses = _build_clauses(sup_turns)
    if not clauses:
        acc.add("content", "undecided", "cited turns are empty")
        return _finish(acc, spans, "empty evidence")
    probe = claim.text
    for extra in (claim.subject, claim.object, claim.value):
        if extra is not None and normalise(str(extra)) not in normalise(claim.text):
            probe += f" {extra}"
    acc.probe = probe
    german = _looks_german(probe)
    all_text = " ".join(t.text for t in sup_turns)
    name_turns = _dedupe_turns([*sup_turns, *bind_turns])
    hay_words: set[str] = set()
    for t in name_turns:
        hay_words.update(normalise(t.text).split())
        if t.speaker:
            hay_words.update(normalise(t.speaker).split())
    latin_support = _latin_ratio(" ".join(t.text for t in name_turns)) >= 0.5

    speakers = {normalise(t.speaker) for t in turns.values() if t.speaker}
    names = _name_candidates(probe, lenient=german)
    subject = claim.subject or next((n for n, i in names if i == 0), None)
    anchors = _anchor_clauses(probe, names, clauses)

    _check_names(names, subject, hay_words, latin_support, acc)
    _check_numbers(probe, clauses, acc)
    _check_dates(probe, clauses, all_text, claim.source_time, acc)
    best = _best_clauses(probe, names, subject, clauses)
    _check_polarity(claim, probe, best, clauses, acc)
    _check_modality(claim, anchors, best, clauses, acc)
    _check_subject(speakers, subject, names, best, clauses, bind_turns, german, acc)
    _check_content(probe, names, subject, clauses, acc)

    for c in best[:1]:
        spans.append(
            FoundSpan(turn=c.turn.key, start=c.start, end=c.end, text=c.text, how="clause")
        )
    return _finish(acc, spans, "")


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _dedupe_turns(items: Iterable[WindowTurn]) -> list[WindowTurn]:
    seen: dict[str, WindowTurn] = {}
    for t in items:
        seen.setdefault(t.key, t)
    return list(seen.values())


# ---- spans ------------------------------------------------------------------------------------


def _check_spans(
    claim: Claim, turns: Mapping[str, WindowTurn], acc: _Acc, spans: list[FoundSpan]
) -> None:
    bad: list[str] = []
    n = 0
    for c in claim.cites:
        if c.role == "context" or c.span is None:
            continue
        n += 1
        text = turns[c.turn].text
        sp = c.span
        if sp.end > len(text):
            bad.append(f"{c.turn}[{sp.start}:{sp.end}] outside the turn")
            continue
        sliced = text[sp.start : sp.end]
        if sp.quote is None:
            spans.append(
                FoundSpan(turn=c.turn, start=sp.start, end=sp.end, text=sliced, how="bounds")
            )
        elif sliced == sp.quote:
            spans.append(
                FoundSpan(turn=c.turn, start=sp.start, end=sp.end, text=sliced, how="exact")
            )
        elif (hit := find_normalised(text, sp.quote)) is not None:
            spans.append(
                FoundSpan(
                    turn=c.turn,
                    start=hit[0],
                    end=hit[1],
                    text=text[hit[0] : hit[1]],
                    how="normalised",
                )
            )
        else:
            bad.append(f"{c.turn}: quote not in turn")
    if n == 0:
        acc.add("span", "skipped", "no span given")
    elif bad:
        acc.add("span", "fail", "; ".join(bad), bad, "hard")
    else:
        acc.add("span", "pass", f"{n} span(s) found")


# ---- names ------------------------------------------------------------------------------------


def _check_names(
    names: list[tuple[str, int]],
    subject: str | None,
    hay: set[str],
    latin_support: bool,
    acc: _Acc,
) -> None:
    if not names:
        acc.add("names", "skipped", "no name in the assertion")
        return
    wrong: list[str] = []
    unknown: list[str] = []
    ok: list[str] = []
    for surface, _ in names:
        f = normalise(surface)
        if not f:
            continue
        parts = f.split()
        if all(_word_match(p, hay) for p in parts):
            ok.append(surface)
        elif not latin_support or (not surface.isascii() and not surface.isalpha()):
            unknown.append(surface)
        else:
            wrong.append(surface)
    acc.positives += sum(1 for s in ok if s != subject)
    if wrong:
        acc.add("names", "fail", "name not in the cited turns", wrong)
    elif unknown:
        acc.add("names", "undecided", "different script, cannot compare", unknown)
    else:
        acc.add("names", "pass", "all names found", ok)


# ---- numbers ----------------------------------------------------------------------------------


def _check_numbers(probe: str, clauses: list[_Clause], acc: _Acc) -> None:
    want = _quantities(probe)
    if not want:
        acc.add("numbers", "skipped", "no number in the assertion")
        return
    have = [q for c in clauses for q in c.quantities]
    bad_num: list[str] = []
    bad_unit: list[str] = []
    for q in want:
        same = [h for h in have if h.value == q.value]
        if not same:
            bad_num.append(q.raw)
        elif (
            q.unit and any(h.unit for h in same) and not any(h.unit in (None, q.unit) for h in same)
        ):
            bad_unit.append(q.raw)
        else:
            acc.positives += 1
    if bad_num or bad_unit:
        acc.add(
            "numbers", "fail", "number or unit differs from the evidence", [*bad_num, *bad_unit]
        )
    else:
        acc.add("numbers", "pass", "all numbers found", [q.raw for q in want])


# ---- dates ------------------------------------------------------------------------------------


def _check_dates(
    probe: str, clauses: list[_Clause], all_text: str, anchor: date | None, acc: _Acc
) -> None:
    want_iso = _iso_dates(probe)
    want_m, want_w = _months_weekdays(probe)
    have_m = set().union(*(c.months for c in clauses))
    have_w = set().union(*(c.weekdays for c in clauses))
    local_bad: list[str] = []
    if want_m and not want_m <= have_m:
        local_bad.extend(f"month {m}" for m in sorted(want_m - have_m))
    if want_w and not want_w <= have_w:
        local_bad.extend(f"weekday {w}" for w in sorted(want_w - have_w))
    acc.positives += len(want_m & have_m) + len(want_w & have_w)
    if local_bad:
        acc.add("dates", "fail", "month or weekday not in the evidence", local_bad)
    if want_iso:
        lit = _iso_dates(all_text)
        found, resolved = _relative_dates(all_text, anchor)
        bad: list[str] = []
        unknown: list[str] = []
        for d in sorted(want_iso):
            if d in lit or d in resolved:
                acc.positives += 1
            elif found and anchor is None:
                bad.append(f"{d.isoformat()} (relative date, no source time or binding)")
            elif found and resolved:
                bad.append(
                    f"{d.isoformat()} (evidence resolves to {sorted(x.isoformat() for x in resolved)})"
                )
            else:
                unknown.append(d.isoformat())
        if bad:
            acc.add(
                "binding",
                "fail",
                "relative date does not resolve to the asserted date",
                bad,
                "hard",
            )
        elif unknown:
            acc.add("binding", "undecided", "date not derivable from the evidence", unknown)
        else:
            acc.add("binding", "pass", "dates resolved")
    elif not local_bad and (want_m or want_w):
        acc.add("dates", "pass", "months and weekdays found")
    elif not (want_m or want_w):
        acc.add("dates", "skipped", "no date in the assertion")


# ---- clause selection -------------------------------------------------------------------------


def _anchor_forms(probe: str, names: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """(kind, folded form) anchors: names, numbers, months, weekdays."""
    out = [("name", normalise(n)) for n, _ in names if normalise(n)]
    out += [("num", f"{q.value:g}") for q in _quantities(probe)]
    months, weekdays = _months_weekdays(probe)
    out += [("month", str(m)) for m in months]
    out += [("wd", str(w)) for w in weekdays]
    return out


def _clause_has(c: _Clause, kind: str, form: str) -> bool:
    if kind == "name":
        return all(_word_match(p, c.words) for p in form.split())
    if kind == "num":
        return any(f"{q.value:g}" == form for q in c.quantities)
    if kind == "month":
        return int(form) in c.months
    return int(form) in c.weekdays


def _anchor_clauses(
    probe: str, names: list[tuple[str, int]], clauses: list[_Clause]
) -> list[list[_Clause]]:
    """For each anchor found in at least one cited clause, the clauses that contain it."""
    out: list[list[_Clause]] = []
    for kind, form in _anchor_forms(probe, names):
        hits = [c for c in clauses if _clause_has(c, kind, form)]
        if hits:
            out.append(hits)
    return out


def _content_stems(probe: str, names: list[tuple[str, int]], subject: str | None) -> list[str]:
    skip = {w for n, _ in names for w in normalise(n).split()}
    if subject:
        skip.update(normalise(subject).split())
    qty_words = {t for q in _quantities(probe) for t in normalise(q.raw).split()}
    out: list[str] = []
    for w in normalise(_strip_dates(probe)).split():
        if (
            w in skip
            or w in _STOP
            or w in _CUE_WORDS
            or w in _NEG
            or w in _HYP
            or w in _UNC
            or w in _PLAN
            or w in _REP
            or w in qty_words
            or w in _UNITS
            or w in _MONTHS
            or w in _NUMWORDS
            or _lookup_plural(_WEEKDAYS, w) is not None
            or len(w) < 2
            or w.isdigit()
        ):
            continue
        out.append(w)
    return out


def _best_clauses(
    probe: str, names: list[tuple[str, int]], subject: str | None, clauses: list[_Clause]
) -> list[_Clause]:
    anchors = _anchor_forms(probe, names)
    gen = {_stem(w) for w in _content_stems(probe, names, subject)}
    cjk = _cjk_bigrams(probe)
    scored: list[tuple[int, _Clause]] = []
    for c in clauses:
        s = 2 * sum(1 for k, f in anchors if _clause_has(c, k, f))
        s += sum(1 for g in gen if g in c.stems or any(_similar(g, x, 0.86) for x in c.stems))
        s += len(cjk & c.words)
        scored.append((s, c))
    top = max((s for s, _ in scored), default=0)
    if top == 0:
        return clauses[:1] if len(clauses) == 1 else []
    return [c for s, c in scored if s == top]


# ---- polarity ---------------------------------------------------------------------------------


def _check_polarity(
    claim: Claim, probe: str, best: list[_Clause], clauses: list[_Clause], acc: _Acc
) -> None:
    want_neg = claim.modality == "negated" or "neg" in _cue_kinds(_main_part(claim.text))
    pool = best
    if not pool:
        pool = clauses
    pol = {"neg" in c.cues for c in pool}
    if len(pol) != 1:
        acc.add("polarity", "undecided", "cited clauses disagree on polarity or none matches")
        return
    have_neg = pol.pop()
    if have_neg == want_neg:
        acc.add("polarity", "pass", "negation agrees")
    else:
        what = (
            "affirmed but the evidence is negated"
            if have_neg
            else "negated but the evidence is affirmative"
        )
        acc.add("polarity", "fail", what, ["negated" if want_neg else "affirmed"], "hard")


# ---- modality ---------------------------------------------------------------------------------

_CONTRADICTS: dict[str, set[str]] = {
    "asserted": {"hyp", "unc", "q", "plan", "rep"},
    "planned": {"hyp", "unc", "q"},
    "negated": {"hyp", "unc", "q"},
    "reported": {"q"},
    "hypothetical": {"q"},
    "question": set(),
}


def _check_modality(
    claim: Claim,
    anchors: list[list[_Clause]],
    best: list[_Clause],
    clauses: list[_Clause],
    acc: _Acc,
) -> None:
    bad_cues = _CONTRADICTS[claim.modality]

    def contradicted(group: list[_Clause]) -> set[str]:
        # an anchor is contradicted only if every clause that holds it carries a contradicting cue
        per = [c.cues & bad_cues for c in group]
        return set.intersection(*per) if all(per) else set()

    groups = anchors or ([best] if best else [])
    if not groups:
        acc.add("modality", "undecided", "no clause could be matched to the assertion")
        return
    hits = [contradicted(g) for g in groups]
    bad = [h for h in hits if h]
    if not bad:
        acc.add("modality", "pass", f"no cue contradicts the claimed modality ({claim.modality})")
        return
    cues = sorted(set().union(*bad))
    soft = cues == ["rep"]
    if len(bad) == len(groups):
        acc.add(
            "modality",
            "fail",
            "cue contradicts the claimed modality",
            [f"{claim.modality} vs {','.join(cues)}"],
            "soft" if soft else "hard",
        )
    else:
        acc.add(
            "modality",
            "fail",
            "cue contradicts the claimed modality for part of the claim",
            [f"{claim.modality} vs {','.join(cues)}"],
            "local",
        )
        acc.positives += len(groups) - len(bad)


# ---- subject / speaker / binding --------------------------------------------------------------


def _check_subject(
    speakers: set[str],
    subject: str | None,
    names: list[tuple[str, int]],
    best: list[_Clause],
    clauses: list[_Clause],
    bind_turns: list[WindowTurn],
    german: bool,
    acc: _Acc,
) -> None:
    if not subject:
        acc.add("subject", "skipped", "no subject name")
        return
    if not best:
        acc.add("subject", "undecided", "no clause matched the assertion")
        return
    sf = normalise(subject)
    parts = sf.split()
    c = best[0]
    if all(_word_match(p, c.words) for p in parts):
        acc.add("subject", "pass", "subject named in the matching clause")
        return
    toks = set(_lower_tokens(c.text))
    spk = normalise(c.turn.speaker or "").split()
    is_speaker = bool(spk) and all(_word_match(p, set(spk)) for p in parts)
    pronoun = bool(toks & _THIRD_PRON)
    skip_rules = german or _prodrop(c.text)
    others = (
        []
        if skip_rules
        else [
            n
            for n in _person_names(c.text, speakers)
            if not all(_word_match(p, set(parts)) for p in normalise(n).split())
        ]
    )
    first = _has_first_subj(c.text)
    if is_speaker and first:
        acc.add("subject", "pass", "first-person statement by the named speaker")
        return
    if is_speaker and others:
        acc.add("subject", "fail", "the matching clause is about another person", [subject], "hard")
        return
    if pronoun:
        _bind_pronoun(subject, parts, bind_turns, acc)
        return
    if is_speaker:
        acc.add("subject", "pass", "statement by the named speaker")
        return
    if others:
        acc.add("subject", "fail", "the matching clause is about another person", [subject], "hard")
        return
    if first and spk and not skip_rules:
        acc.add(
            "subject",
            "fail",
            "first-person statement attributed to a different speaker",
            [subject],
            "hard",
        )
        return
    acc.add("subject", "undecided", "subject not tied to the matching clause")


def _bind_pronoun(subject: str, parts: list[str], bind_turns: list[WindowTurn], acc: _Acc) -> None:
    if not bind_turns:
        acc.add(
            "binding",
            "fail",
            "a pronoun stands for the subject and no binding evidence is cited",
            [subject],
            "hard",
        )
        acc.add("subject", "fail", "pronoun without cited binding", [subject], "hard")
        return
    holders = [
        t for t in bind_turns if all(_word_match(p, set(normalise(t.text).split())) for p in parts)
    ]
    if not holders:
        acc.add(
            "binding", "fail", "cited binding turn does not name the subject", [subject], "hard"
        )
        acc.add("subject", "fail", "binding turn lacks the subject", [subject], "hard")
        return
    others = {
        normalise(n)
        for t in holders
        for n, _ in _name_candidates(t.text, lenient=_looks_german(t.text))
        if normalise(n) and not all(_word_match(p, set(parts)) for p in normalise(n).split())
    }
    if others:
        acc.add("binding", "undecided", "binding turn names more than one person", sorted(others))
        return
    acc.add("binding", "pass", "pronoun bound to the subject by cited evidence")
    acc.add("subject", "pass", "subject bound")
    acc.positives += 1


# ---- content ----------------------------------------------------------------------------------


def _check_content(
    probe: str, names: list[tuple[str, int]], subject: str | None, clauses: list[_Clause], acc: _Acc
) -> None:
    gen = _content_stems(probe, names, subject)
    cjk = _cjk_bigrams(probe)
    if not gen and not cjk:
        acc.add("content", "pass", "nothing beyond names, numbers and dates to compare")
        return
    stems = set().union(*(c.stems for c in clauses))
    cwords = set().union(*(c.words for c in clauses))
    if cjk and _cjk_ratio(probe) > 0.3:
        miss = cjk - cwords
        cov = 1 - len(miss) / len(cjk)
        unmatched: list[str] = sorted(miss)
        total = len(cjk)
    else:
        unmatched = [
            w
            for w in gen
            if not (_stem(w) in stems or any(_similar(_stem(w), s, 0.86) for s in stems))
        ]
        total = len(gen)
        cov = 1 - len(unmatched) / total
    if cov < 0.5:
        acc.add(
            "content",
            "undecided",
            "little shared vocabulary with the evidence (other language?)",
            unmatched[:6],
        )
        return
    if not unmatched:
        acc.add("content", "pass", "all content words found")
        return
    tokens = _lower_tokens(probe)
    coord: list[str] = []
    for w in unmatched:
        for i, t in enumerate(tokens):
            if normalise(t) != w:
                continue
            j = i - 1
            while j >= 0 and normalise(tokens[j]) in _STOP and tokens[j] not in _CONJ:
                j -= 1
            if j >= 0 and tokens[j] in _CONJ:
                coord.append(w)
            break
    if coord:
        acc.add("content", "fail", "an added item is not in the evidence", coord)
        acc.positives += total - len(unmatched)
    else:
        acc.add("content", "undecided", "content words not found (paraphrase?)", unmatched[:6])


# --------------------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------------------


def _finish(acc: _Acc, spans: list[FoundSpan], note: str) -> GroundingResult:
    fails = acc.severities
    n_fail = sum(1 for o in acc.outcomes if o.status == "fail")
    if n_fail == 0:
        label: GroundingLabel = "undecided" if acc.undecided else "supported"
        reason = note or (
            f"undecided: {', '.join(acc.undecided)}" if acc.undecided else "all checks passed"
        )
    elif "hard" in fails:
        label, reason = "unsupported", note or "hard failure: " + ", ".join(acc.failing)
    elif fails and all(s == "soft" for s in fails):
        label, reason = "unsupported", "hearsay cue contradicts an asserted claim"
    elif acc.positives > 0 or "soft" in fails or _has_untouched_conjunct(acc):
        label, reason = (
            "partly",
            "local failure next to verified elements: " + ", ".join(acc.failing),
        )
    else:
        label, reason = (
            "unsupported",
            "local failure and nothing else verified: " + ", ".join(acc.failing),
        )
    return GroundingResult(
        label=label,
        outcomes=tuple(acc.outcomes),
        failing=tuple(acc.failing),
        undecided=tuple(acc.undecided),
        spans=tuple(spans),
        reason=reason,
    )


def _has_untouched_conjunct(acc: _Acc) -> bool:
    """True when the failing element sits in one conjunct and another conjunct has no failure."""
    text = acc.probe
    if not text or not acc.failing_text:
        return False
    parts = [p for p in re.split(r"\s+(?:and|und|en|y)\s+|,\s*", text) if p.strip()]
    if len(parts) < 2:
        return False
    keys = [normalise(str(e)) for e in acc.failing_text]
    touched = [any(k and k.split()[0] in normalise(p).split() for k in keys) for p in parts]
    return not all(touched)
