"""R3-04 (R-06): the language-aware lexical leg: a Tokeniser port and a swappable scorer port.

Two ports, so the lexical signal can be changed by config without touching the leg:

* :class:`Tokeniser` turns text into terms. :class:`UnicodeTokeniser` is NFKC + casefold, then
  words on Unicode word characters; per language it drops stop words and stems (Snowball, through
  the tantivy analyser that is already a core dependency); CJK runs (Han, kana, Hangul) fall back
  to character bigrams (a one-character run stays a unigram); literal codes (``AB-123``,
  ``v2.4.1``, anything with a digit) are kept whole and are never stemmed.
* :class:`LexicalScorer` ranks documents for query terms. :class:`Bm25Scorer` (Okapi BM25) is the
  only scorer today; ``read.lexical_scorer`` names it, and :func:`register_scorer` is the seam for
  another one (for example a word-vector scorer) with no change to the leg.

No I/O. Documents are tokenised once per index build; the index is cached by content so a read
over an unchanged namespace does not re-tokenise.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from memspine.read.legs.core import Hit, LegContext, LegOutput
from memspine.store.records import MemoryRecord, chrono_key

__all__ = [
    "LANGUAGES",
    "SCORERS",
    "Bm25Scorer",
    "LexicalLeg",
    "LexicalScorer",
    "Tokeniser",
    "UnicodeTokeniser",
    "make_scorer",
    "make_tokeniser",
    "register_scorer",
]

#: Han, Hiragana, Katakana, Hangul and compatibility ideographs: scripts without spaces between
#: words, indexed as character bigrams.
_CJK = "぀-ヿ㐀-䶿一-鿿가-힯豈-﫿"
_TOKEN = re.compile(rf"(?P<cjk>[{_CJK}]+)|(?P<word>[^\W_{_CJK}]+(?:[-_.][^\W_{_CJK}]+)*)")
_SPLIT = re.compile(r"[-_.]")

#: ISO 639-1 codes to the Snowball languages the tantivy analyser knows.
LANGUAGES: dict[str, str] = {
    "ar": "arabic",
    "da": "danish",
    "nl": "dutch",
    "en": "english",
    "fi": "finnish",
    "fr": "french",
    "de": "german",
    "el": "greek",
    "hu": "hungarian",
    "it": "italian",
    "no": "norwegian",
    "nb": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "es": "spanish",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
}


@runtime_checkable
class Tokeniser(Protocol):
    """Port: text to index terms. Index and query must use the same instance."""

    name: str

    def tokens(self, text: str) -> list[str]: ...


@lru_cache(maxsize=64)
def _analyzer(language: str) -> Any | None:
    try:
        import tantivy

        return (
            tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
            .filter(tantivy.Filter.lowercase())
            .filter(tantivy.Filter.stopword(language))
            .filter(tantivy.Filter.stemmer(language))
            .build()
        )
    except Exception:  # tantivy missing or language unsupported: no stemming, never an error
        return None


@lru_cache(maxsize=200_000)
def _stem(language: str, word: str) -> tuple[str, ...]:
    analyzer = _analyzer(language)
    if analyzer is None:
        return (word,)
    return tuple(analyzer.analyze(word))


def _bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


class UnicodeTokeniser:
    """NFKC + casefold; per-language stop words and stemming; CJK character bigrams.

    ``language`` is a code from :data:`LANGUAGES` (``"de"``) or a Snowball name (``"french"``);
    any other value tokenises without stop words or stemming. ``stem=False`` keeps stop words
    and surface forms for every language.
    """

    def __init__(self, language: str = "en", *, stem: bool = True) -> None:
        lang = language.strip().lower()
        self.language = LANGUAGES.get(lang, lang if lang in LANGUAGES.values() else "")
        self.stem = stem and bool(self.language)
        self.name = f"unicode:{self.language or 'plain'}{'' if self.stem else ':nostem'}"

    def tokens(self, text: str) -> list[str]:
        out: list[str] = []
        folded = unicodedata.normalize("NFKC", text).casefold()
        for m in _TOKEN.finditer(folded):
            if m.group("cjk"):
                out.extend(_bigrams(m.group("cjk")))
                continue
            chunk = m.group("word")
            if any(ch.isdigit() for ch in chunk):
                # a literal code (AB-123, v2.4.1, 2023-05-07): kept whole and as its pieces
                out.append(chunk)
                parts = [p for p in _SPLIT.split(chunk) if p]
                if len(parts) > 1:
                    out.extend(parts)
                continue
            for word in _SPLIT.split(chunk):
                if word:
                    out.extend(_stem(self.language, word) if self.stem else (word,))
        return out


def make_tokeniser(language: str = "en") -> Tokeniser:
    """The tokeniser of one namespace language (see :class:`UnicodeTokeniser`)."""
    return UnicodeTokeniser(language)


@runtime_checkable
class LexicalScorer(Protocol):
    """Port: rank documents for query terms. ``fit`` once per index, ``search`` per query."""

    name: str

    def fit(self, docs: Mapping[str, Sequence[str]]) -> None: ...

    def search(self, terms: Sequence[str], k: int) -> list[tuple[str, float]]: ...


class Bm25Scorer:
    """Okapi BM25 (``k1`` 1.2, ``b`` 0.75; the non-negative Lucene IDF)."""

    name = "bm25"

    def __init__(self, k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self._tf: dict[str, Counter[str]] = {}
        self._len: dict[str, int] = {}
        self._avg = 1.0
        self._postings: dict[str, list[str]] = {}

    def fit(self, docs: Mapping[str, Sequence[str]]) -> None:
        self._tf = {rid: Counter(toks) for rid, toks in docs.items()}
        self._len = {rid: len(toks) for rid, toks in docs.items()}
        self._postings = {}
        for rid, tf in self._tf.items():
            for term in tf:
                self._postings.setdefault(term, []).append(rid)
        total = sum(self._len.values())
        self._avg = total / len(self._len) if self._len and total else 1.0

    def search(self, terms: Sequence[str], k: int) -> list[tuple[str, float]]:
        n = len(self._tf)
        scores: dict[str, float] = {}
        for term in dict.fromkeys(terms):
            posting = self._postings.get(term)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for rid in posting:
                f = self._tf[rid][term]
                norm = self.k1 * (1.0 - self.b + self.b * self._len[rid] / self._avg)
                scores[rid] = scores.get(rid, 0.0) + idf * f * (self.k1 + 1.0) / (f + norm)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]


#: ``read.lexical_scorer`` value -> scorer factory. Add a scorer with :func:`register_scorer`.
SCORERS: dict[str, Callable[[], LexicalScorer]] = {"bm25": Bm25Scorer}


def register_scorer(name: str, factory: Callable[[], LexicalScorer]) -> None:
    SCORERS[name] = factory


def make_scorer(name: str) -> LexicalScorer:
    try:
        return SCORERS[name]()
    except KeyError:
        raise ValueError(f"unknown lexical scorer {name!r} (known: {', '.join(SCORERS)})") from None


def _doc_text(record: MemoryRecord, dates: bool) -> str:
    """The indexed text of a record; ``read.lexical_dates`` (N30) adds its date words."""
    if not dates:
        return record.content
    from memspine.models.encoders.query.temporal_query import date_words

    return f"{record.content} {date_words(record)}"


@dataclass
class _Index:
    scorer: LexicalScorer
    records: dict[str, MemoryRecord]


class LexicalLeg:
    """The lexical leg: tokenise, score, rank. Family ``lexical``, name ``lexical``.

    The index is built from the live records handed in (the layers this leg covers), tokenised
    with the namespace language's tokeniser and fitted by the configured scorer. It is cached per
    (namespace, tokeniser, scorer, record fingerprints), so only a changed namespace refits.
    """

    family = "lexical"

    def __init__(self, layers: tuple[str, ...] = ("L0", "L1"), name: str = "lexical") -> None:
        self.layers = layers
        self.name = name
        self._cache: OrderedDict[tuple[Any, ...], _Index] = OrderedDict()

    def _index(
        self, ctx: LegContext, tokeniser: Tokeniser, records: Sequence[MemoryRecord]
    ) -> _Index:
        key = (
            ctx.namespace,
            tokeniser.name,
            ctx.lexical_scorer,
            ctx.lexical_dates,
            tuple((r.record_id, r.content_fingerprint) for r in records),
        )
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit
        scorer = make_scorer(ctx.lexical_scorer)
        docs = {r.record_id: tokeniser.tokens(_doc_text(r, ctx.lexical_dates)) for r in records}
        scorer.fit(docs)
        built = _Index(scorer, {r.record_id: r for r in records})
        self._cache[key] = built
        while len(self._cache) > 8:
            self._cache.popitem(last=False)
        return built

    async def run(self, ctx: LegContext) -> LegOutput:
        records = ctx.records_for(self.layers)
        if not records:
            return LegOutput([], f"no_records:{'+'.join(self.layers)}")
        tokeniser = ctx.tokeniser_for()
        terms = tokeniser.tokens(ctx.lexical_query or ctx.query)
        if not terms:
            return LegOutput([], "no_query_terms")
        index = self._index(ctx, tokeniser, records)
        ranked = index.scorer.search(terms, max(ctx.fetch_k * 4, ctx.fetch_k))
        # equal scores settle in chronological order (the engine's tie rule), then cut
        by = index.records
        ranked.sort(key=lambda kv: (-round(kv[1], 12), chrono_key(by[kv[0]])))
        hits = [Hit(rid, score) for rid, score in ranked[: ctx.fetch_k]]
        return LegOutput(hits, None if hits else "no_match")
