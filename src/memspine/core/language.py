"""I23: a cheap language guard for the English-only regex features.

``query_shape`` (question shapes, set nouns), ``temporal_query`` (month names, "in <month>")
and ``temporal_resolve`` ("last week", "two days ago") match English words. On a question or
a turn in another language they can misfire (a French "mars" or a German "Mai", "a" / "may"
inside a Spanish sentence) or, worse, trigger a wrong mode. With ``read.language_guard: on``
these features FAIL CLOSED on text this module judges non-English: the predicate says "no",
the resolver finds nothing. Off (default): nothing here runs.

The check is deliberately cheap and conservative, no model and no dependency:

* a text whose letters are mostly outside the Latin script (CJK, Cyrillic, Arabic,
  Devanagari, ...) is non-English;
* a Latin-script text is non-English when it holds at least two stopwords of French, German,
  Spanish, Italian, Portuguese or Dutch and more of them than English stopwords (or one such
  stopword, an accented letter and no English stopword);
* anything else, including very short or name-only text, counts as English, so the guard
  only ever removes a trigger when it is fairly sure the text is not English.

The guard is a context variable set by the engine around ``read`` / ``assemble``
(:func:`language_scope`); it does not cover write-time mining.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

__all__ = ["blocked", "english_only", "language_scope", "looks_english"]


def _vocab(text: str) -> frozenset[str]:
    return frozenset(text.split())


_EN = _vocab(
    "the of is are was were what when where who which how did does do has have had an and or "
    "in on at to for with from by about that this it he she they his her their i you we my "
    "your not be been will would can could there than then also if but as so just ago last "
    "next"
)
_FOREIGN = _vocab(
    # French
    "le la les des du une est sont et ou je tu il elle nous vous ils elles ce cette ces "
    "dans pour avec sur pas qui quand combien comment pourquoi où quel quelle quels quelles "
    "mon ma mes ses au aux ont avait été dernier dernière "
    # German
    "der das ein eine einer und ist sind nicht mit von zu auf für wann wie wo warum welche "
    "welcher welches hat haben wurde wurden ich du er wir ihr dem im zum zur nach bei "
    "auch oder letzten letzte "
    # Spanish
    "el los las una unos unas es son y que cuándo cuando cuántos cuántas cómo por qué dónde "
    "cuál con para del al su sus mi mis fue fueron han pasado "
    # Italian
    "il lo gli è sono che quanti quante perché dove quale della dei delle nel "
    "nella hanno "
    # Portuguese
    "os um uma não são quanto porque onde qual do da dos das no na foi "
    # Dutch
    "het een zijn en of dat wanneer hoeveel waarom waar welke voor van op niet"
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
#: Latin-script letters with diacritics that English text almost never has.
_ACCENTED = re.compile(r"[àâäçèéêëîïôöùûüÿñßãõìòáíóúœæ]", re.I)
_NON_LATIN_SHARE = 0.3


def _is_latin(ch: str) -> bool:
    try:
        return "LATIN" in unicodedata.name(ch)
    except ValueError:
        return False


def looks_english(text: str) -> bool:
    """False only when ``text`` is fairly clearly not English (see the module docstring)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    if sum(1 for c in letters if not _is_latin(c)) / len(letters) > _NON_LATIN_SHARE:
        return False
    words = [w.lower() for w in _WORD.findall(text)]
    english = sum(1 for w in words if w in _EN)
    foreign = sum(1 for w in words if w in _FOREIGN and w not in _EN)
    if foreign >= 2 and foreign > english:
        return False
    return not (foreign >= 1 and english == 0 and _ACCENTED.search(text) is not None)


_GUARD: ContextVar[bool] = ContextVar("memspine_language_guard", default=False)


@contextmanager
def language_scope(on: bool) -> Iterator[None]:
    """Turn the guard on for the block (``read.language_guard == "on"``); off: no-op."""
    if not on:
        yield
        return
    token = _GUARD.set(True)
    try:
        yield
    finally:
        _GUARD.reset(token)


def blocked(text: str) -> bool:
    """True when the guard is on and ``text`` is not English: the caller fails closed."""
    return _GUARD.get() and not looks_english(text)


F = TypeVar("F", bound=Callable[..., Any])


def english_only(default: Any = False) -> Callable[[F], F]:
    """Decorator for a function whose first argument is the text: under the guard and for
    non-English text it returns ``default`` instead of running."""

    def wrap(fn: F) -> F:
        @functools.wraps(fn)
        def inner(text: str, *args: Any, **kwargs: Any) -> Any:
            if blocked(text):
                return default
            return fn(text, *args, **kwargs)

        return inner  # type: ignore[return-value]

    return wrap
