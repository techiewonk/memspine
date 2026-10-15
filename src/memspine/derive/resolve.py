# ruff: noqa: RUF001
"""Rule-based entity resolver (LIB-04; decisions W-19, M-15; wired later by W2-09).

Pure and deterministic: no I/O, no model call, no engine import. Depends on ``memspine.schemas`` only.

Two layers:

* :func:`resolve_pair` decides whether two mentions are ``same``, ``different`` or ``pending_merge``,
  with a machine-readable reason. Only an exact normalised match, an alias rule, a numeric match or an
  exonym can say ``same``; fuzzy similarity can only propose ``pending_merge``. A false merge is worse
  than a missed one, so every doubtful case lands on ``pending_merge`` or ``different``.
* :class:`OnlineResolver` resolves one mention against candidate entities in one owner scope
  (tenant plus owner, S3-1; the session is provenance and never part of the key). Candidates from
  another scope are ignored, so entities never merge across owners.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from memspine.schemas.durable.assertion import Mention
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "Candidate",
    "EntityDecision",
    "MentionView",
    "OnlineResolver",
    "PairContext",
    "PairDecision",
    "normalise_name",
    "resolve_pair",
]

Verdict = Literal["same", "different", "pending_merge"]

FUZZY_PENDING_THRESHOLD = 0.88

# --------------------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------------------

# Transliteration (primary form). Diacritics not listed here are stripped via NFD.
_TRANSLIT = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ø": "oe",
    "å": "aa",
    "æ": "ae",
    "œ": "oe",
    "ß": "ss",
    "ĳ": "ij",
    "ł": "l",
    "đ": "d",
    "ð": "d",
    "þ": "th",
    "ı": "i",
}
# Accent-stripped form (second variant): the letter without its mark, as ASCII-only systems print it.
_STRIPPED = {**_TRANSLIT, "ä": "a", "ö": "o", "ü": "u", "ø": "o", "å": "a", "œ": "oe"}

_TITLES = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "frau",
        "herr",
        "mevrouw",
        "meneer",
        "sr",
        "sra",
        "mevr",
        "dhr",
        "prof",
        "mx",
        "sir",
    }
)
_PARTICLES = frozenset(
    {"van", "de", "der", "den", "ten", "ter", "te", "von", "vom", "zu", "zur", "da", "di", "del", "della",
     "dos", "das", "du", "la", "le", "el", "al", "bin", "ibn", "af", "av", "op", "het", "t"}
)  # fmt: skip

_ORDINAL = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b")
_DOT_KEEP = re.compile(r"(?<!\d)\.|\.(?!\d)")
_TOKEN_SPLIT = re.compile(r"\d+(?:\.\d+)*|[^\W\d_]+")


def _map_chars(text: str, table: Mapping[str, str]) -> str:
    out = "".join(table.get(ch, ch) for ch in text)
    nfd = unicodedata.normalize("NFD", out)
    return unicodedata.normalize("NFC", "".join(c for c in nfd if not unicodedata.combining(c)))


def _clean(text: str) -> str:
    """NFKC, casefold, drop apostrophes, turn punctuation into spaces (dots inside numbers stay)."""
    s = unicodedata.normalize("NFKC", text).casefold()
    s = s.replace("'", "").replace("’", "")
    s = _ORDINAL.sub("", s)
    s = _DOT_KEEP.sub(" ", s)
    s = "".join(ch if (ch.isalnum() or ch == ".") else " " for ch in s)
    return s


def _tokens(text: str, table: Mapping[str, str] = _TRANSLIT) -> list[str]:
    toks = _clean(_map_chars(_clean(text), table)).split()
    # NFKC may have produced fresh combining marks; _map_chars handled them. Strip leading titles.
    while len(toks) > 1 and toks[0] in _TITLES:
        toks = toks[1:]
    return toks


def normalise_name(text: str) -> str:
    """Primary normal form: casefold, NFKC, transliterated, titles and punctuation removed."""
    return " ".join(_tokens(text))


def _variants(tok: str, raw_tok: str | None = None) -> frozenset[str]:
    """Spelling variants of one normalised token (transliterated, accent-stripped, Slavic/Dutch spellings)."""
    base = {tok}
    if raw_tok:
        base.add(_tokens(raw_tok, _STRIPPED)[0] if _tokens(raw_tok, _STRIPPED) else tok)
    out = set(base)
    for t in list(base):
        if "ij" in t:
            out.add(t.replace("ij", "y"))
        out.add(t.replace("w", "v"))
        if t.startswith("j") and len(t) > 2:
            out.add("y" + t[1:])
            out.add("y" + t[1:].replace("w", "v"))
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class _Prepared:
    primary: str
    tokens: tuple[str, ...]
    tokens_v: tuple[frozenset[str], ...]  # per-token variants
    stripped: tuple[str, ...]
    numbers: tuple[tuple[int, ...], ...]
    words: tuple[str, ...]  # non-numeric tokens after numeric split


def _canon_number(raw: str) -> tuple[int, ...]:
    segs = [int(x) for x in raw.split(".") if x != ""]
    while len(segs) > 1 and segs[-1] == 0:
        segs.pop()
    return tuple(segs)


def _prepare(surface: str) -> _Prepared:
    toks = _tokens(surface)
    stripped = _tokens(surface, _STRIPPED)
    if len(stripped) != len(toks):  # defensive; the two tables only differ per character
        stripped = toks
    variants = tuple(_variants(t, raw) for t, raw in zip(toks, stripped, strict=True))
    numbers: list[tuple[int, ...]] = []
    words: list[str] = []
    for t in toks:
        for piece in _TOKEN_SPLIT.findall(t):
            if piece[0].isdigit():
                numbers.append(_canon_number(piece))
            else:
                words.append(piece)
    return _Prepared(
        " ".join(toks), tuple(toks), variants, tuple(stripped), tuple(numbers), tuple(words)
    )


# --------------------------------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------------------------------

# Cognate groups: formal given names that are the same name in different spellings or languages.
_COGNATES: tuple[frozenset[str], ...] = tuple(
    frozenset(g.split())
    for g in (
        "johannes johann johan john jean juan giovanni joao",
        "abraham abram avraham",
        "alexander aleksander aleksandr alexandr alexandre alessandro alejandro alexandros",
        "alexandra aleksandra alessandra alejandra",
        "elisabeth elizabeth elisabet elisabetta isabelle",
        "margarethe margarete margaret margareta margriet marguerite margherita",
        "katharina katherine catherine katarina katarzyna catharina caterina kathrin katrin",
        "michael michal michel miguel michele mikhail",
        "wilhelm willem william guillaume guillermo wilhelmus",
        "friedrich frederik frederick fredrik frederic federico",
        "heinrich hendrik henrik henry henri enrique hendrikus",
        "josef joseph jozef giuseppe jose",
        "peter pieter petrus pierre pedro piotr pietro petr",
        "paul paulus pablo pavel pawel paolo",
        "nikolaus nicolaas nicholas nikolai nicolas mikolaj nicolo",
        "dorothea dorothy dorothee",
        "theodor theodoor theodore teodor theodorus",
        "stefan stephan stephen steven stefano esteban",
        "karl carl karel charles carlos carlo",
        "robert roberto",
        "richard ricardo riccardo ryszard",
        "eduard edward eduardo edouard edvard",
        "andreas andrew andre andres andrzej anders",
        "jakob jacob jacobus jacques jaime",
        "maria marie mary marija",
        "anna anne ann",
        "johanna joanna jeanne giovanna",
        "tomasz tomas thomas tommaso tomaso",
        "abraham",
        "lukasz lucas lukas luca luc",
        "matthias mathias matthijs mattias matteo mateus mateusz matthew",
        "sebastian sebastiaan sebastien",
        "christian kristian christiaan cristian",
        "christina kristina christine cristina",
        "sophia sofia sophie",
        "daniel daniil",
        "david dawid davide",
        "martin marten maarten martijn martino",
        "gerhard gerard gerrit gerardo",
        "bernhard bernard bernardo bernd",
        "ludwig lodewijk louis luis luigi",
        "eugen eugene eugenio",
        "franz frans francis francisco francesco francois",
        "georg george joris jorge giorgio jerzy",
        "helena helene elena ilona",
        "hannah hanna hanne",
        "julia julie giulia yulia",
        "lukasz",
    )
)
# Nicknames and diminutives: nickname -> the formal names it shortens (looked up by cognate group).
_NICKS: dict[str, tuple[str, ...]] = {
    "bram": ("abraham",), "abe": ("abraham",),
    "tomek": ("tomasz",), "tom": ("thomas",), "tommy": ("thomas",), "tomi": ("tomasz",),
    "grete": ("margarethe",), "greta": ("margarethe",), "gretchen": ("margarethe",), "gretel": ("margarethe",),
    "meg": ("margarethe",), "maggie": ("margarethe",), "peggy": ("margarethe",), "marga": ("margarethe",),
    "margot": ("margarethe",), "grietje": ("margarethe",),
    "hans": ("johannes",), "jan": ("johannes",), "jannes": ("johannes",), "hannes": ("johannes",),
    "johnny": ("johannes",), "jonny": ("johannes",), "jack": ("johannes",),
    "liesbeth": ("elisabeth",), "lisbeth": ("elisabeth",), "liz": ("elisabeth",), "beth": ("elisabeth",),
    "betty": ("elisabeth",), "eliza": ("elisabeth",), "els": ("elisabeth",), "elsa": ("elisabeth",),
    "lies": ("elisabeth",), "lisa": ("elisabeth",), "sabeth": ("elisabeth",), "lisl": ("elisabeth",),
    "ola": ("aleksandra",), "sasha": ("alexander",), "sasja": ("alexander",), "sander": ("alexander",),
    "alex": ("alexander", "alexandra"), "aleks": ("alexander", "alexandra"), "sacha": ("alexander",),
    "kate": ("katharina",), "katie": ("katharina",), "kathy": ("katharina",), "kaat": ("katharina",),
    "kasia": ("katharina",), "katja": ("katharina",), "kitty": ("katharina",), "cathy": ("katharina",),
    "mike": ("michael",), "mick": ("michael",), "michi": ("michael",), "misha": ("michael",),
    "mischa": ("michael",), "mikey": ("michael",),
    "wim": ("wilhelm",), "willy": ("wilhelm",), "will": ("wilhelm",), "bill": ("wilhelm",),
    "billy": ("wilhelm",), "wil": ("wilhelm",), "willi": ("wilhelm",),
    "fritz": ("friedrich",), "fred": ("friedrich",), "freddy": ("friedrich",), "frits": ("friedrich",),
    "friedel": ("friedrich",),
    "heinz": ("heinrich",), "hein": ("heinrich",), "henk": ("heinrich",), "harry": ("heinrich",),
    "hank": ("heinrich",),
    "jos": ("josef",), "joop": ("josef",), "sepp": ("josef",), "joe": ("josef",), "jupp": ("josef",),
    "pepe": ("josef",), "beppe": ("josef",),
    "pete": ("peter",), "piet": ("peter",), "pietje": ("peter",), "peer": ("peter",),
    "klaus": ("nikolaus",), "claus": ("nikolaus",), "nick": ("nikolaus",), "nico": ("nikolaus",),
    "niels": ("nikolaus",), "nils": ("nikolaus",), "niklas": ("nikolaus",),
    "dora": ("dorothea",), "dolly": ("dorothea",), "thea": ("dorothea", "theodor"),
    "theo": ("theodor",), "ted": ("theodor", "eduard"), "teddy": ("theodor", "eduard"),
    "steve": ("stefan",), "stevie": ("stefan",), "stef": ("stefan",),
    "charlie": ("karl",), "chuck": ("karl",),
    "bob": ("robert",), "rob": ("robert",), "bobby": ("robert",), "robbie": ("robert",),
    "rick": ("richard",), "dick": ("richard",), "rich": ("richard",), "richie": ("richard",),
    "ed": ("eduard",), "eddie": ("eduard",),
    "andy": ("andreas",), "drew": ("andreas",), "dries": ("andreas",), "andi": ("andreas",),
    "jaap": ("jakob",), "kobe": ("jakob",), "koos": ("jakob",), "jake": ("jakob",), "jim": ("jakob",),
    "jimmy": ("jakob",),
    "mia": ("maria",), "mieke": ("maria",), "miep": ("maria",), "mimi": ("maria",), "marijke": ("maria",),
    "molly": ("maria",), "polly": ("maria",),
    "anneke": ("anna",), "annie": ("anna",), "anja": ("anna",), "anita": ("anna",), "ans": ("anna",),
    "annette": ("anna",), "nancy": ("anna",),
    "sjaak": ("jakob",),
    "luki": ("lukasz",), "luke": ("lukasz",),
    "matt": ("matthias",), "thijs": ("matthias",), "matthijs": ("matthias",), "tijs": ("matthias",),
    "seb": ("sebastian",), "basti": ("sebastian",),
    "chris": ("christian", "christina"), "tina": ("christina",), "kiki": ("christina",),
    "dan": ("daniel",), "danny": ("daniel",), "dave": ("david",), "davy": ("david",),
    "gerrit": ("gerhard",), "gert": ("gerhard",), "bernd": ("bernhard",),
    "lutz": ("ludwig",), "lou": ("ludwig",), "wiebke": ("wilhelm",),
    "franzi": ("franz",), "frank": ("franz",), "fran": ("franz",),
    "schorsch": ("georg",), "jurgen": ("georg",),
    "lena": ("helena",), "leni": ("helena",), "hanni": ("johanna", "hannah"), "jo": ("johanna", "josef"),
    "jule": ("julia",), "juli": ("julia",),
}  # fmt: skip

# Exonym / cross-language place groups (same city under different language names).
_EXONYM_GROUPS: tuple[tuple[str, ...], ...] = tuple(
    tuple(g.split("|"))
    for g in (
        "Köln|Cologne|Keulen|Colonia",
        "München|Munich|Muenchen|Monachium",
        "Wien|Vienna|Wenen|Viena",
        "Den Haag|The Hague|La Haye|Den Haag",
        "Praha|Prague|Praag|Prag|Praga",
        "Nürnberg|Nuremberg|Nurnberg|Norimberga",
        "東京|Tokyo|Tokio|Tōkyō",
        "京都|Kyoto",
        "大阪|Osaka",
        "北京|Beijing|Peking",
        "Sevilla|Seville|Sevilha",
        "Lisboa|Lisbon|Lissabon|Lisbonne",
        "Genève|Geneva|Genf|Ginevra|Ginebra|Genua",
        "Firenze|Florence|Florenz|Florencia",
        "Antwerpen|Antwerp|Anvers|Amberes",
        "Roma|Rome|Rom|Rome",
        "Venezia|Venice|Venedig|Venetië|Venecia",
        "Milano|Milan|Mailand|Milaan",
        "Napoli|Naples|Neapel|Napels",
        "Torino|Turin|Turijn",
        "Bruxelles|Brussels|Brussel|Brüssel|Bruselas",
        "Warszawa|Warsaw|Warschau|Varsovie|Varsovia",
        "Kraków|Cracow|Krakau|Cracovie",
        "Gdańsk|Danzig",
        "Wrocław|Breslau",
        "Moskva|Moscow|Moskau|Moskou|Moscou|Moscú",
        "Athína|Athens|Athen|Athene|Atenas|Athènes",
        "København|Copenhagen|Kopenhagen|Kopenhaga",
        "Göteborg|Gothenburg|Goteborg",
        "Brugge|Bruges|Brügge",
        "Gent|Ghent|Gand",
        "Luik|Liège|Lüttich|Liege",
        "Mechelen|Malines",
        "Leuven|Louvain|Löwen",
        "Oostende|Ostend|Ostende",
        "Zürich|Zurich|Zuerich",
        "Basel|Bâle|Basilea",
        "Bern|Berne|Berna",
        "Aachen|Aix-la-Chapelle|Aquisgrán",
        "Kyiv|Kiev|Kiew|Kyjiw",
        "Sankt Petersburg|Saint Petersburg|St Petersburg|Sint-Petersburg|Saint-Pétersbourg",
        "București|Bucharest|Bukarest|Bucarest",
        "Beograd|Belgrade|Belgrad",
        "Lyon|Lyons",
        "Marseille|Marseilles",
        "Köln am Rhein|Cologne on the Rhine",
        "Zagreb|Agram",
        "Ljubljana|Laibach",
        "Bratislava|Pressburg|Pozsony",
        "Tallinn|Reval",
        "Helsinki|Helsingfors",
        "Turku|Åbo",
        "Rīga|Riga",
        "Vilnius|Wilno|Vilna",
        "İstanbul|Istanbul|Constantinople",
    )
)

# Designator words: class label. Weak classes are ignorable when only one side states them.
_DESIGNATORS: dict[str, str] = {}
for _cls, _words in {
    "unit": "flat apartment apt wohnung appartement appartment unit suite",
    "room": "room raum zimmer kamer",
    "building": "building gebaeude bldg gebouw",
    "floor": "floor etage stock verdieping",
    "gate": "gate",
    "tram": "tram strassenbahn",
    "bus": "bus",
    "metro": "metro ubahn",
    "train": "train zug",
    "number": "nr no number nummer hausnummer num",
    "line": "line linie lijn",
    "version": "version v ver release rel rev",
    "model": "model modell",
}.items():
    for _w in _words.split():
        _DESIGNATORS[_w] = _cls
_WEAK = frozenset({"number", "line", "version", "model"})
_ORDINAL_SUFFIX = frozenset({"st", "nd", "rd", "th"})

_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "who",
        "that",
        "which",
        "near",
        "this",
        "these",
        "those",
        "was",
        "were",
        "are",
        "has",
        "have",
        "had",
        "his",
        "her",
        "its",
        "their",
        "our",
        "your",
        "who",
        "whom",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "around",
        "there",
        "then",
        "than",
        "also",
        "very",
        "one",
        "two",
        "works",
        "work",
        "worked",
        "lives",
        "lived",
        "live",
        "located",
        "known",
        "called",
        "named",
        "name",
        "city",
        "town",
        "capital",
        "small",
        "large",
        "district",
        "village",
        "area",
        "place",
        "type",
        "een",
        "het",
        "van",
        "voor",
        "die",
        "dat",
        "bij",
        "met",
        "aan",
        "als",
        "dat",
        "niet",
        "zijn",
        "haar",
        "onder",
        "naar",
        "der",
        "die",
        "das",
        "den",
        "dem",
        "des",
        "und",
        "mit",
        "von",
        "auf",
        "aus",
        "ein",
        "eine",
        "einer",
        "eines",
        "bei",
        "zum",
        "zur",
        "dem",
        "sich",
        "nicht",
        "uma",
        "uns",
        "dos",
        "das",
        "com",
        "para",
        "por",
        "que",
        "los",
        "las",
        "del",
        "con",
        "por",
        "una",
        "uno",
        "que",
        "les",
        "des",
        "aux",
        "dans",
        "pour",
        "avec",
        "que",
        "qui",
    ]
)

_LEXICON_BUILT = False
_GROUP_OF: dict[str, set[int]] = {}
_NICK_OF: dict[str, set[int]] = {}
_EXONYM_OF: dict[str, int] = {}


def _build_lexicons() -> None:
    global _LEXICON_BUILT
    if _LEXICON_BUILT:
        return
    for i, group in enumerate(_COGNATES):
        for name in group:
            for v in _variants(name):
                _GROUP_OF.setdefault(v, set()).add(i)
    for nick, formals in _NICKS.items():
        for formal in formals:
            for i, group in enumerate(_COGNATES):
                if formal in group:
                    for v in _variants(nick):
                        _NICK_OF.setdefault(v, set()).add(i)
    for gi, names in enumerate(_EXONYM_GROUPS):
        for name in names:
            p = _prepare(name)
            _EXONYM_OF.setdefault(p.primary, gi)
            _EXONYM_OF.setdefault(" ".join(p.stripped), gi)
    _LEXICON_BUILT = True


def _given_match(a_vars: frozenset[str], b_vars: frozenset[str]) -> str | None:
    """Return ``variant``, ``nickname`` or ``None`` for two given-name tokens (never nick vs nick)."""
    if a_vars & b_vars:
        return "variant"
    a_formal = {g for v in a_vars for g in _GROUP_OF.get(v, ())}
    b_formal = {g for v in b_vars for g in _GROUP_OF.get(v, ())}
    if a_formal & b_formal:
        return "variant"
    a_nick = {g for v in a_vars for g in _NICK_OF.get(v, ())}
    b_nick = {g for v in b_vars for g in _NICK_OF.get(v, ())}
    if (a_nick & b_formal) or (b_nick & a_formal):
        return "nickname"
    return None


# --------------------------------------------------------------------------------------------------
# Public data types
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MentionView:
    """A mention plus the evidence the rules may use. ``type`` and ``lang`` may be empty."""

    surface: str
    type: str = ""
    context: str = ""
    lang: str | None = None
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class PairContext:
    """Pair-level evidence: explicit disambiguators (surname, role, city, age, ...) per side."""

    same_conversation: bool = False
    a_attrs: Mapping[str, str] = field(default_factory=dict)
    b_attrs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PairDecision:
    verdict: Verdict
    reason: str
    score: float | None = None


def _coerce(m: MentionView | Mention | str) -> MentionView:
    if isinstance(m, MentionView):
        return m
    if isinstance(m, Mention):
        return MentionView(surface=m.surface)
    return MentionView(surface=m)


# --------------------------------------------------------------------------------------------------
# Context evidence
# --------------------------------------------------------------------------------------------------

_WORD = re.compile(r"[^\W\d_][\w’']*|\d+(?:\.\d+)*", re.UNICODE)
_SENT_END = re.compile(r"[.!?:;]\s*$")


def _salient(context: str, exclude: set[str]) -> set[str]:
    out: set[str] = set()
    for w in _WORD.findall(context):
        if w[0].isdigit():
            continue
        t = _tokens(w.split("'")[0].split("’")[0], _STRIPPED)
        if not t:
            continue
        tok = t[0]
        if len(tok) < 3 or tok in _STOP:
            continue
        stem = tok[:5]
        if stem in exclude:
            continue
        out.add(stem)
    return out


def _capitalised(context: str, exclude: set[str]) -> set[str]:
    """Capitalised words not at the start of a sentence (places, names): the strongest anchors."""
    out: set[str] = set()
    first = True
    for m in _WORD.finditer(context):
        w = m.group(0)
        sentence_start = first or bool(_SENT_END.search(context[: m.start()]))
        first = False
        if sentence_start or not w[0].isupper():
            continue
        t = _tokens(w.split("'")[0].split("’")[0], _STRIPPED)
        if t and t[0][:5] not in exclude and t[0] not in _STOP:
            out.add(t[0][:5])
    return out


def _numbers_in(context: str) -> set[tuple[int, ...]]:
    return {_canon_number(x) for x in re.findall(r"\d+(?:\.\d+)*", context)}


def _context_signal(a: MentionView, b: MentionView, pa: _Prepared, pb: _Prepared) -> str:
    """``none`` (a side is empty), ``conflict``, ``agree`` or ``unknown``."""
    if not a.context.strip() or not b.context.strip():
        return "none"
    exclude = {t[:5] for t in (*pa.stripped, *pb.stripped)}
    caps_a, caps_b = _capitalised(a.context, exclude), _capitalised(b.context, exclude)
    nums_a, nums_b = _numbers_in(a.context), _numbers_in(b.context)
    sal_a, sal_b = _salient(a.context, exclude), _salient(b.context, exclude)
    if caps_a and caps_b and not caps_a & caps_b:
        return "conflict"
    if nums_a and nums_b and not nums_a & nums_b:
        return "conflict"
    same_lang = bool(a.lang) and a.lang == b.lang
    if sal_a and sal_b and not sal_a & sal_b and same_lang:
        return "conflict"
    if sal_a & sal_b or caps_a & caps_b or nums_a & nums_b:
        return "agree"
    return "unknown"


def _attrs_conflict(ctx: PairContext | None) -> str | None:
    if ctx is None:
        return None
    for key, va in ctx.a_attrs.items():
        vb = ctx.b_attrs.get(key)
        if vb is not None and normalise_name(str(va)) != normalise_name(str(vb)):
            return key
    return None


# --------------------------------------------------------------------------------------------------
# Pair decision
# --------------------------------------------------------------------------------------------------


def _designator_signature(p: _Prepared) -> tuple[frozenset[str], frozenset[str]]:
    classes: set[str] = set()
    others: set[str] = set()
    for w in p.words:
        if w in _ORDINAL_SUFFIX:
            continue
        cls = _DESIGNATORS.get(w)
        if cls:
            classes.add(cls)
        else:
            others.add(w)
    return frozenset(classes), frozenset(others)


def _gated_same(
    reason: str,
    a: MentionView,
    b: MentionView,
    pa: _Prepared,
    pb: _Prepared,
    ctx: PairContext | None,
) -> PairDecision:
    """A non-exact ``same`` claim survives only if the contexts do not contradict it."""
    if _attrs_conflict(ctx) is not None or _context_signal(a, b, pa, pb) == "conflict":
        return PairDecision("pending_merge", f"{reason}_context_conflict")
    return PairDecision("same", reason)


def _person_alias(pa: _Prepared, pb: _Prepared) -> tuple[str, str] | None:
    """Return ``(verdict, reason)`` from given-name and surname rules, or ``None`` if not applicable."""

    def split(p: _Prepared) -> tuple[list[int], list[int]]:
        n = len(p.tokens)
        if n == 1:
            return [0], []
        cut = n - 1
        for i in range(1, n):
            if p.tokens[i] in _PARTICLES:
                cut = i
                break
        return list(range(cut)), list(range(cut, n))

    ga, sa = split(pa)
    gb, sb = split(pb)
    if bool(sa) != bool(sb):
        # one side is a bare given name: only a hint, never an auto-merge
        bare, full, gf = (pa, pb, gb) if not sa else (pb, pa, ga)
        if len(gf) >= 1 and _given_match(bare.tokens_v[0], full.tokens_v[gf[0]]):
            return ("pending_merge", "partial_name")
        return None
    if not sa:
        m = _given_match(pa.tokens_v[0], pb.tokens_v[0])
        if m == "variant":
            return ("same", "alias_variant")
        if m == "nickname":
            return ("pending_merge", "unqualified_nickname")
        return None
    if len(sa) != len(sb) or not all(
        pa.tokens_v[i] & pb.tokens_v[j] for i, j in zip(sa, sb, strict=True)
    ):
        return ("different", "surname_mismatch")
    reasons: list[str] = []
    if len(ga) != len(gb):
        lead = _given_pair(pa, ga[0], pb, gb[0])
        return ("pending_merge", "partial_name") if lead else ("different", "given_mismatch")
    for i, j in zip(ga, gb, strict=True):
        r = _given_pair(pa, i, pb, j)
        if r is None:
            return ("different", "given_mismatch")
        reasons.append(r)
    if all(r == "alias_variant" for r in reasons):
        return ("same", "alias_variant")
    if "alias_initials" in reasons:
        return ("same", "alias_initials")
    return ("same", "alias_nickname")


def _given_pair(pa: _Prepared, i: int, pb: _Prepared, j: int) -> str | None:
    ta, tb = pa.stripped[i], pb.stripped[j]
    ia, ib = len(ta) == 1, len(tb) == 1
    if ia and ib:
        return "alias_variant" if ta == tb else None
    if ia or ib:
        initial, full = (ta, tb) if ia else (tb, ta)
        return "alias_initials" if full.startswith(initial) else None
    m = _given_match(pa.tokens_v[i], pb.tokens_v[j])
    if m == "variant":
        return "alias_variant"
    if m == "nickname":
        return "alias_nickname"
    return None


def resolve_pair(
    a: MentionView | Mention | str,
    b: MentionView | Mention | str,
    context: PairContext | None = None,
) -> PairDecision:
    """Decide ``same`` / ``different`` / ``pending_merge`` for two mentions, with a reason code."""
    _build_lexicons()
    ma, mb = _coerce(a), _coerce(b)
    pa, pb = _prepare(ma.surface), _prepare(mb.surface)
    if not pa.primary or not pb.primary:
        return PairDecision("different", "empty_name")
    ta, tb = ma.type.casefold().strip(), mb.type.casefold().strip()
    if ta and tb and ta != tb:
        return PairDecision("different", "type_mismatch")

    # numeric tokens must match exactly
    if pa.numbers or pb.numbers:
        if pa.numbers != pb.numbers:
            return PairDecision("different", "numeric_mismatch")
        ca, oa = _designator_signature(pa)
        cb, ob = _designator_signature(pb)
        if oa != ob or (ca - _WEAK) != (cb - _WEAK):
            return PairDecision("different", "designator_mismatch")
        if pa.primary == pb.primary:
            return _exact(ma, mb, pa, pb, context)
        return _gated_same("numeric_match", ma, mb, pa, pb, context)

    if pa.primary == pb.primary:
        return _exact(ma, mb, pa, pb, context)

    # cross-language exonyms
    ea = _EXONYM_OF.get(pa.primary, _EXONYM_OF.get(" ".join(pa.stripped)))
    eb = _EXONYM_OF.get(pb.primary, _EXONYM_OF.get(" ".join(pb.stripped)))
    if ea is not None and ea == eb:
        return _gated_same("exonym", ma, mb, pa, pb, context)
    if ea is not None and eb is not None:
        return PairDecision("different", "exonym_other_place")

    # spelling variants of the whole name (Soren / Søren, Ayse / Ayşe)
    if len(pa.tokens) == len(pb.tokens) and all(
        x & y for x, y in zip(pa.tokens_v, pb.tokens_v, strict=True)
    ):
        return _gated_same("alias_variant", ma, mb, pa, pb, context)

    if (not ta or ta == "person") and (not tb or tb == "person"):
        alias = _person_alias(pa, pb)
        if alias is not None:
            verdict, reason = alias
            if verdict == "same":
                return _gated_same(reason, ma, mb, pa, pb, context)
            if verdict == "different":
                return PairDecision("different", reason)
            return PairDecision("pending_merge", reason)

    score = difflib.SequenceMatcher(None, pa.primary, pb.primary).ratio()
    if score >= FUZZY_PENDING_THRESHOLD:
        return PairDecision("pending_merge", "fuzzy", round(score, 3))
    return PairDecision("different", "no_match", round(score, 3))


def _exact(
    ma: MentionView, mb: MentionView, pa: _Prepared, pb: _Prepared, ctx: PairContext | None
) -> PairDecision:
    """Same normalised name: ``same`` unless a homonym signal is present."""
    key = _attrs_conflict(ctx)
    if key is not None:
        return PairDecision("different", f"explicit_disambiguator_{key}")
    if (
        ctx is not None
        and ctx.same_conversation
        and ma.speaker is not None
        and mb.speaker is not None
        and ma.speaker != mb.speaker
    ):
        return PairDecision("pending_merge", "homonym_speakers")
    sig = _context_signal(ma, mb, pa, pb)
    if sig == "conflict":
        return PairDecision("different", "homonym_context")
    if sig == "unknown":
        return PairDecision("pending_merge", "homonym_unresolved")
    return PairDecision("same", "exact_match")


# --------------------------------------------------------------------------------------------------
# Online resolver
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """An existing entity offered to the resolver. ``scope`` must equal the resolve scope to count."""

    entity_id: str
    name: str
    scope: OwnerScope
    type: str = ""
    context: str = ""
    aliases: tuple[str, ...] = ()
    lang: str | None = None


@dataclass(frozen=True, slots=True)
class EntityDecision:
    """``existing``: use ``entity_id``. ``new``: ``entity_id`` is a fresh deterministic id.

    ``pending_merge``: ``entity_id`` is a provisional id (I-5) and ``candidates`` lists the entities it
    might merge with; the caller keeps the mention separate until a later pass decides.
    """

    kind: Literal["existing", "new", "pending_merge"]
    entity_id: str
    candidates: tuple[str, ...] = ()
    reason: str = ""


def _same_scope(a: OwnerScope, b: OwnerScope) -> bool:
    return a.tenant == b.tenant and a.owner == b.owner


def entity_id_for(scope: OwnerScope, type_: str, primary: str, ordinal: int = 0) -> str:
    """Deterministic entity id under tenant + owner (never the session)."""
    blob = "\x1f".join([scope.tenant, scope.owner, type_.casefold().strip(), primary, str(ordinal)])
    return "ent_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class OnlineResolver:
    """Resolve one mention at a time against candidates, with a bounded LRU cache of final decisions.

    The cache key is (tenant, owner, type, normalised name, context signature); a hit is honoured only
    while its entity is still among the in-scope candidates, so the cache never changes a result.
    """

    def __init__(self, max_cache: int = 1024) -> None:
        if max_cache < 1:
            raise ValueError("max_cache must be at least 1")
        self._max = max_cache
        self._cache: OrderedDict[tuple[str, ...], str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._cache)

    def resolve(
        self,
        mention: MentionView | Mention | str,
        scope: OwnerScope,
        candidates: Iterable[Candidate] = (),
        pair_context: PairContext | None = None,
    ) -> EntityDecision:
        _build_lexicons()
        m = _coerce(mention)
        p = _prepare(m.surface)
        in_scope = [c for c in candidates if _same_scope(c.scope, scope)]
        by_id = {c.entity_id: c for c in in_scope}
        sig = hashlib.sha256(
            "|".join(sorted(_salient(m.context, {t[:5] for t in p.stripped}))).encode("utf-8")
        ).hexdigest()[:12]
        key = (scope.tenant, scope.owner, m.type.casefold().strip(), p.primary, sig)
        hit = self._cache.get(key)
        if hit is not None and hit in by_id:
            self._cache.move_to_end(key)
            return EntityDecision("existing", hit, (hit,), "cache")

        same: dict[str, str] = {}
        pending: dict[str, str] = {}
        for c in in_scope:
            best: PairDecision | None = None
            for name in (c.name, *c.aliases):
                view = MentionView(name, c.type, c.context, c.lang)
                d = resolve_pair(m, view, pair_context)
                if d.verdict == "same":
                    best = d
                    break
                if d.verdict == "pending_merge" and best is None:
                    best = d
            if best is None:
                continue
            (same if best.verdict == "same" else pending)[c.entity_id] = best.reason

        ordinal = sum(1 for c in in_scope if _prepare(c.name).primary == p.primary)
        fresh = entity_id_for(scope, m.type, p.primary, ordinal)
        if len(same) == 1 and not pending:
            ((eid, reason),) = same.items()
            self._remember(key, eid)
            return EntityDecision("existing", eid, (eid,), reason)
        if same or pending:
            ids = tuple(sorted({*same, *pending}))
            reason = "ambiguous_same" if len(same) > 1 else next(iter({**same, **pending}.values()))
            if len(same) == 1:
                reason = "same_with_pending"
            return EntityDecision("pending_merge", fresh, ids, reason)
        self._remember(key, fresh)
        return EntityDecision("new", fresh, (), "no_candidate")

    def _remember(self, key: tuple[str, ...], entity_id: str) -> None:
        self._cache[key] = entity_id
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
