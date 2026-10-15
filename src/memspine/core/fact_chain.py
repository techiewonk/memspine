"""E01 write-time arm: validated atomic assertions (pure).

``memories.semantic.policies.fact_projection`` makes ONE bounded structured call per closed
session and gets subject-relation-object assertions, each with the exact source span, its
modality and its time as written. This module holds what needs no service:

* :func:`validate_assertions` keeps an assertion only when its span really is a substring of
  the numbered line it cites (whitespace, case and punctuation aside), so a paraphrase or an
  invented relation never reaches a deposit.

The read-time chain join (``read.fact_chain``) was measured net -1 and is retired (G-22,
``docs/v2/RETIRED.md``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "Assertion",
    "fact_projection_on",
    "norm_entity",
    "slot_name",
    "validate_assertions",
]

#: Modalities that never take part in a join: the assertion does not state a fact.
NON_JOINING = frozenset(
    {"negated", "hypothetical", "uncertain", "conditional", "question", "doubt", "denied"}
)
_PRONOUNS = frozenset(
    [
        "he",
        "she",
        "it",
        "they",
        "them",
        "him",
        "her",
        "his",
        "hers",
        "its",
        "their",
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "this",
        "that",
        "these",
        "those",
        "someone",
        "somebody",
        "something",
        "anyone",
        "one",
    ]
)
_ARTICLES = re.compile(r"^(?:the|a|an)\s+", re.I)
_POSSESSIVE = re.compile(r"^(?:my|his|her|their|our|your|its|the)\s+", re.I)
_OWNER = re.compile(r"^[\w.-]+['’]s\s+", re.I)  # noqa: RUF001
_PUNCT = re.compile(r"[^\w\s]+", re.U)


def _squash(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.casefold().replace("_", " ")).split())


def fact_projection_on(policies: object) -> bool:
    """``memories.semantic.policies.fact_projection``: ``off`` (default) | ``on``.

    A bare YAML ``on`` / ``off`` arrives as a boolean and is read as such. Anything else
    raises ``ValueError`` (the engine turns it into a config error at start)."""
    value = policies.get("fact_projection", "off") if hasattr(policies, "get") else "off"
    if value is None or value is False or str(value).strip().casefold() in ("off", "false", "no"):
        return False
    if value is True or str(value).strip().casefold() in ("on", "true", "yes"):
        return True
    raise ValueError(
        f"unknown memories.semantic.policies.fact_projection {value!r} (valid: off, on)"
    )


def norm_entity(text: str) -> str:
    """An entity name for exact comparison: case, punctuation, a leading article and a
    possessive ``'s`` suffix removed. Pronouns and empty names normalise to ``""``."""
    text = _ARTICLES.sub("", text.strip())
    text = re.sub(r"['’]s\b", "", text)  # noqa: RUF001
    out = _squash(text)
    return "" if out in _PRONOUNS else out


def slot_name(text: str) -> str:
    """A relation or slot name for exact comparison (``home_country`` == "his home country"
    == "Sven's home country")."""
    text = _OWNER.sub("", _POSSESSIVE.sub("", text.strip()))
    return _squash(text)


@dataclass(frozen=True, slots=True)
class Assertion:
    subject: str
    relation: str
    object: str
    line: int  # 1-based index of the numbered evidence line
    span: str
    modality: str = "asserted"
    time: str = ""

    @property
    def joinable(self) -> bool:
        return self.modality.casefold() not in NON_JOINING


def validate_assertions(items: Iterable[object], lines: Sequence[str]) -> list[Assertion]:
    """The assertions whose cited line exists and contains their span.

    ``items`` are objects with the :class:`~memspine.prompts.models.AssertionOut` fields.
    Duplicates (same triple on the same line) are dropped."""
    shown = [_squash(line) for line in lines]
    out: list[Assertion] = []
    seen: set[tuple[str, str, str, int]] = set()
    for item in items:
        line = int(getattr(item, "line", 0) or 0)
        span = str(getattr(item, "span", "") or "").strip()
        if not 1 <= line <= len(lines) or not span:
            continue
        needle = _squash(span)
        if not needle or needle not in shown[line - 1]:
            continue
        a = Assertion(
            subject=str(getattr(item, "subject", "")).strip(),
            relation=str(getattr(item, "relation", "")).strip(),
            object=str(getattr(item, "object", "")).strip(),
            line=line,
            span=span,
            modality=str(getattr(item, "modality", "") or "asserted").strip().casefold(),
            time=str(getattr(item, "time", "") or "").strip(),
        )
        key = (norm_entity(a.subject), slot_name(a.relation), norm_entity(a.object), line)
        if key in seen or not (key[0] and key[1] and key[2]):
            continue
        seen.add(key)
        out.append(a)
    return out
