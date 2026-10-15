# ruff: noqa: RUF001
"""Typed query contract by rules, for English, German, Dutch and Spanish (LIB-16).

Pure and unwired (wired later by R3-02; decisions R-03, R-20 and ER-19). Imports only the standard
library, pydantic, ``memspine.schemas`` and ``memspine.derive.temporal`` (LIB-01). No I/O, no clock read
("now" is an argument), no model call, deterministic.

:func:`build_contract` reads one question and fills a :class:`TypedContract`:

* ``answer_type``: entity / date / duration / count / list / yesno / free.
* ``shape``: single / join / comparison / latest / list_count / as_of / history.
* ``time_view``: current / as_of (an instant) / history / range (a half-open ``[start, end)``), with times
  anchored through LIB-01 when the phrase is relative and ``now`` is given. Absolute dates and years need
  no ``now``. A relative phrase without ``now`` stays an unanchored phrase (``anchored=False``); a date
  with no year ("3 March") is never completed from ``now``.
* ``personal``: ``True`` (first or second person, or a named person), ``False`` (general-knowledge
  phrasing and nothing personal), ``None`` (cannot tell).
* ``request_kind``: lookup / enumeration / as_of / show_all (the relevance gate, LIB-12, keys on it).
* ``targets`` (surface mentions, as written), ``attributes`` (canonical attribute hints), ``language``.

Never guess. Every field has a confidence in ``confidence``; a field the rules cannot decide is
``unknown`` (``answer_type="unknown"``, ``shape="unknown"``, ``personal=None``, ``language="unknown"``)
and is named in ``unknown``. :func:`render_header` prints only fields at or above a confidence floor, with
language-neutral keys, so an undecided contract adds nothing to the reader's context.

Interpretations (flagged in the LIB-16 report): ``as_of`` is the *start* of the named period when the
date is coarser than a day; a plain date or period ("in 2019", "yesterday") is a ``range`` over that
period; ``before X`` excludes X, ``until X`` includes it, ``after X`` excludes it, ``since X`` includes it.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.schemas.steps.shared import QueryContract as SchemaQueryContract

__all__ = [
    "MIN_HEADER_CONFIDENCE",
    "AnswerType",
    "RequestKind",
    "Shape",
    "TimeView",
    "TypedContract",
    "build_contract",
    "render_header",
]

AnswerType = Literal["entity", "date", "duration", "count", "list", "yesno", "free", "unknown"]
Shape = Literal[
    "single", "join", "comparison", "latest", "list_count", "as_of", "history", "unknown"
]
RequestKind = Literal["lookup", "enumeration", "as_of", "show_all"]
ViewKind = Literal["current", "as_of", "history", "range", "unspecified"]

MIN_HEADER_CONFIDENCE = 0.5


class TimeView(BaseModel):
    """Which slice of time the question reads. ``range`` is half open ``[start, end)``; ``None`` = open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ViewKind = "unspecified"
    as_of: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    phrase: str = ""  # the time phrase as matched (case- and accent-folded)
    anchored: bool = False  # True when the instants above were computed
    precision: str = "unknown"  # of the first matched period, when there is one


