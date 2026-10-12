# ruff: noqa: E501, RUF001
"""Extraction coverage check and targeted re-extraction plan (LIB-17; decisions ER-12 B, ER-15; wired later by W2-15).

Pure and deterministic: no I/O, no model call, no engine import. Depends on ``memspine.schemas`` and
the merged name normaliser of :mod:`memspine.derive.resolve` only.

After the one normal extraction call for a window, :func:`coverage` asks, for each NEW line, two cheap
questions:

1. Does the line look information-bearing? Signals are rule-based and language-aware (English, German,
   Dutch, Spanish, plus a few caseless-script cues): named entities (capitalisation; katakana runs and
   bracket-quoted spans for caseless scripts), numbers, dates and time words, first-person statements
   of state, preference or plan, and list shapes. Greetings and filler ("ok", "haha", "danke") carry no
   signal.
2. Did any accepted envelope cite the line as ``support`` or ``binding`` evidence? ``context``
   evidence never counts (S4-1).

Each new line is ``covered``, ``uncovered_salient`` or ``not_salient``. Independently, *omission
diagnostics* list salient spans (entities, numbers, dates) that appear in a line but in none of the
envelopes citing it, which flags a partially extracted line.

:func:`plan_reextraction` turns a report into a small targeted follow-up plan under a recovery budget
(calls and tokens per source, purpose ``coverage``, ER-15): lines are grouped into small windows with
context lines, prioritised by salience, never planned twice for coverage, and every line left out
carries an explicit skip reason. The plan is advice: the caller makes the calls and records the
re-extracted lines in :attr:`CoverageBudget.already_reextracted`.

All signals are heuristics tuned to over-flag slightly (the follow-up call is cheap and bounded) while
keeping greetings and filler quiet. Raw text is always kept regardless of what this module says.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from memspine.derive.resolve import normalise_name
from memspine.schemas.durable.assertion import AssertionEnvelope

__all__ = [
    "COVERAGE_PURPOSE",
    "SALIENCE_THRESHOLD",
    "CoverageBudget",
    "CoverageReport",
    "LineCoverage",
    "Omission",
    "ReextractionPlan",
    "ReextractionWindow",
    "SalientSpan",
    "SkippedLine",
    "WindowLine",
    "coverage",
    "plan_reextraction",
]

COVERAGE_PURPOSE = "coverage"  # LlmCall.purpose for every call this module plans
SALIENCE_THRESHOLD = 1.5
LineStatus = Literal["covered", "uncovered_salient", "not_salient"]
SpanKind = Literal["entity", "number", "date"]
SkipReason = Literal["budget_calls", "budget_tokens", "already_reextracted"]

# --------------------------------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowLine:
    """One line (turn) of an extraction window.

    ``seg_index`` is the turn index envelopes cite (``TurnRef.seg_index``). ``is_new`` is False for
    context lines shown to the model but not owed coverage. When ``source_id`` / ``source_version`` are
    set, evidence must match them too (windows of one source normally leave them ``None``).
    """

    seg_index: int
    text: str
    is_new: bool = True
    source_id: str | None = None
    source_version: int | None = None


@dataclass(frozen=True, slots=True)
class SalientSpan:
    kind: SpanKind
    text: str


@dataclass(frozen=True, slots=True)
class Omission:
    """A salient span present in the line but in no envelope that cites it."""

    seg_index: int
    kind: SpanKind
    text: str


@dataclass(frozen=True, slots=True)
class LineCoverage:
    seg_index: int
    status: LineStatus
    signals: tuple[str, ...]  # subset of: entity, number, date, first_person, list
    score: float  # salience score; >= SALIENCE_THRESHOLD means salient
    spans: tuple[SalientSpan, ...]
    omissions: tuple[Omission, ...]
    cited_by: tuple[str, ...]  # write_ids of the envelopes citing this line as support/binding
    omission_score: float = 0.0


@dataclass(frozen=True, slots=True)
class CoverageReport:
    window: tuple[WindowLine, ...]
    lines: tuple[LineCoverage, ...]  # new lines only, window order
    salient_lines: int
    covered_salient_lines: int
    ratio: float  # covered salient / salient; 1.0 when nothing is salient
    omission_count: int

    @property
    def uncovered(self) -> tuple[LineCoverage, ...]:
        return tuple(ln for ln in self.lines if ln.status == "uncovered_salient")

    @property
    def omissions(self) -> tuple[Omission, ...]:
        return tuple(o for ln in self.lines for o in ln.omissions)


@dataclass(frozen=True, slots=True)
class CoverageBudget:
    """Recovery budget for purpose ``coverage`` for ONE source (ER-15).

    ``max_calls`` / ``max_tokens`` are the totals allowed for this source; ``calls_used`` /
    ``tokens_used`` what earlier plans already spent. ``already_reextracted`` holds the ``seg_index``
    values re-extracted for coverage before: they are never planned again. A window's estimated cost is
    ``call_overhead_tokens`` plus the tokens of its lines (targets and context).
    """

    max_calls: int = 2
    max_tokens: int = 4000
    calls_used: int = 0
    tokens_used: int = 0
    max_targets_per_window: int = 6
    context_lines: int = 2
    call_overhead_tokens: int = 150
    already_reextracted: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "max_calls",
            "max_tokens",
            "calls_used",
            "tokens_used",
            "call_overhead_tokens",
            "context_lines",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.max_targets_per_window < 1:
            raise ValueError("max_targets_per_window must be >= 1")


@dataclass(frozen=True, slots=True)
class ReextractionWindow:
    """One targeted follow-up call: extract from ``target_indices``; the rest are context."""

    target_indices: tuple[int, ...]
    line_indices: tuple[int, ...]  # contiguous in window order, targets and context
    context_indices: tuple[int, ...]
    est_tokens: int
    priority: float
    purpose: str = COVERAGE_PURPOSE


@dataclass(frozen=True, slots=True)
class SkippedLine:
    seg_index: int
    reason: SkipReason
    priority: float


@dataclass(frozen=True, slots=True)
class ReextractionPlan:
    windows: tuple[ReextractionWindow, ...]
    skipped: tuple[SkippedLine, ...]
    calls_planned: int
    tokens_planned: int
    calls_remaining: int
    tokens_remaining: int
    reextracted_after: frozenset[int] = field(
        default_factory=frozenset
    )  # budget.already_reextracted plus targets
    purpose: str = COVERAGE_PURPOSE

    @property
    def target_indices(self) -> tuple[int, ...]:
        return tuple(i for w in self.windows for i in w.target_indices)


# --------------------------------------------------------------------------------------------------
# Lexicons (English, German, Dutch, Spanish)
# --------------------------------------------------------------------------------------------------

_FILLER = frozenset(
    [
        "ok",
        "okay",
        "okey",
        "k",
        "kk",
        "haha",
        "hahaha",
        "hehe",
        "lol",
        "lmao",
        "hmm",
        "hm",
        "mhm",
        "uh",
        "um",
        "oh",
        "ah",
        "aha",
        "ahh",
        "wow",
        "yes",
        "yeah",
        "yep",
        "yup",
        "no",
        "nope",
        "nah",
        "sure",
        "thanks",
        "thank",
        "you",
        "thx",
        "ty",
        "cheers",
        "hi",
        "hello",
        "hey",
        "bye",
        "goodbye",
        "cya",
        "please",
        "sorry",
        "great",
        "nice",
        "cool",
        "good",
        "fine",
        "well",
        "right",
        "true",
        "alright",
        "yay",
        "oops",
        "welcome",
        "np",
        "done",
        "here",
        "there",
        "back",
        "me",
        "too",
        "so",
        "then",
        "and",
        "but",
        "or",
        "the",
        "a",
        "an",
        "it",
        "is",
        "that",
        "this",
        "danke",
        "dankeschon",
        "dankeschoen",
        "bitte",
        "ja",
        "jo",
        "nee",
        "nein",
        "doch",
        "genau",
        "klar",
        "gut",
        "super",
        "prima",
        "toll",
        "schon",
        "hallo",
        "tschuss",
        "tschues",
        "moin",
        "servus",
        "ciao",
        "dank",
        "bedankt",
        "alsjeblieft",
        "graag",
        "oke",
        "prima",
        "top",
        "goed",
        "fijn",
        "oké",
        "hoi",
        "dag",
        "doei",
        "tot",
        "gracias",
        "hola",
        "vale",
        "bueno",
        "bien",
        "claro",
        "adios",
        "adiós",
        "si",
        "sí",
        "pues",
        "eso",
        "genial",
        "perfecto",
        "vaya",
        "jaja",
        "jeje",
        "en",
        "y",
        "de",
        "la",
        "el",
        "los",
        "las",
        "und",
        "der",
        "die",
        "das",
        "ist",
        "ein",
        "eine",
        "mit",
        "nicht",
        "auch",
        "noch",
        "nur",
        "dann",
        "aber",
        "oder",
        "het",
        "een",
        "is",
        "niet",
        "ook",
        "maar",
        "of",
        "lo",
        "un",
        "una",
        "es",
        "no",
        "que",
    ]
)

# Capitalised words that are never entities on their own (pronouns, fillers, month/day words handled as dates).
_CAP_STOP = frozenset(
    [
        "i",
        "i'm",
        "i've",
        "i'll",
        "i'd",
        "im",
        "ive",
        "ich",
        "er",
        "sie",
        "es",
        "wir",
        "ihr",
        "ik",
        "jij",
        "hij",
        "zij",
        "wij",
        "jullie",
        "yo",
        "tu",
        "el",
        "ella",
        "nosotros",
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "so",
        "then",
        "when",
        "while",
        "he",
        "she",
        "it",
        "we",
        "you",
        "they",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "ok",
        "okay",
        "haha",
        "lol",
        "hmm",
        "hello",
        "hi",
        "hey",
        "thanks",
        "thank",
        "yes",
        "no",
        "oh",
        "well",
        "sure",
        "please",
        "sorry",
        "danke",
        "bitte",
        "ja",
        "nein",
        "doch",
        "hallo",
        "gut",
        "genau",
        "klar",
        "nee",
        "hoi",
        "bedankt",
        "gracias",
        "hola",
        "vale",
        "bueno",
        "si",
        "sí",
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "und",
        "oder",
        "aber",
        "wenn",
        "weil",
        "dass",
        "dann",
        "auch",
        "noch",
        "nur",
        "mit",
        "von",
        "zu",
        "im",
        "am",
    ]
)

_MONTHS: dict[str, int] = {}
for _n, _names in enumerate(
    (
        "january jan januar januari enero",
        "february feb februar februari febrero",
        "march märz maart marzo",
        "april apr abril",
        "may mai mei mayo",
        "june jun juni junio",
        "july jul juli julio",
        "august aug augustus agosto",
        "september sept sep septiembre setiembre",
        "october oct oktober okt octubre",
        "november nov noviembre",
        "december dec dezember des diciembre",
    ),
    start=1,
):
    for _w in _names.split():
        _MONTHS[_w] = _n
_MONTH_RE = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "mon",
    "tue",
    "tues",
    "wed",
    "thu",
    "thur",
    "thurs",
    "fri",
    "sat",
    "sun",
    "montag",
    "dienstag",
    "mittwoch",
    "donnerstag",
    "freitag",
    "samstag",
    "sonnabend",
    "sonntag",
    "maandag",
    "dinsdag",
    "woensdag",
    "donderdag",
    "vrijdag",
    "zaterdag",
    "zondag",
    "lunes",
    "martes",
    "miércoles",
    "miercoles",
    "jueves",
    "viernes",
    "sábado",
    "sabado",
    "domingo",
]
_WEEKDAY_RE = "|".join(
    sorted((re.escape(w) for w in _WEEKDAYS if len(w) > 3), key=len, reverse=True)
)
_REL_WORDS = [
    "yesterday",
    "tomorrow",
    "tonight",
    "gestern",
    "vorgestern",
    "übermorgen",
    "uebermorgen",
    "gisteren",
    "eergisteren",
    "overmorgen",
    "ayer",
    "anteayer",
    "mañana",
    "manana",
    "pasado",
]
# 'morgen' (de: tomorrow/morning, nl: tomorrow/morning) is a time word in both.
_REL_RE = "|".join(re.escape(w) for w in [*_REL_WORDS, "morgen"])
_UNIT_RE = (
    r"(?:day|days|week|weeks|month|months|year|years|weekend|tag|tage|tagen|woche|wochen|monat|monate|monaten|jahr|jahre|jahren|"
    r"dag|dagen|weekend|week|weken|maand|maanden|jaar|jaren|día|días|dias|semana|semanas|mes|meses|año|años|ano|anos|fin de semana)"
)
_SOON_RE = (
    r"(?:last|next|this|coming|past|previous|letzte[nrms]?|nächste[nrms]?|naechste[nrms]?|kommende[nrms]?|vergangene[nrms]?|"
    r"diese[nrms]?|vorige|volgende|afgelopen|aankomend|deze|dit|pasad[oa]s?|próxim[oa]s?|proxim[oa]s?|este|esta|"
    r"el|la|últim[oa]s?|ultim[oa]s?)"
)

_DATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2})?(?!\d)",  # ISO
        r"(?<!\d)\d{1,2}[./]\d{1,2}[./]\d{2,4}(?!\d)",  # 5/6/2024, 05.06.2024
        rf"(?<!\w)\d{{1,2}}(?:st|nd|rd|th|\.|er)?\s+(?:of\s+|de\s+)?(?:{_MONTH_RE})\b\.?(?:,?\s+(?:de\s+)?\d{{4}})?",  # 5 May 2024
        rf"(?<!\w)(?:{_MONTH_RE})\b\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?",  # May 5, 2024
        rf"(?<!\w)(?:{_MONTH_RE})\b\.?\s+(?:de\s+)?\d{{4}}",  # May 2024
        r"(?<!\d)\d{1,2}:\d{2}(?:\s?[ap]m)?(?!\d)",  # clock time
        r"(?<!\d)(?:1[5-9]|20)\d{2}(?!\d)(?![\d.,]\d)",  # bare year
        rf"(?<!\w)(?:{_SOON_RE})\s+(?:{_UNIT_RE}|{_WEEKDAY_RE})(?!\w)",  # next week, letzte Woche
        rf"(?<!\w)(?:{_WEEKDAY_RE})(?!\w)",
        rf"(?<!\w)(?:{_REL_RE})(?!\w)",
        rf"(?<!\w)\d+\s+{_UNIT_RE}\s+(?:ago|geleden)(?!\w)",
        rf"(?<!\w)(?:vor|hace)\s+\d+\s+{_UNIT_RE}(?!\w)",
    )
)
# Month names alone (capitalised only for the ambiguous 'may'/'mar'), e.g. "in July".
_MONTH_ONLY = re.compile(rf"(?<!\w)(?:{_MONTH_RE})(?!\w)", re.IGNORECASE | re.UNICODE)
_AMBIGUOUS_MONTHS = frozenset(
    {
        "may",
        "sep",
        "sept",
        "des",
        "mar",
        "dec",
        "oct",
        "nov",
        "jan",
        "feb",
        "jun",
        "jul",
        "aug",
        "apr",
        "sat",
        "sun",
        "mon",
        "wed",
        "mai",
    }
)

_NUMBER = re.compile(
    r"(?<![\w.,])\d+(?:[.,   ]\d{3})*(?:[.,]\d+)?(?![\w])|(?<![\w])\d+(?:[.,]\d+)?"
)

# First-person statements of state, preference or plan.
_FP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        # English
        r"(?<!\w)i(?:'m|’m|'ve|’ve|'ll|’ll|'d|’d| am| was| have| had| live[sd]?| work(?:ed|s)?| moved| like[sd]?| love[sd]?"
        r"| hate[sd]?| prefer(?:red)?| want(?:ed)?| need(?:ed)?| plan| planning| will| got| bought| started| quit| went| adopted| study| studied"
        r"| married| divorced| got engaged| grew| was born| speak| drive| own| use| take)(?!\w)",
        r"(?<!\w)my\s+(?:name|wife|husband|partner|girlfriend|boyfriend|son|daughter|brother|sister|mother|mom|mum|dad|father|dog|cat"
        r"|job|birthday|favou?rite|address|phone|email|doctor|boss)(?!\w)",
        # German
        r"(?<!\w)ich\s+(?:\w+\s+){0,2}?(?:wohne|arbeite|bin|habe|hatte|war|mag|liebe|hasse|plane|werde|ziehe|zog|kaufte|kaufe|heirate|"
        r"studiere|studierte|spreche|fahre|besitze|brauche|will|möchte|moechte|bevorzuge|esse|trinke|lebe|heiße|heisse|komme)(?!\w)",
        r"(?<!\w)mein(?:e[nrms]?)?\s+(?:name|frau|mann|freund(?:in)?|sohn|tochter|bruder|schwester|mutter|vater|hund|katze|job|geburtstag|adresse|arzt|chef)(?!\w)",
        # Dutch
        r"(?<!\w)ik\s+(?:\w+\s+){0,2}?(?:woon|werk|ben|heb|had|was|hou|houd|haat|plan|ga|wil|verhuis|verhuisde|kocht|koop|studeer|spreek|rijd|bezit|"
        r"heet|kom|eet|drink|leef|voorkeur)(?!\w)",
        r"(?<!\w)mijn\s+(?:naam|vrouw|man|vriend(?:in)?|zoon|dochter|broer|zus|moeder|vader|hond|kat|baan|verjaardag|adres|huisarts|baas)(?!\w)",
        # Spanish
        r"(?<!\w)(?:yo\s+)?(?:vivo|trabajo|soy|estoy|tengo|tuve|estudio|prefiero|quiero|planeo|pienso|compré|compre|me mudo|me mudé|me mude|"
        r"me llamo|me gusta|me encanta|odio|necesito|hablo|manejo|conduzco|voy a|vengo de|nací|naci|me casé|me case)(?!\w)",
        r"(?<!\w)mi\s+(?:nombre|esposa|esposo|marido|mujer|novi[oa]|hij[oa]|herman[oa]|madre|padre|perro|gato|trabajo|cumpleaños|dirección|médico|jefe)(?!\w)",
        # Caseless scripts: Chinese / Japanese first-person state
        r"我(?:住|在|是|有|喜欢|喜歡|爱|愛|讨厌|討厭|想|打算|计划|計劃|要|买|買|搬|工作|结婚|結婚)",
        r"(?:私|僕|俺)(?:は|も|の)",
        r"(?:住んでいます|住んでる|が好き|が嫌い|予定です|働いています|に引っ越)",
    )
)
_BULLET = re.compile(r"^\s*(?:[-*•–]|\d{1,2}[.)])\s+\S")
_KATAKANA = re.compile(r"[ァ-ヺーヽヾ]{3,}")
_CJK_QUOTED = re.compile(r"[「『《“‘]([^」』》”’\n]{1,30})[」』》”’]")
_WORD = re.compile(r"\w+(?:[-'’]\w+)*", re.UNICODE)
_SENT_BREAK = re.compile(r"[.!?¿¡。\n•*]|:\s*$|^\s*(?:\d+[.)]|-)\s*$")
_GERMAN_MARKERS = frozenset(
    [
        "ich",
        "und",
        "ist",
        "nicht",
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "mit",
        "auf",
        "für",
        "fuer",
        "wir",
        "sie",
        "er",
        "habe",
        "bin",
        "wohne",
        "arbeite",
        "heute",
        "gestern",
        "auch",
        "aber",
        "noch",
        "sehr",
        "den",
        "dem",
        "von",
        "zu",
        "im",
        "am",
    ]
)
_DE_ENTITY_CUES = frozenset(
    [
        "in",
        "nach",
        "aus",
        "bei",
        "von",
        "namens",
        "heiße",
        "heisse",
        "heißt",
        "heisst",
        "bin",
        "bei",
    ]
)
_PARTICLES = frozenset(
    {
        "van",
        "von",
        "de",
        "der",
        "den",
        "del",
        "la",
        "le",
        "da",
        "di",
        "dos",
        "zu",
        "ten",
        "ter",
        "bin",
        "al",
        "el",
    }
)


# --------------------------------------------------------------------------------------------------
# Signal detection
# --------------------------------------------------------------------------------------------------


def _content_tokens(text: str) -> list[str]:
    return [t for t in (w.casefold() for w in _WORD.findall(text)) if t not in _FILLER]


def _is_filler(text: str) -> bool:
    toks = [w.casefold() for w in _WORD.findall(text)]
    return not toks or all(t in _FILLER or (t.isdigit() is False and len(t) == 1) for t in toks)


def _date_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    for m in _MONTH_ONLY.finditer(text):
        word = m.group(0)
        if word.casefold() in _AMBIGUOUS_MONTHS and not word[0].isupper():
            continue
        if (
            word.casefold() in {"mar", "dec", "des", "sat", "sun", "mon", "wed"}
            and not word[0].isupper()
        ):
            continue
        # a bare month word needs a capital letter unless it is unambiguous
        spans.append((m.start(), m.end()))
    return _merge(spans)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _inside(pos: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(pos < e and end > s for s, e in spans)


def _german_like(text: str) -> bool:
    toks = {w.casefold() for w in _WORD.findall(text)}
    return len(toks & _GERMAN_MARKERS) >= 2


def _entity_spans(text: str, date_spans: list[tuple[int, int]]) -> list[str]:
    """Capitalised runs that are not sentence-initial singletons, plus caseless-script cues."""
    out: list[str] = []
    german = _german_like(text)
    toks = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
    runs: list[list[int]] = []  # indices into toks
    for i, (w, s, e) in enumerate(toks):
        lowered = w.casefold()
        cap = (
            w[0].isupper()
            and not w.isdigit()
            and lowered not in _CAP_STOP
            and lowered not in _FILLER
        )
        if cap and _inside(s, e, date_spans):
            cap = False
        if cap and len(w) == 1:
            cap = False
        if cap:
            gap = text[toks[i - 1][2] : s] if runs and runs[-1] and runs[-1][-1] == i - 1 else None
            if gap is not None and gap.strip() == "":
                runs[-1].append(i)
            else:
                runs.append([i])
        elif (
            lowered in _PARTICLES
            and runs
            and runs[-1][-1] == i - 1
            and i + 1 < len(toks)
            and toks[i + 1][0][0].isupper()
            and text[e : toks[i + 1][1]].strip() == ""
        ):
            runs[-1].append(i)  # "Ludwig van Beethoven"
    for run in runs:
        while run and toks[run[-1]][0].casefold() in _PARTICLES:
            run.pop()
        if not run:
            continue
        first_s = toks[run[0]][1]
        prefix = text[:first_s]
        stripped = prefix.rstrip(" \t\"'“„([«¿¡")
        sentence_start = (
            (not stripped)
            or bool(_SENT_BREAK.search(stripped[-1:]))
            or bool(_BULLET.match(prefix + "x"))
        )
        if len(run) == 1 and run[0] > 0 and not sentence_start:
            prev = toks[run[0] - 1][0].casefold()
            # German capitalises every noun: a lone capitalised word needs a name cue before it.
            if german and prev not in _DE_ENTITY_CUES:
                continue
        elif len(run) == 1 and sentence_start:
            continue
        elif len(run) == 1:
            pass
        out.append(text[toks[run[0]][1] : toks[run[-1]][2]])
    for m in _KATAKANA.finditer(text):
        out.append(m.group(0))
    for m in _CJK_QUOTED.finditer(text):
        out.append(m.group(1))
    # de-duplicate, keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        k = s.casefold()
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq


def _first_person(text: str) -> bool:
    for pat in _FP_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        rest = _content_tokens(text[m.end() :])
        # CJK patterns carry their predicate inside the match
        if any("぀" <= c <= "鿿" for c in m.group(0)):
            return True
        if len(rest) >= 2:
            return True
        # "my wife ..." style nouns already carry content
        if (
            pat.pattern.startswith(("(?<!\\w)my", "(?<!\\w)mein", "(?<!\\w)mijn", "(?<!\\w)mi\\s"))
            and len(rest) >= 1
        ):
            return True
    return False


def _list_shape(text: str) -> bool:
    if _BULLET.match(text):
        return True
    parts = [p for p in re.split(r"[,;、]|\s(?:and|und|en|y|e|and/or)\s", text) if p.strip()]
    items = [p for p in parts if _content_tokens(p)]
    return (
        len(items) >= 3
        and len(parts) >= 3
        and text.count(",") + text.count(";") + text.count("、") >= 2
    )


def _number_spans(text: str, date_spans: list[tuple[int, int]]) -> list[str]:
    out: list[str] = []
    for m in _NUMBER.finditer(text):
        if _inside(m.start(), m.end(), date_spans):
            continue
        out.append(m.group(0).strip())
    return out


def _detect(text: str) -> tuple[tuple[str, ...], float, tuple[SalientSpan, ...]]:
    if _is_filler(text):
        return (), 0.0, ()
    dspans = _date_spans(text)
    dates = [text[s:e] for s, e in dspans]
    ents = _entity_spans(text, dspans)
    nums = _number_spans(text, dspans)
    fp = _first_person(text)
    lst = _list_shape(text)

    signals: list[str] = []
    spans: list[SalientSpan] = []
    score = 0.0
    if ents:
        signals.append("entity")
        score += min(1.5 + 0.5 * (len(ents) - 1), 3.0)
        spans += [SalientSpan("entity", e) for e in ents]
    if nums:
        signals.append("number")
        score += max(_number_weight(n) for n in nums)
        spans += [SalientSpan("number", n) for n in nums]
    if dates:
        signals.append("date")
        score += 2.0
        spans += [SalientSpan("date", d) for d in dates]
    if fp:
        signals.append("first_person")
        score += 2.0
    if lst:
        signals.append("list")
        score += 1.5
    return tuple(signals), score, tuple(spans)


def _number_weight(n: str) -> float:
    return 1.5 if sum(c.isdigit() for c in n) >= 2 else 1.0


_SPAN_WEIGHT = {"entity": 1.5, "number": 1.0, "date": 2.0}


# --------------------------------------------------------------------------------------------------
# Envelope content for omission diagnostics
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Content:
    names: set[str] = field(default_factory=set)  # normalised tokens
    ints: set[int] = field(default_factory=set)
    raw_numbers: set[str] = field(default_factory=set)
    has_event_time: bool = False


def _digits_of(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def _add_text(c: _Content, text: str) -> None:
    c.names.update(normalise_name(text).split())
    for d in _digits_of(text):
        c.ints.add(int(d))
    for n in _NUMBER.findall(text):
        c.raw_numbers.add(_canon_number(n))


def _canon_number(n: str) -> str:
    n = re.sub(r"[   ]", "", n.strip())
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", n):
        return re.sub(r"[.,]", "", n)
    return n.replace(",", ".")


def _envelope_content(envs: Iterable[AssertionEnvelope]) -> _Content:
    c = _Content()
    for e in envs:
        _add_text(c, e.text)
        for m in (e.subject, e.object):
            if m is not None:
                _add_text(c, m.surface)
        if e.value is not None:
            _add_text(c, str(e.value))
        if e.attribute:
            _add_text(c, e.attribute)
        if e.speaker:
            _add_text(c, e.speaker)
        for ev in e.evidence:
            if ev.span is not None and ev.span.quote:
                _add_text(c, ev.span.quote)
        for iv in (e.time.event,):
            if iv is None:
                continue
            for ref in (iv.start, iv.end):
                if ref is None:
                    continue
                c.has_event_time = True
                if ref.text:
                    _add_text(c, ref.text)
                if ref.at is not None:
                    for d in (ref.at.year, ref.at.month, ref.at.day):
                        c.ints.add(d)
    return c


def _entity_reflected(entity: str, c: _Content) -> bool:
    toks = normalise_name(entity).split()
    return bool(toks) and all(t in c.names for t in toks)


def _number_reflected(num: str, c: _Content) -> bool:
    canon = _canon_number(num)
    if canon in c.raw_numbers:
        return True
    return (
        all(int(d) in c.ints for d in _digits_of(canon))
        and bool(_digits_of(canon))
        and "." not in canon
    )


def _date_reflected(date: str, c: _Content) -> bool:
    low = date.casefold()
    digits = [int(d) for d in _digits_of(date)]
    months = [n for w, n in _MONTHS.items() if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low)]
    if digits or months:
        if digits and not all(d in c.ints for d in digits):
            return False
        return (
            not months
            or any(m in c.ints for m in months)
            or any(w in c.names for w in normalise_name(date).split())
        )
    # relative words, weekdays: the word itself, or a resolved event time
    return c.has_event_time or all(t in c.names for t in normalise_name(date).split())


def _omissions(seg_index: int, spans: Sequence[SalientSpan], c: _Content) -> list[Omission]:
    out: list[Omission] = []
    for s in spans:
        ok = (
            _entity_reflected(s.text, c)
            if s.kind == "entity"
            else _number_reflected(s.text, c)
            if s.kind == "number"
            else _date_reflected(s.text, c)
        )
        if not ok:
            out.append(Omission(seg_index, s.kind, s.text))
    return out


# --------------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------------


def _cites(env: AssertionEnvelope, line: WindowLine) -> bool:
    for ev in env.evidence:
        if ev.role not in ("support", "binding") or ev.turn is None:
            continue
        t = ev.turn
        if t.seg_index != line.seg_index:
            continue
        if line.source_id is not None and t.source_id != line.source_id:
            continue
        if line.source_version is not None and t.source_version != line.source_version:
            continue
        return True
    return False


def coverage(
    window_lines: Sequence[WindowLine], envelopes: Sequence[AssertionEnvelope]
) -> CoverageReport:
    """Check which NEW lines of an extraction window the accepted ``envelopes`` cover.

    ``envelopes`` are the accepted envelopes of the window's extraction (callers pass only accepted
    ones; this function does not filter on grounding). Line indices must be unique.
    """
    window = tuple(window_lines)
    if len({ln.seg_index for ln in window}) != len(window):
        raise ValueError("window lines must have unique seg_index values")
    lines: list[LineCoverage] = []
    for ln in window:
        if not ln.is_new:
            continue
        signals, score, spans = _detect(ln.text)
        salient = score >= SALIENCE_THRESHOLD
        citing = [e for e in envelopes if _cites(e, ln)]
        if not citing:
            status: LineStatus = "uncovered_salient" if salient else "not_salient"
            oms = [Omission(ln.seg_index, s.kind, s.text) for s in spans] if salient else []
        else:
            status = "covered"
            oms = _omissions(ln.seg_index, spans, _envelope_content(citing)) if salient else []
        oscore = _omission_score(oms)
        lines.append(
            LineCoverage(
                seg_index=ln.seg_index,
                status=status,
                signals=signals if (salient or status == "covered") else (),
                score=score,
                spans=spans,
                omissions=tuple(oms),
                cited_by=tuple(e.write_id for e in citing),
                omission_score=oscore,
            )
        )
    # a covered line is only "owed" if salient; ratio counts salient lines
    sal = [ln for ln in lines if ln.score >= SALIENCE_THRESHOLD]
    cov = [ln for ln in sal if ln.status == "covered"]
    ratio = len(cov) / len(sal) if sal else 1.0
    return CoverageReport(
        window=window,
        lines=tuple(lines),
        salient_lines=len(sal),
        covered_salient_lines=len(cov),
        ratio=ratio,
        omission_count=sum(len(ln.omissions) for ln in lines),
    )


def _omission_score(oms: Sequence[Omission]) -> float:
    ents = [o for o in oms if o.kind == "entity"]
    score = 0.0
    if ents:
        score += min(1.5 + 0.5 * (len(ents) - 1), 3.0)
    nums = [o for o in oms if o.kind == "number"]
    if nums:
        score += max(_number_weight(o.text) for o in nums)
    if any(o.kind == "date" for o in oms):
        score += 2.0
    return score


# --------------------------------------------------------------------------------------------------
# Targeted re-extraction plan
# --------------------------------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4 if text else 0


def plan_reextraction(
    report: CoverageReport,
    budget: CoverageBudget | None = None,
    *,
    include_omissions: bool = True,
    count_tokens: Callable[[str], int] | None = None,
) -> ReextractionPlan:
    """Plan targeted follow-up extraction calls (purpose ``coverage``) within ``budget``.

    Candidates are ``uncovered_salient`` lines (priority = salience score) and, with
    ``include_omissions``, covered lines whose omitted spans are themselves salient (priority = half
    the omission score, so wholly missed lines go first). Candidates already in
    ``budget.already_reextracted`` are skipped (``already_reextracted``): a line is re-extracted for
    coverage at most once. Nearby candidates share a window (at most ``max_targets_per_window``
    targets, ``context_lines`` neighbours on each side). Windows are served by total priority; a window
    that does not fit the remaining tokens drops its lowest-priority targets first. Deterministic and
    idempotent: planning twice from the same inputs gives the same plan.
    """
    b = budget or CoverageBudget()
    count = count_tokens or _estimate_tokens
    by_index = {ln.seg_index: ln for ln in report.window}
    order = [ln.seg_index for ln in report.window]
    pos = {i: k for k, i in enumerate(order)}

    prio: dict[int, float] = {}
    for ln in report.lines:
        if ln.status == "uncovered_salient":
            prio[ln.seg_index] = ln.score
        elif (
            include_omissions and ln.status == "covered" and ln.omission_score >= SALIENCE_THRESHOLD
        ):
            prio[ln.seg_index] = 0.5 * ln.omission_score

    skipped: list[SkippedLine] = []
    cands: list[int] = []
    for idx in sorted(prio, key=lambda i: pos[i]):
        if idx in b.already_reextracted:
            skipped.append(SkippedLine(idx, "already_reextracted", prio[idx]))
        else:
            cands.append(idx)

    # Cluster candidates whose context ranges would touch, capped at max_targets_per_window.
    clusters: list[list[int]] = []
    for idx in cands:  # window order
        if clusters:
            last = clusters[-1]
            if (
                pos[idx] - pos[last[-1]] <= 2 * b.context_lines + 1
                and len(last) < b.max_targets_per_window
            ):
                last.append(idx)
                continue
        clusters.append([idx])

    clusters.sort(key=lambda cl: (-sum(prio[i] for i in cl), pos[cl[0]]))

    calls_left = max(0, b.max_calls - b.calls_used)
    tokens_left = max(0, b.max_tokens - b.tokens_used)
    windows: list[ReextractionWindow] = []
    tokens_planned = 0
    for cl in clusters:
        if calls_left <= 0:
            skipped += [SkippedLine(i, "budget_calls", prio[i]) for i in cl]
            continue
        targets = list(cl)
        dropped: list[int] = []
        while targets:
            lo = max(0, pos[targets[0]] - b.context_lines)
            hi = min(len(order) - 1, pos[targets[-1]] + b.context_lines)
            idxs = order[lo : hi + 1]
            cost = b.call_overhead_tokens + sum(count(by_index[i].text) for i in idxs)
            if cost <= tokens_left:
                break
            # drop the lowest-priority target (latest index on ties)
            worst = min(targets, key=lambda i: (prio[i], -pos[i]))
            targets.remove(worst)
            dropped.append(worst)
        skipped += [SkippedLine(i, "budget_tokens", prio[i]) for i in dropped]
        if not targets:
            continue
        tset = set(targets)
        windows.append(
            ReextractionWindow(
                target_indices=tuple(targets),
                line_indices=tuple(idxs),
                context_indices=tuple(i for i in idxs if i not in tset),
                est_tokens=cost,
                priority=sum(prio[i] for i in targets),
            )
        )
        calls_left -= 1
        tokens_left -= cost
        tokens_planned += cost

    windows.sort(key=lambda w: (-w.priority, pos[w.target_indices[0]]))
    skipped.sort(key=lambda s: (-s.priority, pos[s.seg_index]))
    targets_all = frozenset(i for w in windows for i in w.target_indices)
    return ReextractionPlan(
        windows=tuple(windows),
        skipped=tuple(skipped),
        calls_planned=len(windows),
        tokens_planned=tokens_planned,
        calls_remaining=calls_left,
        tokens_remaining=tokens_left,
        reextracted_after=b.already_reextracted | targets_all,
    )
