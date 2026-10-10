"""The privacy filter between a private question and a public search.

The only text that may leave the process is a short list of generic topic words taken from
the question itself. Everything that could identify a person, a place or a conversation is
removed first, and a final guard refuses to return a query that still contains a private
term. The filter is deliberately conservative: it drops a public name rather than risk a
private one, because a weaker query costs a worse snippet and a leaked name cannot be undone.

Removed: URLs, e-mail addresses, @handles, quoted spans, any token with a digit, every
capitalised word (names, places, titles), every term in ``private_terms`` (the store's
entity vocabulary, the speakers, the capitalised words of the retrieved private context),
relationship words, pronouns, question-frame words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "FRAME_WORDS",
    "PrivacyLeakError",
    "PublicQuery",
    "assert_public",
    "build_public_query",
    "private_terms_from",
]

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_HANDLE = re.compile(r"(?<!\w)[@#]\w+")
_HAS_DIGIT = re.compile(r"\S*\d\S*")
_QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|(?<!\w)'[^']{2,}'(?!\w)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*")
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z'\u2019-]*")

#: Words of the question's frame (the invitation to infer), never of its topic.
FRAME_WORDS = frozenset(
    [
        "would",
        "likely",
        "might",
        "could",
        "should",
        "probably",
        "possibly",
        "perhaps",
        "maybe",
        "recommend",
        "recommended",
        "recommendation",
        "suggest",
        "suggestion",
        "suggestions",
        "enjoy",
        "enjoys",
        "like",
        "likes",
        "love",
        "loves",
        "want",
        "wants",
        "interested",
        "interest",
        "consider",
        "appreciate",
        "prefer",
        "prefers",
        "think",
        "thinks",
        "ever",
        "again",
        "still",
        "much",
        "many",
        "some",
        "any",
        "idea",
        "ideas",
        "good",
        "great",
        "nice",
        "best",
        "better",
        "type",
        "kind",
        "sort",
        "something",
        "anything",
        "someone",
        "anyone",
        "thing",
        "things",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "does",
        "did",
        "do",
        "doing",
        "done",
        "have",
        "has",
        "had",
        "having",
        "been",
        "being",
        "were",
        "was",
        "are",
        "is",
        "am",
        "can",
        "cannot",
        "will",
        "shall",
        "not",
        "never",
        "also",
        "just",
        "very",
        "really",
        "quite",
    ]
)
#: Words that name a private relationship or a private role.
_RELATIONSHIP = frozenset(
    [
        "mom",
        "mum",
        "mother",
        "dad",
        "father",
        "wife",
        "husband",
        "partner",
        "spouse",
        "fiance",
        "fiancee",
        "boyfriend",
        "girlfriend",
        "son",
        "daughter",
        "kid",
        "kids",
        "child",
        "children",
        "baby",
        "brother",
        "sister",
        "sibling",
        "grandma",
        "grandpa",
        "grandmother",
        "grandfather",
        "aunt",
        "uncle",
        "cousin",
        "niece",
        "nephew",
        "friend",
        "friends",
        "buddy",
        "boss",
        "colleague",
        "coworker",
        "neighbour",
        "neighbor",
        "roommate",
        "family",
    ]
)
_FUNCTION = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "as",
        "than",
        "then",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "he",
        "she",
        "they",
        "them",
        "their",
        "his",
        "her",
        "him",
        "hers",
        "theirs",
        "i",
        "me",
        "my",
        "mine",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "so",
        "such",
        "no",
        "yes",
        "up",
        "down",
        "out",
        "off",
        "there",
        "here",
        "be",
        "to",
        "me",
    ]
)
_MIN_LEN = 3


class PrivacyLeakError(ValueError):
    """The built query still contains a private term. Never caught inside the engine."""


@dataclass(frozen=True, slots=True)
class PublicQuery:
    text: str
    terms: tuple[str, ...]


def _norm(token: str) -> str:
    t = token.lower().replace("\u2019", "'")
    for suffix in ("'s", "'"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t


def _variants(term: str) -> set[str]:
    t = _norm(term)
    out = {t}
    if t.endswith("s") and len(t) > 3:
        out.add(t[:-1])
    else:
        out.add(t + "s")
    return out


def private_terms_from(texts: Iterable[str], names: Iterable[str] = ()) -> frozenset[str]:
    """Private terms to keep out of a query: every capitalised word of ``texts`` (a retrieved
    private context: names, places, titles) and every name in ``names`` (speakers, the
    store's entity vocabulary), each as lowercase words."""
    found: set[str] = set()
    for text in texts:
        for token in _CAPITALISED.findall(text):
            found.update(_variants(token))
    for name in names:
        for part in re.split(r"[\s,_/]+", name):
            if part:
                found.update(_variants(part))
    return frozenset(w for w in found if w)


def build_public_query(
    question: str,
    private_terms: Iterable[str] = (),
    *,
    max_terms: int = 6,
) -> PublicQuery | None:
    """A generic public query from ``question``, or None when no safe topic word is left.

    ``private_terms`` are lowercase terms to drop wherever they occur (see
    :func:`private_terms_from`). The result is at most ``max_terms`` distinct lowercase words
    in question order."""
    private = {p for term in private_terms for p in _variants(term)}
    text = _QUOTED.sub(" ", question)
    text = _URL.sub(" ", text)
    text = _EMAIL.sub(" ", text)
    text = _HANDLE.sub(" ", text)
    text = _HAS_DIGIT.sub(" ", text)
    terms: list[str] = []
    for match in _WORD.finditer(text):
        token = match.group(0)
        norm = _norm(token)
        if token[0].isupper() and norm not in FRAME_WORDS and norm not in _FUNCTION:
            continue  # a capitalised word that is not a frame word: a name, place or title
        if (
            len(norm) < _MIN_LEN
            or norm in FRAME_WORDS
            or norm in _FUNCTION
            or norm in _RELATIONSHIP
            or norm in private
            or "'" in norm
            or any(v in private for v in _variants(norm))
            or norm in terms
        ):
            continue
        terms.append(norm)
        if len(terms) >= max_terms:
            break
    if not terms:
        return None
    query = PublicQuery(text=" ".join(terms), terms=tuple(terms))
    assert_public(query.text, private)
    return query


def assert_public(query: str, private_terms: Iterable[str]) -> None:
    """Raise :class:`PrivacyLeakError` when ``query`` contains any private term as a word."""
    private = {p for term in private_terms for p in _variants(term)}
    words = {_norm(w) for w in _WORD.findall(query)}
    leaked = sorted(w for w in words if w in private or any(v in private for v in _variants(w)))
    if leaked:
        raise PrivacyLeakError(f"private term(s) in public query: {leaked}")