class TypedContract(BaseModel):
    """The typed query contract (rules). Compatible with the schema ``QueryContract`` via :meth:`to_schema`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_type: AnswerType = "unknown"
    shape: Shape = "unknown"
    time_view: TimeView = Field(default_factory=TimeView)
    personal: bool | None = None
    request_kind: RequestKind = "lookup"
    cardinality: Literal["one", "many", "count"] = "one"
    targets: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    language: str = "unknown"  # en / de / nl / es, a caller hint that is not supported, or unknown
    confidence: dict[str, float] = Field(default_factory=dict)
    unknown: tuple[str, ...] = ()
    source: Literal["rules"] = "rules"

    @property
    def known(self) -> bool:
        return self.answer_type != "unknown" or self.shape != "unknown"

    def to_schema(self) -> SchemaQueryContract:
        """The nearest schema ``QueryContract`` (lossy: list becomes entity with cardinality many)."""
        mapped = {
            "entity": "entity",
            "date": "date",
            "duration": "duration",
            "count": "count",
            "list": "entity",
            "yesno": "yesno",
            "free": "description",
            "unknown": "unknown",
        }[self.answer_type]
        return SchemaQueryContract(
            subjects=list(self.targets),
            relation=" ".join(self.attributes),
            answer_type=mapped,  # type: ignore[arg-type]
            time_scope=self.time_view.phrase,
            cardinality=self.cardinality,
            request="recall",
            source="rules",
        )


# ------------------------------------------------------------------------------------------- folding


def _fold(text: str) -> str:
    """Lower-case, German digraphs, accents removed, apostrophes straightened."""
    s = text.lower().replace("’", "'").replace("‘", "'")
    s = s.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


# ------------------------------------------------------------------------------------------ language

_LANGS = ("en", "de", "nl", "es")

_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "did",
            "does",
            "what",
            "when",
            "where",
            "who",
            "whom",
            "which",
            "how",
            "is",
            "are",
            "was",
            "were",
            "i",
            "my",
            "me",
            "you",
            "your",
            "of",
            "have",
            "has",
            "had",
            "and",
            "with",
            "about",
            "there",
            "for",
            "that",
            "this",
            "would",
            "could",
            "do",
            "our",
            "we",
            "from",
            "much",
            "many",
            "show",
            "tell",
            "list",
            "name",
            "explain",
            "doing",
            "eat",
            "ate",
            "lived",
            "work",
            "works",
            "went",
            "visit",
            "visited",
            "moved",
            "started",
            "left",
            "owned",
            "pets",
            "still",
            "last",
            "first",
            "since",
            "before",
            "after",
            "compare",
            "time",
            "or",
            "but",
            "if",
            "at",
            "by",
            "as",
            "an",
            "it",
            "he",
            "she",
            "they",
            "his",
            "her",
            "their",
            "them",
            "than",
            "not",
            "will",
            "on",
            "any",
            "some",
            "all",
            "been",
            "being",
            "be",
        ]
    ),
    "de": frozenset(
        [
            "der",
            "die",
            "das",
            "den",
            "dem",
            "ich",
            "mein",
            "meine",
            "meinen",
            "meinem",
            "mir",
            "mich",
            "wann",
            "wer",
            "wie",
            "was",
            "welche",
            "welcher",
            "welches",
            "ist",
            "sind",
            "hat",
            "habe",
            "hatte",
            "nicht",
            "und",
            "mit",
            "von",
            "im",
            "fuer",
            "ueber",
            "ein",
            "eine",
            "einen",
            "wo",
            "wurde",
            "auch",
            "bei",
            "aus",
            "nach",
            "vor",
            "zum",
            "zur",
            "alle",
            "viele",
            "lange",
            "oft",
            "warum",
            "gestern",
            "heute",
            "du",
            "dein",
            "wir",
            "unser",
            "zeig",
            "zeige",
            "erzaehl",
            "erzaehle",
            "liste",
            "nenne",
            "erklaere",
            "welchen",
            "welchem",
            "alles",
            "wohnt",
            "wohne",
            "arbeitet",
            "kostet",
            "gibt",
            "noch",
            "schon",
            "nur",
            "aber",
            "oder",
            "wenn",
            "dass",
            "weil",
            "seit",
            "gegen",
            "haben",
            "hast",
            "hatten",
            "gewesen",
            "gegessen",
            "gelesen",
            "unter",
        ]
    ),
    "nl": frozenset(
        [
            "de",
            "het",
            "een",
            "ik",
            "mijn",
            "mij",
            "wanneer",
            "wie",
            "wat",
            "waar",
            "welke",
            "hoe",
            "heeft",
            "hebben",
            "zijn",
            "van",
            "op",
            "met",
            "niet",
            "en",
            "voor",
            "naar",
            "bij",
            "ook",
            "jaar",
            "geleden",
            "gisteren",
            "vandaag",
            "hoeveel",
            "waarom",
            "jij",
            "jouw",
            "je",
            "onze",
            "ons",
            "toen",
            "die",
            "alle",
            "alles",
            "noem",
            "vertel",
            "toon",
            "leg",
            "uit",
            "woont",
            "woon",
            "werkt",
            "kost",
            "kosten",
            "heb",
            "hebt",
            "bent",
            "wordt",
            "wel",
            "nog",
            "al",
            "maar",
            "of",
            "dat",
            "omdat",
            "sinds",
            "zeker",
            "verdient",
            "verhuisd",
            "gelezen",
            "gegeten",
            "geweest",
            "veranderd",
            "laatst",
            "keer",
            "boeken",
            "is",
            "me",
            "ouder",
            "dan",
            "jonger",
            "meer",
            "minder",
            "voor",
            "steeds",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "los",
            "las",
            "un",
            "una",
            "que",
            "cuando",
            "donde",
            "quien",
            "cual",
            "cuales",
            "como",
            "mi",
            "mis",
            "tu",
            "tus",
            "es",
            "son",
            "fue",
            "por",
            "para",
            "con",
            "del",
            "al",
            "en",
            "hace",
            "cuanto",
            "cuantos",
            "cuantas",
            "yo",
            "nuestro",
            "ayer",
            "hoy",
            "tiene",
            "he",
            "ha",
            "y",
            "se",
            "de",
            "dime",
            "muestrame",
            "enumera",
            "explica",
            "lista",
            "todo",
            "todos",
            "todas",
            "vive",
            "vivo",
            "trabaja",
            "tienen",
            "pero",
            "porque",
            "sobre",
            "desde",
            "entre",
            "hasta",
            "mas",
            "muy",
            "tambien",
            "mascotas",
            "hermana",
            "visite",
            "comio",
            "dejo",
            "empezo",
            "cambie",
            "mudanza",
            "trabajo",
            "ultima",
            "vez",
            "veces",
        ]
    ),
}


def _weights() -> dict[str, float]:
    """Each marker word is worth 1/k to each of the k languages that list it (shared words count less)."""
    count: dict[str, int] = {}
    for words in _MARKERS.values():
        for w in words:
            count[w] = count.get(w, 0) + 1
    return {w: 1.0 / n for w, n in count.items()}


_WEIGHT = _weights()


def _detect_language(raw: str, folded: str, hint: str | None) -> tuple[str, float, bool]:
    """(language, confidence, supported). A hint wins unless the text clearly disagrees (score >= 3)."""
    toks = re.findall(r"[a-z]+", folded)
    scores = {lang: sum(_WEIGHT[t] for t in toks if t in _MARKERS[lang]) for lang in _LANGS}
    if "¿" in raw or "ñ" in raw or "¡" in raw:
        scores["es"] += 2
    if any(c in raw.lower() for c in "äöüß"):
        scores["de"] += 1
    ranked = sorted(_LANGS, key=lambda lang: (-scores[lang], lang))
    best, second = ranked[0], ranked[1]
    top, runner = scores[best], scores[second]
    detected: str | None = None
    dconf = 0.0
    if top >= 0.99 and top - runner >= 0.49:
        detected = best
        dconf = min(0.95, 0.45 + 0.15 * top) * (top - runner) / top
    norm = None
    if hint:
        norm = re.split(r"[-_]", hint.strip().lower())[0] or None
    if norm in _LANGS:
        if detected and detected != norm and top >= 3:
            return detected, round(dconf, 2), True
        return norm, 1.0, True
    if detected and (norm is None or top >= 3):
        return detected, round(dconf, 2), True
    if norm:
        return norm, 1.0, False
    return "unknown", 0.0, False


# ------------------------------------------------------------------------------- per-language cues


@dataclass(frozen=True)
class _Lex:
    heads: tuple[tuple[str, re.Pattern[str]], ...]  # ordered; earliest match wins, ties by order
    list_strong: re.Pattern[str]
    list_weak: re.Pattern[str]
    show_all: re.Pattern[str]
    choice: re.Pattern[str]
    not_choice: re.Pattern[str]
    history: re.Pattern[str]
    current: re.Pattern[str]
    latest: re.Pattern[str]
    comparison: re.Pattern[str]
    join: re.Pattern[str]
    between_join: re.Pattern[str]
    generic: re.Pattern[str]
    first_person: frozenset[str]
    second_person: frozenset[str]
    politeness: re.Pattern[str]
    bare_where: re.Pattern[str]


def _heads(**kw: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    order = ("duration", "count", "amount", "date", "free", "entity", "yesno")
    return tuple((k, _rx(kw[k])) for k in order if k in kw)


_LEX: dict[str, _Lex] = {
    "en": _Lex(
        heads=_heads(
            duration=r"\bhow long\b|\bhow (?:many|much) (?:seconds?|minutes?|hours?|days?|nights?|weeks?|months?|years?|decades?)\b|\bhow much time\b",
            count=r"\bhow (?:many|often)\b|\bnumber of\b",
            amount=r"\bhow much\b",
            date=r"\bwhen\b|\b(?:what|which) (?:exact |specific )?(?:date|day|year|month|week|season|time)\b|\bsince when\b|\buntil when\b",
            free=r"\bwhy\b|\bhow come\b|\bfor what reason\b|\bhow (?:did|do|does|can|could|would|should|will|has|have|is|was|were)\b",
            entity=r"\bwho(?:m|se)?\b|\bwhere\b|\bwhich\b|\bwhat\b",
            yesno=r"^\W*(?:(?:and|so|also|ok|okay|hey)\s+)?(?:did|does|do|is|are|was|were|has|have|had|will|would|can|could|should|shall|may|might|am)\b",
        ),
        list_strong=_rx(
            r"\blist\b|\benumerate\b|\bname (?:all|every)\b|\bwhat are (?:all )?(?:the |my |his |her |their )\w+s\b|\b(?:which|what) (?:\w+ )?\w*[^su\W]s (?:did|do|does|have|has|had|are|were|am)\b|\bwho all\b"
        ),
        list_weak=_rx(r"\ball (?:the|of|my|his|her|their|our)\b|\bevery\b|\beach\b|\beverything\b"),
        show_all=_rx(
            r"\b(?:tell|show|give) me (?:everything|all)\b|\beverything (?:you|i|we) (?:know|have|remember|stored)\b|\bwhat do you (?:know|remember) about\b|\bshow (?:me )?(?:all|everything)\b|\ball (?:that )?you (?:know|remember)\b"
        ),
        choice=_rx(r"\bor\b"),
        not_choice=_rx(r"\bor not\b|\bor no\b"),
        history=_rx(
            r"\bhistory of\b|\btimeline\b|\bused to\b|\bpreviously\b|\bformerly\b|\bover time\b|\b(?:has|have|had) changed\b|\bchanged over\b|\ball (?:my |the )?previous\b|\bearlier (?:address|job|name)\w*\b"
        ),
        current=_rx(
            r"\bcurrent(?:ly)?\b|\bright now\b|\bat the moment\b|\bnowadays\b|\bthese days\b|\bat present\b|\bpresently\b|\bnow\b"
        ),
        latest=_rx(
            r"\blatest\b|\bmost recent(?:ly)?\b|\bnewest\b|\blast time\b|\bwhen did (?:i|we|you|\w+) last\b|\bi last\b|\bwe last\b"
        ),
        comparison=_rx(
            r"\bcompare[ds]?\b|\bcomparison\b|\bversus\b|\bvs\.?\b|\bdifference between\b|\b(?:\w{3,}er|more|less|fewer) than\b|\bwhich (?:one )?(?:came|happened|was|is) (?:first|earlier|later|before|after)\b"
        ),
        join=_rx(
            r"\bboth\b|\bin common\b|\bshared?\b|\bthe same\b|\btogether\b|\bat the same time\b|\bhow long (?:after|before|between)\b"
        ),
        between_join=_rx(r"\bbetween\b.+\band\b"),
        generic=_rx(
            r"\bcapital of\b|\bpopulation of\b|\bmeaning of\b|\bdefinition of\b|\bwho (?:wrote|invented|discovered|painted|composed|founded|directed|built)\b|\bwhat does \w+ mean\b|\bhow (?:tall|high|wide|deep|far) is\b|\bboiling point\b|\bspeed of\b|^\W*(?:explain|define|translate)\b|\bwhat (?:is|are) (?:a|an)\b"
        ),
        first_person=frozenset(
            [
                "i",
                "my",
                "me",
                "mine",
                "myself",
                "we",
                "our",
                "ours",
                "us",
                "i'm",
                "i've",
                "i'd",
                "i'll",
            ]
        ),
        second_person=frozenset(["you", "your", "yours", "you're", "you've"]),
        politeness=_rx(
            r"\b(?:can|could|would|will) you(?: please)?\b|\bplease\b|\bdo you (?:know|think)\b"
        ),
        bare_where=_rx(r"^\W*where (?:is|are|was|were) [^\s]+(?: [^\s]+)?\W*$"),
    ),
    "de": _Lex(
        heads=_heads(
            duration=r"\bwie lange\b|\bwie viele? (?:tage|wochen|monate|jahre|stunden|minuten|nacht|naechte)\b|\bwie viel zeit\b",
            count=r"\bwie viele[nrms]?\b|\bwie oft\b|\banzahl\b",
            amount=r"\bwie viel\b|\bwieviel\b",
            date=r"\bwann\b|\b(?:welche[nrms]?|an welchem|in welchem) (?:datum|tag|jahr|monat|woche|zeit|uhrzeit)\b|\bseit wann\b|\bbis wann\b",
            free=r"\bwarum\b|\bweshalb\b|\bwieso\b|\bwie (?:hat|haben|ist|war|wurde|kann|kam|komme)\b",
            entity=r"\bwer\b|\bwen\b|\bwem\b|\bwessen\b|\bwo\b|\bwoher\b|\bwohin\b|\bwelche[nrms]?\b|\bwomit\b|\bwas\b",
            yesno=r"^\W*(?:(?:und|also|ok|hey)\s+)?(?:hat|hast|habe|haben|hatte|hatten|ist|sind|bin|bist|war|waren|wurde|wurden|wird|werden|kann|kannst|koennen|konnte|mag|magst|muss|musst|soll|darf|gibt|gab|lebt|wohnt|arbeitet)\b",
        ),
        list_strong=_rx(
            r"\bliste\b|\bnenne alle\b|\bnenn alle\b|\bzaehl\w* .*\bauf\b|\bwas alles\b|\bwelche \w+ (?:habe|hat|hast|haben|sind|waren|hatte)\b"
        ),
        list_weak=_rx(r"\balle\b|\bjede[rsmn]?\b|\balles\b"),
        show_all=_rx(
            r"\b(?:zeig|sag|erzaehl\w*) mir alles\b|\bwas weisst du (?:alles )?(?:ueber|von)\b|\balles (?:was|ueber)\b"
        ),
        choice=_rx(r"\boder\b"),
        not_choice=_rx(r"\boder nicht\b"),
        history=_rx(
            r"\bverlauf\b|\bgeschichte\b|\bhistorie\b|\bfrueher\b|\bvorher\b|\bim laufe der zeit\b|\bueber die zeit\b|\bhat sich .*geaendert\b|\bbisherige[nr]?\b|\behemalig\w*\b"
        ),
        current=_rx(
            r"\baktuell\w*\b|\bderzeit\b|\bmomentan\b|\bzurzeit\b|\bjetzt\b|\bgegenwaertig\b|\bheutzutage\b"
        ),
        latest=_rx(r"\bzuletzt\b|\bletzte[sn]? mal\b|\bneueste\w*\b|\bjuengste\w*\b"),
        comparison=_rx(
            r"\bvergleich\w*\b|\bgegenueber\b|\bvs\.?\b|\bunterschied zwischen\b|\b(?:\w{3,}er|mehr|weniger|eher|lieber) als\b"
        ),
        join=_rx(
            r"\bbeide[nrs]?\b|\bgemeinsam\w*\b|\bzusammen\b|\bwie lange (?:nach|vor|zwischen)\b|\bgleiche[nrms]?\b|\bgleichzeitig\b"
        ),
        between_join=_rx(r"\bzwischen\b.+\bund\b"),
        generic=_rx(
            r"\bhauptstadt von\b|\beinwohner\b|\bbedeutung von\b|\bdefinition\b|\bwer hat \w+ (?:erfunden|geschrieben|entdeckt|gemalt|gegruendet)\b|^\W*(?:erklaere|uebersetze)\b|\bwie hoch ist\b|\bwas bedeutet\b|\bwas ist (?:ein|eine)\b"
        ),
        first_person=frozenset(
            [
                "ich",
                "mein",
                "meine",
                "meiner",
                "meinem",
                "meinen",
                "meines",
                "mir",
                "mich",
                "wir",
                "unser",
                "unsere",
                "unserem",
                "unseren",
                "uns",
            ]
        ),
        second_person=frozenset(
            ["du", "dich", "dir", "dein", "deine", "deinem", "deinen", "deiner"]
        ),
        politeness=_rx(
            r"\b(?:kannst|koenntest|wuerdest|kann|koennen|koennten) (?:du|sie)(?: mir)?\b|\bbitte\b"
        ),
        bare_where=_rx(r"^\W*wo (?:ist|sind|war|waren) [^\s]+(?: [^\s]+)?\W*$"),
    ),
    "nl": _Lex(
        heads=_heads(
            duration=r"\bhoe lang\b|\bhoeveel (?:dagen|weken|maanden|jaar|jaren|uur|uren|minuten|tijd|nachten)\b",
            count=r"\bhoeveel\b(?! (?:kost|kosten|verdient|verdien|betaal\w*|is|was)\b)|\bhoe vaak\b|\baantal\b",
            amount=r"\bhoeveel (?:kost|kosten|verdient|verdien|betaal\w*|is|was)\b",
            date=r"\bwanneer\b|\b(?:welke|op welke|in welk(?:e)?) (?:datum|dag|jaar|maand|week|tijd)\b|\bsinds wanneer\b|\btot wanneer\b",
            free=r"\bwaarom\b|\bhoe (?:heb|heeft|had|is|was|kan|kwam|komt|deed|doe)\b",
            entity=r"\bwie\b|\bwiens\b|\bwat\b|\bwaar\b|\bwaarheen\b|\bwaarvandaan\b|\bwelke\b|\bwelk\b",
            yesno=r"^\W*(?:(?:en|dus|ook|hey)\s+)?(?:heb|heeft|hebben|had|hadden|is|zijn|was|waren|ben|bent|kan|kun|kunt|kunnen|zal|zou|moet|mag|wordt|werd|gaat|woont|werkt|doet|deed)\b",
        ),
        list_strong=_rx(
            r"\blijst\b|\bnoem alle\b|\bsom .*\bop\b|\bwat allemaal\b|\bwelke \w+ (?:heb|heeft|hebben|zijn|waren|had)\b"
        ),
        list_weak=_rx(r"\balle\b|\belke\b|\bieder\w*\b|\balles\b"),
        show_all=_rx(
            r"\balles (?:wat|over)\b|\bvertel (?:me )?alles\b|\btoon (?:me )?alles\b|\bwat weet je (?:allemaal )?(?:over|van)\b|\blaat (?:me )?alles zien\b"
        ),
        choice=_rx(r"\bof\b"),
        not_choice=_rx(r"\bof niet\b"),
        history=_rx(
            r"\bgeschiedenis\b|\bverloop\b|\bvroeger\b|\bvoorheen\b|\beerdere?\b|\bin de loop van de tijd\b|\b(?:is|zijn) .{0,40}\bveranderd\b"
        ),
        current=_rx(r"\bmomenteel\b|\bnu\b|\bop dit moment\b|\btegenwoordig\b|\bhuidige\b"),
        latest=_rx(
            r"\blaatst\b|\blaatste keer\b|\bvoor het laatst\b|\bmeest recente\b|\bnieuwste\b"
        ),
        comparison=_rx(
            r"\bvergelijk\w*\b|\bversus\b|\bvs\.?\b|\bverschil tussen\b|\b(?:\w{3,}er|meer|minder) dan\b"
        ),
        join=_rx(
            r"\bbeide[n]?\b|\ballebei\b|\bgemeen\b|\bgemeenschappelijk\w*\b|\bsamen\b|\bhoe lang (?:na|voor|tussen)\b|\bhetzelfde\b|\bdezelfde\b"
        ),
        between_join=_rx(r"\btussen\b.+\ben\b"),
        generic=_rx(
            r"\bhoofdstad van\b|\binwoners\b|\bbetekenis van\b|\bdefinitie\b|\bwie heeft \w+ (?:uitgevonden|geschreven|ontdekt|geschilderd|opgericht)\b|^\W*(?:leg uit|vertaal)\b|\bhoe hoog is\b|\bwat betekent\b|\bwat is een\b"
        ),
        first_person=frozenset(["ik", "mijn", "mij", "me", "wij", "we", "ons", "onze"]),
        second_person=frozenset(["jij", "jouw", "jou", "je"]),
        politeness=_rx(r"\b(?:kun|kan|zou) (?:je|jij|u)\b|\balsjeblieft\b"),
        bare_where=_rx(r"^\W*waar (?:is|zijn|was|waren|ligt|liggen) [^\s]+(?: [^\s]+)?\W*$"),
    ),
    "es": _Lex(
        heads=_heads(
            duration=r"\bcuanto tiempo\b|\bcuantos? (?:dias?|semanas?|meses|anos?|horas?|minutos?|noches)\b(?! (?:tiene|tienes|tengo|cumple|cumples|cumplo)\b)|\bcuantas (?:semanas|horas|minutos|noches)\b|\bcuanto (?:dura|duro|tardo|tarda)\b",
            count=r"\bcuantos\b|\bcuantas\b|\bnumero de\b|\bcon que frecuencia\b",
            amount=r"\bcuanto\b",
            date=r"\bcuando\b|\bque (?:fecha|dia|ano|mes|hora|semana)\b|\bdesde cuando\b|\bhasta cuando\b",
            free=r"\bpor que\b|\bpara que\b|\bcomo (?:ha|han|es|fue|era|se|puedo|llego|conocio)\b",
            entity=r"\bquien(?:es)?\b|\bdonde\b|\badonde\b|\bcual(?:es)?\b|^\W*(?:\w+ ){0,2}?que\b",
            yesno=r"^\W*(?:(?:y|pero|oye)\s+)?(?:tiene|tienes|tengo|es|son|era|eran|fue|fueron|ha|han|he|has|hay|habia|puede|puedes|vive|vives|trabaja|trabajas|esta|estan|sigue|habla|sabe|conoce|hizo|hice|dijo)\b",
        ),
        list_strong=_rx(r"\blista\b|\benumera\b|\bmenciona todos\b|\bcuales son\b"),
        list_weak=_rx(r"\btodos\b|\btodas\b|\btodo lo\b|\bcada\b"),
        show_all=_rx(
            r"\btodo (?:lo que|sobre)\b|\bdime todo\b|\bmuestrame todo\b|\bque sabes (?:de|sobre|acerca)\b|\bcuentame todo\b"
        ),
        choice=_rx(r"\bo\b"),
        not_choice=_rx(r"\bo no\b"),
        history=_rx(
            r"\bhistorial\b|\bhistoria de\b|\blinea de tiempo\b|\bantes(?! de)\b|\banteriormente\b|\bpreviamente\b|\bsolia\w*\b|\ba lo largo del tiempo\b|\bcon el tiempo\b|\b(?:ha|han) cambiado\b"
        ),
        current=_rx(
            r"\bactualmente\b|\bahora\b|\ben este momento\b|\bhoy en dia\b|\bactual(?:es)?\b|\bpor ahora\b"
        ),
        latest=_rx(r"\bultima vez\b|\bmas reciente\b|\bultimo\b|\bmas nuevo\b"),
        comparison=_rx(
            r"\bcompara\w*\b|\bcomparacion\b|\bfrente a\b|\bdiferencia entre\b|\b(?:mas|menos) \w+ que\b|\b(?:mayor|menor|mejor|peor) que\b|\bquien (?:fue|llego|nacio|empezo) primero\b"
        ),
        join=_rx(
            r"\bambos\b|\bambas\b|\ben comun\b|\bjuntos\b|\bjuntas\b|\bcuanto tiempo (?:despues|antes|entre)\b|\bel mismo\b|\bla misma\b|\bmismo\b"
        ),
        between_join=_rx(r"\bentre\b.+\by\b"),
        generic=_rx(
            r"\bcapital de\b|\bpoblacion de\b|\bsignificado de\b|\bdefinicion\b|\bquien (?:escribio|invento|descubrio|pinto|compuso|fundo)\b|^\W*(?:explica|traduce)\b|\bque significa\b|\bque es (?:un|una)\b|\bcuanto mide\b"
        ),
        first_person=frozenset(
            [
                "yo",
                "mi",
                "mis",
                "me",
                "nosotros",
                "nosotras",
                "nuestro",
                "nuestra",
                "nuestros",
                "nuestras",
                "conmigo",
            ]
        ),
        second_person=frozenset(["tu", "tus", "ti", "usted"]),
        politeness=_rx(r"\b(?:puedes|podrias|puede|podria)\b|\bpor favor\b"),
        bare_where=_rx(r"^\W*donde (?:esta|estan|queda|fue|era) [^\s]+(?: [^\s]+)?\W*$"),
    ),
}


# ---------------------------------------------------------------------------------- dates and times

_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "maerz": 3, "mai": 5, "juni": 6, "juli": 7, "oktober": 10,
    "dezember": 12, "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}  # fmt: skip
_MON = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_ISO = _rx(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DOT = _rx(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DMY = _rx(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+(?:de\s+|of\s+)?{_MON}(?:\s+(?:de\s+)?(\d{{4}}))?\b"
)
_MDY = _rx(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}}))?\b")
_MY = _rx(rf"\b{_MON}\s+(?:de\s+|of\s+)?(\d{{4}})\b")
_YEAR = _rx(r"\b(1[89]\d\d|20\d\d)\b")
_MONTH_ONLY = _rx(rf"\b{_MON}\b")
_VAGUE = _rx(
    r"\bpast (?:few )?(?:days|weeks|months|years)\b|\brecently\b|\blately\b|\bkuerzlich\b|\bneulich\b"
    r"|\bonlangs\b|\brecentelijk\b|\brecientemente\b|\bultimamente\b|\bafgelopen (?:dagen|weken|maanden|jaren)\b"
    r"|\bin den letzten (?:tagen|wochen|monaten|jahren)\b"
)

_BEFORE = r"before|prior to|vor|voor|antes de|antes del"
_UNTIL = r"until|till|by|up to|bis|tot aan|tot|hasta|uiterlijk"
_AFTER = r"after|nach|na|despues de|despues del|tras|following"
_SINCE = r"since|from|seit|ab|sinds|vanaf|desde|a partir de"
_FILL = r"(?:(?:the|dem|der|den|het|de|el|la|im|jahr|jaar|ano|year|mid|end|ende|einde|finales|del|in)\s+)*"
_DIRS = tuple(
    (name, _rx(rf"(?:^|\W)(?:{alts})\s+{_FILL}$"))
    for name, alts in (("before", _BEFORE), ("until", _UNTIL), ("after", _AFTER), ("since", _SINCE))
)
_BETWEEN = _rx(
    r"\bbetween\b.+\band\b|\bfrom\b.+\bto\b|\bzwischen\b.+\bund\b|\bvon\b.+\bbis\b|\btussen\b.+\ben\b"
    r"|\bvan\b.+\btot\b|\bentre\b.+\by\b|\bdesde\b.+\bhasta\b|\b\d{4}\s*-\s*\d{4}\b"
)
_AS_OF = _rx(
    rf"\bas of\b|\bas at\b|\bback in\b|\bat the time of\b|\bstand (?:vom|von|am|zum|\d)|\bzum stichtag\b"
    rf"|\bzum zeitpunkt\b|\bper (?=\d|{_MON}\b)|\bdamals\b|\bdestijds\b|\bop dat moment\b"
    rf"|\ba fecha de\b|\ben aquel momento\b|\bpor entonces\b|\ben ese entonces\b"
)

# Relative phrases. The English/German/Dutch tables and the calendar rules below mirror
# ``memspine.derive.temporal`` (LIB-01): the layering contract forbids ``read`` importing ``derive``, so the
# few rules are repeated here and a parity test (tests/unit/lib/test_lib_16_contract_details.py) pins them
# to LIB-01's own ``normalise_relative_phrase`` + ``anchor``.
_FIXED: dict[tuple[str, ...], tuple[str, str, int | None]] = {
    ("yesterday",): ("ago", "day", 1),
    ("today",): ("this", "day", None),
    ("tomorrow",): ("ahead", "day", 1),
    ("the", "day", "before", "yesterday"): ("ago", "day", 2),
    ("the", "day", "after", "tomorrow"): ("ahead", "day", 2),
    ("gestern",): ("ago", "day", 1),
    ("heute",): ("this", "day", None),
    ("morgen",): ("ahead", "day", 1),
    ("vorgestern",): ("ago", "day", 2),
    ("uebermorgen",): ("ahead", "day", 2),
    ("gisteren",): ("ago", "day", 1),
    ("vandaag",): ("this", "day", None),
    ("eergisteren",): ("ago", "day", 2),
    ("overmorgen",): ("ahead", "day", 2),
}
_LAST_W = frozenset({"last", "previous", "letzte", "letzten", "letztes", "letzter", "vergangene",
                     "vergangenen", "vergangenes", "vorige", "vorig", "vorigen"})  # fmt: skip
_NEXT_W = frozenset({"next", "naechste", "naechsten", "naechstes", "naechster", "kommende",
                     "kommenden", "kommendes", "volgende", "volgend", "komende", "komend"})  # fmt: skip
_THIS_W = frozenset({"this", "current", "diese", "diesen", "dieses", "dieser", "deze", "dit"})
_SPAN_UNITS: dict[str, str] = {
    "day": "day", "week": "week", "month": "month", "year": "year", "weekend": "weekend",
    "tag": "day", "woche": "week", "monat": "month", "jahr": "year", "wochenende": "weekend",
    "dag": "day", "maand": "month", "jaar": "year",
}  # fmt: skip
_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5,
    "sonnabend": 5, "sonntag": 6,
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4, "zaterdag": 5,
    "zondag": 6,
}  # fmt: skip
_EN_UNITS: dict[str, str] = {
    "minute": "minute", "minutes": "minute", "hour": "hour", "hours": "hour", "day": "day",
    "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
    "year": "year", "years": "year",
}  # fmt: skip
_DE_UNITS: dict[str, str] = {
    "minute": "minute", "minuten": "minute", "stunde": "hour", "stunden": "hour", "tag": "day",
    "tage": "day", "tagen": "day", "woche": "week", "wochen": "week", "monat": "month",
    "monate": "month", "monaten": "month", "jahr": "year", "jahre": "year", "jahren": "year",
}  # fmt: skip
_NL_UNITS: dict[str, str] = {
    "minuut": "minute", "minuten": "minute", "uur": "hour", "uren": "hour", "dag": "day",
    "dagen": "day", "week": "week", "weken": "week", "maand": "month", "maanden": "month",
    "jaar": "year", "jaren": "year",
}  # fmt: skip
_NUMBERS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "ein": 1, "eine": 1, "einer": 1, "einem": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwoelf": 12,
    "een": 1, "twee": 2, "drie": 3, "vijf": 5, "zes": 6, "zeven": 7, "negen": 9, "tien": 10, "twaalf": 12,
}  # fmt: skip
_ALL_UNITS = {**_EN_UNITS, **_DE_UNITS, **_NL_UNITS}
_NL_ONLY = frozenset(set(_NL_UNITS) - set(_EN_UNITS) - set(_DE_UNITS))

_ES_NUM = {"un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
           "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12}  # fmt: skip
_ES_UNIT = {"dia": "day", "dias": "day", "semana": "week", "semanas": "week", "mes": "month",
            "meses": "month", "ano": "year", "anos": "year", "hora": "hour", "horas": "hour",
            "minuto": "minute", "minutos": "minute"}  # fmt: skip
_ES_FIXED = {("ayer",): ("ago", "day", 1), ("hoy",): ("this", "day", None),
            ("anteayer",): ("ago", "day", 2), ("antes", "de", "ayer"): ("ago", "day", 2)}  # fmt: skip
_ES_SPAN = {"semana": "week", "mes": "month", "ano": "year"}


@dataclass(frozen=True)
class _Rel:
    rel: str  # ago / ahead / last / this / next
    unit: str  # minute hour day week month year weekday weekend
    n: int | None = None
    weekday: int | None = None


def _number(tok: str) -> int | None:
    if tok.isascii() and tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 100_000 else None
    return _NUMBERS.get(tok)


def _es_num(tok: str) -> int | None:
    if tok.isascii() and tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 100_000 else None
    return _ES_NUM.get(tok)


def _parse_relative(toks: tuple[str, ...]) -> _Rel | None:
    """A relative phrase (folded tokens) in English, German, Dutch or Spanish, or ``None`` if not sure."""
    if not toks:
        return None
    if toks in _FIXED:
        rel, unit, n = _FIXED[toks]
        return _Rel(rel, unit, n)
    if toks in _ES_FIXED:
        rel, unit, n = _ES_FIXED[toks]
        return _Rel(rel, unit, n)
    if len(toks) == 2:
        det, word = toks
        rel = ""
        if det in _LAST_W:
            rel = "last"
        elif det in _NEXT_W:
            rel = "next"
        elif det in _THIS_W:
            rel = "this"
        if rel and word in _SPAN_UNITS:
            return _Rel(rel, _SPAN_UNITS[word])
        if rel and word in _WEEKDAYS:
            return _Rel(rel, "weekday", None, _WEEKDAYS[word])
        if det in {"esta", "este"} and word in _ES_SPAN:
            return _Rel("this", _ES_SPAN[word])
        return None
    if len(toks) == 3:
        a, b, c = toks
        if c == "ago" and a != "in" and b in _EN_UNITS and (n := _number(a)) is not None:
            return _Rel("ago", _EN_UNITS[b], n)
        if c == "geleden" and b in _NL_UNITS and (n := _number(a)) is not None:
            return _Rel("ago", _NL_UNITS[b], n)
        if a == "hace" and c in _ES_UNIT and (n := _es_num(b)) is not None:
            return _Rel("ago", _ES_UNIT[c], n)
        if a in {"el", "la"}:
            if c in {"pasado", "pasada"} and b in _ES_SPAN:
                return _Rel("last", _ES_SPAN[b])
            if c in {"proximo", "proxima"} and b in _ES_SPAN:
                return _Rel("next", _ES_SPAN[b])
            if b in {"proximo", "proxima"} and c in _ES_SPAN:
                return _Rel("next", _ES_SPAN[c])
            return None
        n = _number(b)
        if n is None:
            return None
        if a == "in" and c in _ALL_UNITS:
            return _Rel("ahead", _ALL_UNITS[c], n)
        if a == "vor" and c in _DE_UNITS:
            return _Rel("ago", _DE_UNITS[c], n)
        if a == "over" and c in _NL_ONLY:
            return _Rel("ahead", _NL_UNITS[c], n)
        return None
    if len(toks) == 4 and toks[:2] == ("dentro", "de") and toks[3] in _ES_UNIT:
        n = _es_num(toks[2])
        if n is not None:
            return _Rel("ahead", _ES_UNIT[toks[3]], n)
    return None


def _add_months(d: date, k: int) -> date:
    total = d.year * 12 + (d.month - 1) + k
    year, month0 = divmod(total, 12)
    if not 1 <= year <= 9999:
        raise OverflowError("year out of range")
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _midnight(d: date, tz: tzinfo | None) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz)


def _floor(dt: datetime, precision: str) -> datetime:
    if precision == "minute":
        return dt.replace(second=0, microsecond=0)
    if precision == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    d = dt.date()
    if precision == "week":
        d = d - timedelta(days=d.weekday())
    elif precision == "month":
        d = d.replace(day=1)
    elif precision == "year":
        d = date(d.year, 1, 1)
    return _midnight(d, dt.tzinfo)


def _shift(floor: datetime, precision: str, k: int) -> datetime:
    if precision in {"minute", "hour"}:
        step = timedelta(minutes=k) if precision == "minute" else timedelta(hours=k)
        return (floor.astimezone(UTC) + step).astimezone(floor.tzinfo)
    d = floor.date()
    if precision == "day":
        nd = d + timedelta(days=k)
    elif precision == "week":
        nd = d + timedelta(days=7 * k)
    elif precision == "month":
        nd = _add_months(d, k)
    else:  # year
        nd = _add_months(d, 12 * k)
    return _midnight(nd, floor.tzinfo)


def _anchor_relative(r: _Rel, now: datetime) -> tuple[datetime, datetime, str] | None:
    """The half-open period a relative phrase stands for, plus its precision (mirrors LIB-01)."""
    try:
        if r.rel in {"ago", "ahead"}:
            k = (r.n or 1) * (-1 if r.rel == "ago" else 1)
            if r.unit in {"minute", "hour"}:
                step = timedelta(minutes=k) if r.unit == "minute" else timedelta(hours=k)
                lo = _floor((now.astimezone(UTC) + step).astimezone(now.tzinfo), r.unit)
            else:
                lo = _shift(_floor(now, r.unit), r.unit, k)
            return lo, _shift(lo, r.unit, 1), r.unit
        step1 = {"last": -1, "this": 0, "next": 1}[r.rel]
        if r.unit in {"day", "week", "month", "year"}:
            lo = _shift(_floor(now, r.unit), r.unit, step1)
            return lo, _shift(lo, r.unit, 1), r.unit
        d = now.date()
        if r.unit == "weekday" and r.weekday is not None:
            if r.rel == "this":
                target = d - timedelta(days=d.weekday()) + timedelta(days=r.weekday)
            elif r.rel == "last":
                target = d - timedelta(days=(d.weekday() - r.weekday) % 7 or 7)
            else:
                target = d + timedelta(days=(r.weekday - d.weekday()) % 7 or 7)
            lo = _midnight(target, now.tzinfo)
            return lo, _shift(lo, "day", 1), "day"
        if r.unit == "weekend":
            if d.weekday() < 5:
                sat = d + timedelta(days=5 - d.weekday())
            else:
                sat = d - timedelta(days=d.weekday() - 5)
            if r.rel == "last":
                sat = sat - timedelta(days=7)
            elif r.rel == "next":
                if d.weekday() < 5:
                    return None  # "next weekend" from a weekday is ambiguous: never guess
                sat = sat + timedelta(days=7)
            lo = _midnight(sat, now.tzinfo)
            return lo, _shift(lo, "day", 1), "day"
    except (OverflowError, ValueError):
        return None
    return None


def _period_of(lo: datetime, precision: str) -> tuple[datetime, datetime]:
    return lo, _shift(lo, precision, 1)


@dataclass
class _Mention:
    start: int
    end: int
    text: str
    lo: datetime | None
    hi: datetime | None
    precision: str
    direction: str = ""  # before / until / after / since / ""
    vague: bool = False

    @property
    def anchored(self) -> bool:
        return self.lo is not None and self.hi is not None


def _abs_mention(m: re.Match[str], y: int, mo: int, d: int | None, tz: tzinfo) -> _Mention | None:
    prec = "day" if d is not None else ("month" if mo else "year")
    try:
        lo = datetime(y, mo or 1, d or 1, tzinfo=tz)
    except ValueError:
        return None
    lo, hi = _period_of(lo, prec)
    return _Mention(m.start(), m.end(), m.group(0), lo, hi, prec)


def _find_mentions(folded: str, lang: str, now: datetime | None, tz: tzinfo) -> list[_Mention]:
    out: list[_Mention] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    def add(men: _Mention | None) -> None:
        if men is not None and free(men.start, men.end):
            out.append(men)
            taken.append((men.start, men.end))

    for m in _ISO.finditer(folded):
        add(_abs_mention(m, int(m[1]), int(m[2]), int(m[3]), tz))
    for m in _DOT.finditer(folded):
        add(_abs_mention(m, int(m[3]), int(m[2]), int(m[1]), tz))
    for m in _DMY.finditer(folded):
        if not free(m.start(), m.end()):
            continue
        if m[3]:
            add(_abs_mention(m, int(m[3]), _MONTHS[m[2]], int(m[1]), tz))
        else:
            add(_Mention(m.start(), m.end(), m.group(0), None, None, "day"))
    for m in _MDY.finditer(folded):
        if not free(m.start(), m.end()):
            continue
        if m[3]:
            add(_abs_mention(m, int(m[3]), _MONTHS[m[1]], int(m[2]), tz))
        else:
            add(_Mention(m.start(), m.end(), m.group(0), None, None, "day"))
    for m in _MY.finditer(folded):
        add(_abs_mention(m, int(m[2]), _MONTHS[m[1]], None, tz))
    for m in _YEAR.finditer(folded):
        add(_abs_mention(m, int(m[1]), 0, None, tz))
    for m in _MONTH_ONLY.finditer(folded):
        if m[1] == "may" or not free(m.start(), m.end()):
            continue  # "may" alone is a modal verb; a month with no year is never completed
        add(_Mention(m.start(), m.end(), m.group(0), None, None, "month"))
    for m in _VAGUE.finditer(folded):
        add(_Mention(m.start(), m.end(), m.group(0), None, None, "unknown", vague=True))

    # relative phrases, longest window first, left to right
    toks = [(t.group(0), t.start(), t.end()) for t in re.finditer(r"[a-z0-9]+", folded)]
    i = 0
    while i < len(toks):
        matched = False
        for n in (4, 3, 2, 1):
            if i + n > len(toks):
                continue
            window = toks[i : i + n]
            s, e = window[0][1], window[-1][2]
            if not free(s, e):
                continue
            words = tuple(w[0] for w in window)
            if s > 0 and folded[s - 1] in "-+" and words[0].isdigit():
                continue  # a signed number is not a phrase we understand (as LIB-01)
            if words == ("morgen",) and lang == "de":
                prev = toks[i - 1][0] if i else ""
                if prev in {"heute", "gestern", "vorgestern", "am", "jeden", "diesen", "frueh"}:
                    continue
            if words == ("morgen",) and lang == "es":
                continue
            phrase = " ".join(words)
            rel = _parse_relative(words)
            if rel is None:
                continue
            span = _anchor_relative(rel, now) if now is not None else None
            if span is None:
                add(_Mention(s, e, phrase, None, None, "unknown"))
            else:
                add(_Mention(s, e, phrase, span[0], span[1], span[2]))
            i += n
            matched = True
            break
        if not matched:
            i += 1
    out.sort(key=lambda x: x.start)
    for men in out:
        prefix = folded[: men.start].rstrip()
        for name, rx in _DIRS:
            if rx.search(prefix + " "):
                men.direction = name
                break
    return out


# --------------------------------------------------------------------------------------- name finding

_TOK = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
_STOP = frozenset(
    _fold(
        """
        i you he she it we they what when where who whom whose which why how did does do is are was were
        has have had will would can could should shall may might and or but so if the a an in on at to of
        for with by from as not no yes please tell show give list name explain define translate compare
        wer wie was wann wo warum welche welcher welches wen wem und oder aber ob der die das den dem des
        ein eine einen einem ich du er sie es wir ihr mein dein sein unser hat ist sind war waren
        wie wat waar wanneer welke welk waarom hoe en of maar de het een ik jij hij zij wij jullie heeft
        zijn que cuando donde quien cual cuales como por el la los las un una yo tu el ella nosotros y o
        pero si es son fue mom dad mum mama papa oma opa mutti vati english german dutch spanish
        englisch deutsch niederlaendisch nederlands engels duits ingles aleman espanol spanisch spaans
        monday tuesday wednesday thursday friday saturday sunday montag dienstag mittwoch donnerstag
        freitag samstag sonnabend sonntag maandag dinsdag woensdag donderdag vrijdag zaterdag zondag
        lunes martes miercoles jueves viernes sabado domingo
        """
    ).split()
) | frozenset(_MONTHS)
_LOC = frozenset(
    [
        "in",
        "at",
        "near",
        "to",
        "into",
        "across",
        "nach",
        "bei",
        "naar",
        "bij",
        "en",
        "cerca",
        "hacia",
        "aus",
        "uit",
        "te",
    ]
)
_WITH = frozenset(["with", "mit", "met", "con"])
_DE_TRIG = frozenset(
    [
        "von",
        "bei",
        "mit",
        "fuer",
        "ueber",
        "an",
        "zu",
        "ob",
        "ist",
        "hat",
        "war",
        "hatte",
        "wird",
        "kann",
        "mag",
        "sind",
        "wo",
        "wer",
        "wann",
        "frau",
        "herr",
        "dr",
        "onkel",
        "tante",
        "und",
        "als",
        "haben",
        "hast",
        "habe",
        "hatten",
        "wurde",
        "wurden",
        "werden",
        "koennen",
    ]
)
_PARTICLES = frozenset(["van", "von", "de", "del", "der", "den", "la", "di", "da"])
_QUOTE = re.compile(r"[\"“„«]([^\"”“»]{1,60})[\"”“»]")


_DE_NOT_VERB = frozenset(
    [
        "nicht",
        "mit",
        "seit",
        "jetzt",
        "bereit",
        "damit",
        "statt",
        "zuletzt",
        "gesamt",
        "hochzeit",
        "nacht",
        "acht",
        "macht",
    ]
)


def _de_verb(prev: str) -> bool:
    """A German finite verb (3rd person singular ends in t) right before a capitalised word: a name more
    often than a bare noun. Adjective and article endings (-e, -en, -er) never qualify."""
    return len(prev) > 3 and prev.endswith("t") and prev not in _DE_NOT_VERB


@dataclass
class _Name:
    text: str
    possessive: bool
    loc: bool
    with_prep: bool


def _find_names(raw: str, lang: str) -> list[_Name]:
    toks = [(m.group(0), m.start(), m.end()) for m in _TOK.finditer(raw)]
    groups: list[_Name] = []
    open_group = False
    last_idx = -2
    poss_end = False
    for i, (tok, s, e) in enumerate(toks):
        poss = bool(re.search(r"['’]s?$", tok)) and tok[:1].isupper()
        base = re.sub(r"['’]s?$", "", tok)
        prev = _fold(toks[i - 1][0]) if i else ""
        is_cap = base[:1].isupper() and len(base) > 1 and _fold(base) not in _STOP
        if not is_cap:
            continue
        licensed = True
        if i == 0:
            licensed = poss or raw[e : e + 1] == ","
        if lang == "de" and not (poss or prev in _DE_TRIG or _de_verb(prev)):
            licensed = False
        gap = raw[toks[i - 1][2] : s] if i else "x"
        adjacent = open_group and last_idx == i - 1 and not poss_end and gap.strip() == ""
        j = i - 1
        while j >= 0 and toks[j][0].islower() and _fold(toks[j][0]) in _PARTICLES:
            j -= 1
        with_particle = (
            open_group
            and j < i - 1
            and last_idx == j
            and not poss_end
            and raw[toks[j][2] : s].split() == [t[0] for t in toks[j + 1 : i]]
        )
        if adjacent or with_particle:
            g = groups[-1]
            joiner = " " + " ".join(t[0] for t in toks[j + 1 : i]) + " " if with_particle else " "
            groups[-1] = _Name(g.text + joiner + base, g.possessive or poss, g.loc, g.with_prep)
        elif licensed:
            groups.append(_Name(base, poss, prev in _LOC and not poss, prev in _WITH))
            open_group = True
        else:
            open_group = False
            continue
        last_idx, poss_end = i, poss
    seen: set[str] = set()
    out: list[_Name] = []
    for g in groups:
        if g.text not in seen:
            seen.add(g.text)
            out.append(g)
    return out


# ------------------------------------------------------------------------------------- attribute hints

_ATTRS: dict[str, frozenset[str]] = {
    k: frozenset(v.split())
    for k, v in {
        "birthday": "birthday birthdate born birth bday geburtstag geboren geburtsdatum verjaardag geboortedatum cumpleanos nacimiento nacio nacido naci",
        "job": "job work works worked working occupation profession career arbeit arbeitet arbeite beruf stelle werk werkt baan beroep trabajo trabaja empleo profesion",
        "employer": "employer company boss arbeitgeber firma unternehmen werkgever bedrijf empresa jefe baas chef",
        "residence": "live lives lived living address home resides moved move wohnt wohne wohnen adresse wohnort umgezogen zuhause woont woon adres woonplaats verhuisd thuis vive vivo viven direccion domicilio mudo mudado",
        "email": "email mail correo",
        "phone": "phone telephone mobile handy telefon telefoonnummer telefono movil celular gsm",
        "salary": "salary pay wage income gehalt lohn einkommen salaris loon inkomen salario sueldo",
        "pet": "pet dog cat haustier hund katze huisdier hond kat mascota perro gato",
        "car": "car vehicle auto wagen coche carro vehiculo",
        "hobby": "hobby hobbies pastime hobbys interests freizeit aficion aficiones pasatiempo",
        "diet": "diet vegetarian vegan diaet ernaehrung dieet vegetarisch dieta vegetariano",
        "allergy": "allergy allergic allergies allergie allergisch allergien alergia alergico",
        "partner": "partner spouse wife husband girlfriend boyfriend married ehepartner ehefrau ehemann freundin freund verheiratet echtgenoot echtgenote gehuwd getrouwd vriendin vriend esposa esposo marido novia novio casado pareja",
        "sibling": "sister brother sibling schwester bruder geschwister zus zuster broer hermana hermano",
        "parent": "mother father mom dad parents mutter vater eltern mama papa moeder vader ouders madre padre padres",
        "child": "child children son daughter kid kids kind kinder sohn tochter kinderen zoon dochter hijo hija hijos",
        "education": "degree school university studied study major studium studiert universitaet abschluss schule studie studeerde universiteit opleiding escuela universidad estudio",
        "medication": "medication medicine prescription pill medikament medikamente tabletten medicijn medicijnen medicamento receta",
        "age": "age alter leeftijd edad",
    }.items()
}


def _attributes(folded: str) -> tuple[str, ...]:
    seen: list[str] = []
    for tok in re.findall(r"[a-z]+", folded):
        for key, words in _ATTRS.items():
            if (tok in words or (tok.endswith("s") and tok[:-1] in words)) and key not in seen:
                seen.append(key)
    return tuple(seen)


# ------------------------------------------------------------------------------------------- the build


def _conf(d: dict[str, float], key: str, value: float) -> None:
    d[key] = round(max(0.0, min(1.0, value)), 2)


def _answer_type(
    folded: str, raw: str, lex: _Lex, lang: str, conf: dict[str, float]
) -> tuple[AnswerType, int]:
    """(answer type, head position). Earliest head wins, ties by order of ``heads``."""
    best: tuple[int, int, str] | None = None
    for order, (cat, rx) in enumerate(lex.heads):
        m = rx.search(folded)
        if m is not None:
            cand = (m.start(), order, cat)
            if best is None or cand < best:
                best = cand
    strong = lex.list_strong.search(folded) is not None
    weak = lex.list_weak.search(folded) is not None
    show = lex.show_all.search(folded) is not None
    head = best[2] if best else None
    pos = best[0] if best else -1
    near = best is not None and pos <= 14
    if show:
        _conf(conf, "answer_type", 0.6)
        return "list", pos
    if head in {"duration", "count", "date"}:
        _conf(conf, "answer_type", 0.9 if near else 0.75)
        return head, pos  # type: ignore[return-value]
    if head == "yesno":
        if lex.choice.search(folded) and not lex.not_choice.search(folded):
            _conf(conf, "answer_type", 0.55)
            return "entity", pos
        _conf(conf, "answer_type", 0.85)
        return "yesno", pos
    if head in {"entity", None} and (strong or (weak and head is None)):
        _conf(conf, "answer_type", 0.8 if strong else 0.6)
        return "list", pos
    if head == "entity" and weak:
        _conf(conf, "answer_type", 0.6)
        return "list", pos
    if head == "free":
        _conf(conf, "answer_type", 0.75 if near else 0.55)
        return "free", pos
    if head == "amount":
        _conf(conf, "answer_type", 0.5)
        return "free", pos
    if head == "entity":
        _conf(conf, "answer_type", 0.7 if near else 0.5)
        return "entity", pos
    if lang == "es" and ("¿" in raw or raw.rstrip().endswith("?")) and head is None:
        _conf(conf, "answer_type", 0.5)
        return "yesno", pos
    return "unknown", pos


def _personal(
    folded: str,
    lex: _Lex | None,
    names: list[_Name],
    conf: dict[str, float],
) -> bool | None:
    if lex is None:
        return None
    text = lex.politeness.sub(" ", folded)
    toks = set(re.findall(r"[a-z]+(?:'[a-z]+)?", text))
    if toks & lex.first_person:
        _conf(conf, "personal", 0.95)
        return True
    if toks & lex.second_person:
        _conf(conf, "personal", 0.65)
        return True
    generic = lex.generic.search(text) is not None
    people = [n for n in names if not n.loc]
    if generic:
        _conf(conf, "personal", 0.8 if not names else 0.65)
        return False
    if lex.bare_where.search(folded):
        return None
    if people:
        strong = any(n.possessive or n.with_prep for n in people)
        _conf(conf, "personal", 0.75 if strong else 0.6)
        return True
    return None


def build_contract(
    query: str, *, now: datetime | None = None, lang_hint: str | None = None
) -> TypedContract:
    """The rules-only typed contract of ``query``. Never raises; an empty or unreadable question gives
    the all-unknown contract. ``now`` anchors relative time phrases (naive ``now`` is read as UTC)."""
    if not isinstance(query, str) or not query.strip():
        return TypedContract(unknown=("answer_type", "shape", "personal", "language"))
    raw = query.strip()
    folded = re.sub(r"[¿¡]", " ", _fold(raw)).strip()
    conf: dict[str, float] = {}
    lang, lconf, supported = _detect_language(raw, folded, lang_hint)
    _conf(conf, "language", lconf)
    lex = _LEX.get(lang) if supported else None
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    tz: tzinfo = now.tzinfo if now is not None and now.tzinfo is not None else UTC

    # --- answer type, list/count flags
    if lex is not None:
        core = lex.politeness.sub(" ", folded).strip()
        answer_type, _pos = _answer_type(core, raw, lex, lang, conf)
    else:
        answer_type = "unknown"
    if answer_type == "unknown":
        _conf(conf, "answer_type", 0.0)

    # --- names, attributes
    names = _find_names(_QUOTE.sub(lambda m: " " * len(m.group(0)), raw), lang)
    targets: list[str] = []
    for n in names:
        if n.text not in targets:
            targets.append(n.text)
    for q in _QUOTE.finditer(raw):
        t = q.group(1).strip()
        if t and t not in targets:
            targets.append(t)
    attrs = _attributes(folded)
    _conf(conf, "targets", 0.7 if targets else 0.0)
    _conf(conf, "attributes", 0.7 if attrs else 0.0)

    # --- time view
    mentions = _find_mentions(folded, lang, now, tz)
    view, vconf, vconflict, via_between = _build_view(folded, lex, mentions)
    _conf(conf, "time_view", vconf)

    # --- shape
    shape: Shape = "unknown"
    sconf = 0.0
    if lex is not None:
        join_hit = lex.join.search(folded) is not None or (
            lex.between_join.search(folded) is not None and not via_between
        )
        if view.kind == "history":
            shape, sconf = "history", 0.8
        elif view.kind == "as_of":
            shape, sconf = "as_of", 0.8
        elif lex.comparison.search(folded):
            shape, sconf = "comparison", 0.75
        elif join_hit:
            shape, sconf = "join", 0.7
        elif answer_type in {"list", "count"}:
            shape, sconf = "list_count", conf.get("answer_type", 0.6)
        elif lex.latest.search(folded) or (view.kind == "current" and answer_type != "unknown"):
            shape, sconf = "latest", 0.75
        elif answer_type != "unknown":
            shape, sconf = "single", 0.6
    _conf(conf, "shape", sconf)

    # --- personal
    personal = _personal(folded, lex, names, conf)
    if personal is None:
        _conf(conf, "personal", 0.0)

    # --- request kind
    request: RequestKind = "lookup"
    rconf = 0.6 if answer_type != "unknown" else 0.5
    if lex is not None and lex.show_all.search(folded):
        request, rconf = "show_all", 0.85
    elif answer_type in {"list", "count"} or view.kind == "history":
        request, rconf = "enumeration", 0.75
    elif view.kind in {"as_of", "range"}:
        request, rconf = "as_of", 0.8 if view.kind == "as_of" else 0.6
    _conf(conf, "request_kind", rconf)

    cardinality: Literal["one", "many", "count"] = (
        "count" if answer_type == "count" else "many" if answer_type == "list" else "one"
    )
    unknown = []
    if answer_type == "unknown":
        unknown.append("answer_type")
    if shape == "unknown":
        unknown.append("shape")
    if personal is None:
        unknown.append("personal")
    if lang == "unknown" or not supported:
        unknown.append("language")
    if vconflict:
        unknown.append("time_view")
    return TypedContract(
        answer_type=answer_type,
        shape=shape,
        time_view=view,
        personal=personal,
        request_kind=request,
        cardinality=cardinality,
        targets=tuple(targets),
        attributes=attrs,
        language=lang,
        confidence=conf,
        unknown=tuple(unknown),
    )


def _build_view(
    folded: str, lex: _Lex | None, mentions: list[_Mention]
) -> tuple[TimeView, float, bool, bool]:
    """(view, confidence, conflict, range_from_between)."""
    history = lex is not None and lex.history.search(folded) is not None
    current = lex is not None and lex.current.search(folded) is not None
    as_of_marker = _AS_OF.search(folded) is not None
    first = mentions[0] if mentions else None
    phrase = first.text if first else ""
    prec = first.precision if first else "unknown"

    def make(kind: ViewKind, **kw: object) -> TimeView:
        return TimeView(kind=kind, phrase=phrase, precision=prec, **kw)  # type: ignore[arg-type]

    if not mentions:
        if history:
            return make("history"), 0.8, False, False
        if as_of_marker:
            return make("as_of"), 0.55, False, False
        if current:
            return make("current"), 0.8, False, False
        return TimeView(), 0.6, False, False

    # two or more time mentions
    if len(mentions) >= 2:
        a, b = mentions[0], mentions[1]
        if _BETWEEN.search(folded) and not (a.vague or b.vague):
            if a.anchored and b.anchored and a.lo is not None and b.hi is not None and a.lo <= b.hi:
                kind: ViewKind = "history" if history else "range"
                return make(kind, start=a.lo, end=b.hi, anchored=True), 0.8, False, True
            return make("history" if history else "range", anchored=False), 0.55, False, True
        return TimeView(), 0.0, True, False  # two unrelated time phrases: never guess

    men = first
    assert men is not None
    if men.vague:
        return make("history" if history else "range", anchored=False), 0.5, False, False
    if as_of_marker and not history:
        if men.anchored:
            return make("as_of", as_of=men.lo, anchored=True), 0.85, False, False
        return make("as_of", anchored=False), 0.6, False, False
    kind = "history" if history else "range"
    if not men.anchored:
        return make(kind, anchored=False), 0.6, False, False
    lo, hi = men.lo, men.hi
    if men.direction == "before":
        return make(kind, start=None, end=lo, anchored=True), 0.8, False, False
    if men.direction == "until":
        return make(kind, start=None, end=hi, anchored=True), 0.8, False, False
    if men.direction == "after":
        return make(kind, start=hi, end=None, anchored=True), 0.8, False, False
    if men.direction == "since":
        return make(kind, start=lo, end=None, anchored=True), 0.8, False, False
    return make(kind, start=lo, end=hi, anchored=True), 0.85, False, False


# ----------------------------------------------------------------------------------------- the header


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt.date().isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M")


def _clean(s: str) -> str:
    return re.sub(r"[;|=\n\r]", " ", s).strip()[:40]


def render_header(contract: TypedContract, *, min_confidence: float = MIN_HEADER_CONFIDENCE) -> str:
    """The short structured header for the reader, e.g.
    ``[contract] type=date; shape=latest; view=range:2024-05-06..2024-05-13; req=as_of; scope=personal;
    targets=Ilse|Bram; attrs=birthday; lang=de``. Empty when nothing is known at the floor. Only fields
    with confidence >= ``min_confidence`` are printed; ``req=lookup`` (the default) and an unspecified
    view are omitted. Keys and values are language-neutral and the order is fixed."""
    c = contract.confidence

    def ok(key: str) -> bool:
        return c.get(key, 0.0) >= min_confidence and key not in contract.unknown

    parts: list[str] = []
    if contract.answer_type != "unknown" and ok("answer_type"):
        parts.append(f"type={contract.answer_type}")
    if contract.shape != "unknown" and ok("shape"):
        parts.append(f"shape={contract.shape}")
    v = contract.time_view
    if v.kind != "unspecified" and ok("time_view"):
        if v.kind == "as_of":
            parts.append(f"view=as_of:{_fmt_dt(v.as_of) or '?'}")
        elif v.kind in {"range", "history"}:
            span = f"{_fmt_dt(v.start)}..{_fmt_dt(v.end)}" if v.anchored else "unanchored"
            parts.append(f"view={v.kind}:{span}" if v.kind == "range" else f"view=history:{span}")
        else:
            parts.append(f"view={v.kind}")
        if v.kind == "history" and not v.anchored:
            parts[-1] = "view=history"
    if ok("request_kind") and contract.request_kind != "lookup":
        parts.append(f"req={contract.request_kind}")
    if contract.personal is not None and ok("personal"):
        parts.append(f"scope={'personal' if contract.personal else 'generic'}")
    if contract.targets and ok("targets"):
        parts.append("targets=" + "|".join(_clean(t) for t in contract.targets[:4]))
    if contract.attributes and ok("attributes"):
        parts.append("attrs=" + "|".join(contract.attributes[:4]))
    if not parts:
        return ""
    if contract.language != "unknown" and ok("language"):
        parts.append(f"lang={contract.language}")
    return "[contract] " + "; ".join(parts)
